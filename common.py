"""Shared constants and helpers for the Hybrid FTP control (TCP) and data (UDP) channels."""

import hashlib
import re
import socket
import struct
import zlib

CONTROL_PORT = 2121
DATA_PORT = 2122          # fixed server-side UDP data port (Basic Level: single fixed mechanism)
CHUNK_SIZE = 1024         # payload bytes per UDP data packet
SOCK_TIMEOUT = 5.0        # seconds to wait for a UDP packet before declaring the transfer failed
CONTROL_IDLE_TIMEOUT = 300.0  # seconds of TCP control-channel silence before the (single-
                               # threaded) server gives up on a client that never sends QUIT —
                               # covers a hard crash / lost network where no FIN or RST ever
                               # arrives, which recv() alone can't detect. NOOP resets this timer.

# --- Data-channel mode -------------------------------------------------------
# Basic Level only ever uses FIXED. ACTIVE/PASSIVE are named placeholders so a
# future Advanced-Level implementation has somewhere to plug in without
# renaming anything that already ships.
class DataMode:
    FIXED = "FIXED"
    ACTIVE = "ACTIVE"      # reserved, not implemented at Basic Level
    PASSIVE = "PASSIVE"    # reserved, not implemented at Basic Level


# --- UDP packet framing -------------------------------------------------------
PKT_HELLO = 0   # client -> server: "here is my UDP address, remember it for this session"
PKT_DATA = 1    # a chunk of file payload
PKT_FIN = 2     # marks the end of a transfer
PKT_ACK = 3     # Go-Back-N cumulative ACK (Excellent Level reliable-UDP layer): `seq` is the
                # highest correctly-received-in-order DATA/FIN seq so far, empty payload

HEADER_FMT = "!BII"       # type(1B) + seq(4B) + checksum(4B)
HEADER_SIZE = struct.calcsize(HEADER_FMT)


def make_packet(pkt_type, seq, payload=b""):
    checksum = zlib.crc32(payload) & 0xFFFFFFFF
    return struct.pack(HEADER_FMT, pkt_type, seq, checksum) + payload


def parse_packet(raw):
    """Returns (pkt_type, seq, payload, checksum_valid).

    Packets shorter than the header (e.g. stray/non-protocol UDP traffic
    hitting the data port) are reported as invalid instead of raising, so a
    malformed datagram can't take down the whole server.
    """
    if len(raw) < HEADER_SIZE:
        return None, None, b"", False
    pkt_type, seq, checksum = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
    payload = raw[HEADER_SIZE:]
    valid = (zlib.crc32(payload) & 0xFFFFFFFF) == checksum
    return pkt_type, seq, payload, valid


def drain_stale_packets(sock):
    """Discard any datagrams already sitting in `sock`'s receive buffer
    before starting a new data-channel operation.

    FIXED mode reuses one UDP socket for an entire session (or, server-side,
    the whole server lifetime) rather than opening a fresh one per transfer.
    Packet `seq` numbers reset to 0 for every new STOR/RETR/LIST/etc — there
    is no other field identifying which logical transfer a packet belongs
    to. Over a real network, a straggler packet from a *previous* transfer
    (e.g. one the receiver already gave up on after a timeout, but the
    sender fired anyway with no ACK to know better) can still be in flight
    or unread in the OS socket buffer when the *next* command starts — and
    gets misread as belonging to the new transfer, silently corrupting it
    with leftover data from the old one. Call this right before a receive
    loop starts (both gbn_receive() and the best-effort paths in
    server.py/client.py) to guarantee a clean slate."""
    sock.settimeout(0)
    try:
        while True:
            sock.recvfrom(CHUNK_SIZE + 64)
    except (BlockingIOError, socket.timeout):
        pass


# --- Go-Back-N reliable-UDP layer (Excellent Level) ---------------------------
# Opt-in via config.ini's [reliability] mode=gbn (default "none" = the
# original best-effort framing above: checksummed and sequenced, but no
# ACK/retransmit). Both ends of a transfer must agree on the mode — there's
# no way for one side to "opt out" while the other expects GBN framing, the
# same way two TCP stacks must speak the same protocol version. Living here
# (not duplicated in server.py/client.py) is what guarantees that: both
# STOR/RETR/LIST (server) and put/get/list_dir (client) call the exact same
# sender/receiver implementation.
#
# Sender: a sliding window of up to `window_size` unacknowledged packets in
# flight (the flow-control knob — the rubric's "Sliding Window" requirement),
# cumulative ACKs, and a single timer for the oldest unacked packet; on
# timeout, the whole in-flight window is retransmitted (classic GBN, as
# opposed to Selective Repeat's per-packet retransmission). The stream's
# closing PKT_FIN is packet number N (one past the last DATA chunk) and rides
# the same reliable pipeline, so "transfer complete" is itself acknowledged —
# unlike the best-effort path, where a lost FIN just means the receiver waits
# out SOCK_TIMEOUT with no way to tell "done" from "still coming".
#
# Receiver: only ever advances on the *next expected* in-order seq; anything
# else (corrupt, out-of-order, or a duplicate retransmission of an already-
# acked packet because the sender never saw its ACK) gets the last good
# cumulative ACK re-sent, never buffered — this is what makes it Go-Back-N
# rather than Selective Repeat, and why the sender's retransmission always
# resends the whole window instead of just the missing packet.

GBN_WINDOW_SIZE = 4     # default packets-in-flight; overridden by config.ini [reliability] window_size
GBN_RTO = 0.3           # default per-packet retransmit timeout (seconds); overridden by rto_ms
GBN_MAX_RETRIES = 30    # default consecutive-timeout cap before giving up; overridden by max_retries


def gbn_send(sock, dest_addr, data, chunk_size=CHUNK_SIZE, window_size=GBN_WINDOW_SIZE,
             rto=GBN_RTO, max_retries=GBN_MAX_RETRIES, on_retransmit=None):
    """Reliably send `data` to dest_addr using Go-Back-N. Returns True once
    the final PKT_FIN is acknowledged, False if max_retries consecutive
    timeouts elapse first (peer presumed gone/unreachable).

    Drains any stale packets (e.g. a late duplicate ACK from a *previous*
    gbn_send() call on this same socket) before sending anything — safe to
    do here, unlike on the receive side, because at this exact point nothing
    has been sent yet for this call, so nothing genuine could be waiting.

    `on_retransmit(base, next_seq, retries)`, if given, is called every time
    an RTO fires and the in-flight window gets resent — purely observational
    (e.g. for demo logging of real packet loss recovery), never affects the
    protocol's own correctness."""
    drain_stale_packets(sock)
    packets = []
    seq = 0
    for i in range(0, len(data), chunk_size):
        packets.append(make_packet(PKT_DATA, seq, data[i:i + chunk_size]))
        seq += 1
    packets.append(make_packet(PKT_FIN, seq))   # FIN is seq N — part of the same reliable stream
    total = len(packets)

    base = 0          # oldest seq not yet acknowledged
    next_seq = 0       # next seq not yet sent
    retries = 0
    sock.settimeout(rto)

    def send_window():
        nonlocal next_seq
        while next_seq < total and next_seq < base + window_size:
            sock.sendto(packets[next_seq], dest_addr)
            next_seq += 1

    send_window()
    while base < total:
        try:
            raw, addr = sock.recvfrom(HEADER_SIZE + 64)
            if addr != dest_addr:
                continue
            pkt_type, ack_seq, _, valid = parse_packet(raw)
            if not valid or pkt_type != PKT_ACK:
                continue
            if ack_seq >= base:
                base = ack_seq + 1
                retries = 0
                send_window()
        except socket.timeout:
            retries += 1
            if retries > max_retries:
                return False
            if on_retransmit:
                on_retransmit(base, next_seq, retries)
            for s in range(base, next_seq):   # GBN: resend the whole in-flight window
                sock.sendto(packets[s], dest_addr)
    return True


def gbn_receive(sock, expected_addr, rto=GBN_RTO, max_retries=GBN_MAX_RETRIES, on_reack=None):
    """Reliably receive a Go-Back-N stream from expected_addr. Returns the
    reassembled payload bytes once PKT_FIN arrives in order, or None if
    nothing usable arrives for max_retries consecutive rto-second waits.

    Does NOT drain the socket itself — unlike gbn_send(), by the time a
    receiver is invoked the peer may already be sending (it was just told to
    start), so draining here on a fast/local network can discard genuinely
    fresh packets that arrived before this call started. The caller must
    drain *before* announcing readiness to the peer (before replying 150 /
    before sending the request) instead — see handle_stor()/get()/list_dir().

    `on_reack(expected_seq, got_seq)`, if given, is called whenever we have to
    re-send the last cumulative ACK because of a corrupt/out-of-order/duplicate
    arrival — purely observational (e.g. for demo logging that real loss was
    detected and handled), never affects the protocol's own correctness."""
    chunks = []
    expected_seq = 0
    idle = 0
    sock.settimeout(rto)
    while True:
        try:
            raw, addr = sock.recvfrom(CHUNK_SIZE + 64)
            if addr != expected_addr:
                continue
            idle = 0
            pkt_type, seq, payload, valid = parse_packet(raw)
            if valid and pkt_type in (PKT_DATA, PKT_FIN) and seq == expected_seq:
                if pkt_type == PKT_DATA:
                    chunks.append(payload)
                sock.sendto(make_packet(PKT_ACK, expected_seq), addr)
                if pkt_type == PKT_FIN:
                    return b"".join(chunks)
                expected_seq += 1
            elif expected_seq > 0:
                # Corrupt, out-of-order, or a duplicate the sender re-sent
                # because our earlier ACK for it was lost — re-ACK the last
                # good cumulative seq so the sender's window can slide.
                if on_reack:
                    on_reack(expected_seq, seq if valid else None)
                sock.sendto(make_packet(PKT_ACK, expected_seq - 1), addr)
            # else: nothing correctly received yet, no valid ACK to send —
            # stay silent and let the sender's own timeout retry seq 0.
        except socket.timeout:
            idle += 1
            if idle > max_retries:
                return None


def ascii_mask(data):
    """RFC 959 TYPE A (NVT-ASCII) is nominally 7-bit ASCII: the sender clears
    the high bit of every byte before it goes on the wire. A genuine text
    file (every byte already <= 0x7F) survives this unchanged. A binary file
    (image, archive, ...) routinely has bytes >= 0x80 — roughly half of them,
    for arbitrary binary data — and those get permanently altered. This is
    irreversible by construction: there is no decode step, only encode. It's
    the textbook reason FTP folklore insists on TYPE I for binary transfers;
    called by the sending side only (handle_retr()/put()), never the
    receiving side, since the corrupted bytes it produces ARE what the
    receiver is meant to get, corruption and all."""
    return bytes(b & 0x7F for b in data)


def compute_hash(data, algorithm="sha256"):
    """SHA-256 (default) or MD5 hex digest of `data` — the Excellent Level
    end-to-end integrity check shared by HASH (server) and the client's
    optional post-transfer verification."""
    return hashlib.new(algorithm, data).hexdigest()


# --- PORT/PASV address encoding (Advanced Level: Active/Passive mode) --------
# Same "h1,h2,h3,h4,p1,p2" encoding RFC 959 uses for the PORT command and the
# PASV reply's parenthesized address, shared here since both client and
# server need to format/parse it identically.
def format_port_arg(ip, port):
    h1, h2, h3, h4 = ip.split(".")
    p1, p2 = divmod(port, 256)
    return f"{h1},{h2},{h3},{h4},{p1},{p2}"


def parse_port_arg(arg):
    """Returns (ip, port), or None if arg isn't a valid h1,h2,h3,h4,p1,p2 tuple."""
    try:
        parts = [int(x) for x in arg.strip().split(",")]
        if len(parts) != 6 or any(not (0 <= x <= 255) for x in parts):
            return None
        h1, h2, h3, h4, p1, p2 = parts
        return f"{h1}.{h2}.{h3}.{h4}", p1 * 256 + p2
    except ValueError:
        return None


def parse_pasv_reply(text):
    """Extracts (ip, port) from a '227 ... (h1,h2,h3,h4,p1,p2).' reply line."""
    m = re.search(r"\(([\d,]+)\)", text)
    if not m:
        return None
    return parse_port_arg(m.group(1))


# --- TCP control-channel line protocol ---------------------------------------
def recv_line(conn):
    """Read a single CRLF/LF-terminated line from a TCP socket. None on EOF."""
    chunks = []
    while True:
        b = conn.recv(1)
        if not b:
            return None
        if b == b"\n":
            break
        if b != b"\r":
            chunks.append(b)
    return b"".join(chunks).decode("ascii", errors="replace")


def send_line(conn, text):
    conn.sendall((text + "\r\n").encode("ascii"))


# --- Server reply codes (see project spec Sec.2.3) ----------------------------
# Every fixed-text reply the server sends, named instead of scattered as
# string literals through the command dispatcher in server.py.
class Reply:
    SERVICE_READY = "220 Service ready."
    USER_OK_NEED_PASS = "331 Username OK, need password."
    LOGIN_SUCCESS = "230 Login successful."
    NOT_LOGGED_IN = "530 Not logged in."
    COMMAND_OK = "200 Command OK."
    HELP_TEXT = ("214 Commands: USER PASS QUIT NOOP PWD CWD CDUP MKD RMD LIST NLST STAT "
                 "SIZE MDTM TYPE MODE PORT PASV STOR RETR HASH HELP")
    FILE_STATUS_OK = "150 File status okay, opening data connection."
    TRANSFER_COMPLETE = "226 Transfer complete."
    TRANSFER_ABORTED = "426 Connection closed; transfer aborted."
    CANT_OPEN_DATA_CONN = "425 Can't open data connection."
    FILE_UNAVAILABLE = "550 File unavailable."
    SYNTAX_ERROR_CMD = "500 Syntax error, command unrecognized."
    SYNTAX_ERROR_PARAMS = "501 Syntax error in parameters."
    NOT_IMPLEMENTED = "502 Command not implemented."
    TYPE_NOT_IMPLEMENTED = "502 Command not implemented (supported: TYPE A, TYPE I)."
    MODE_NOT_IMPLEMENTED = "502 Command not implemented (Advanced Level supports MODE S only)."
    GOODBYE = "221 Goodbye."

    @staticmethod
    def pwd(path):
        return f'257 "{path}"'

    @staticmethod
    def size(nbytes):
        return f"213 {nbytes}"

    @staticmethod
    def cwd_ok(path):
        return f'250 Directory changed to "{path}".'

    @staticmethod
    def dir_created(path):
        return f'257 "{path}" created.'

    @staticmethod
    def pasv(ip, port):
        return f"227 Entering Passive Mode ({format_port_arg(ip, port)})."

    @staticmethod
    def port_ok(server_port):
        # The extra "data port N" text is a deliberate, disclosed deviation
        # from RFC 959: real active-mode FTP doesn't need to report the
        # server's port back because TCP's server-initiated connect() reuses
        # one bidirectional socket. Our UDP data channel is connectionless,
        # so the client's upload direction (STOR) needs to be told where the
        # server's per-session socket lives; only the reply text carries that.
        return f"200 PORT command successful; server data port {server_port}."

    @staticmethod
    def status(session):
        return (f"211 user={session.username!r} cwd={session.cwd} "
                f"mode={session.data_mode} type={session.type_mode}")
