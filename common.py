"""Shared constants and helpers for the Hybrid FTP control (TCP) and data (UDP) channels."""

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
    Call this right before a receive loop starts to guarantee a clean slate."""
    sock.settimeout(0)
    try:
        while True:
            sock.recvfrom(CHUNK_SIZE + 64)
    except (BlockingIOError, socket.timeout):
        pass


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
                 "SIZE MDTM TYPE MODE STOR RETR HELP")
    MODE_NOT_IMPLEMENTED = "502 Command not implemented (Advanced Level supports MODE S only)."
    FILE_STATUS_OK = "150 File status okay, opening data connection."
    TRANSFER_COMPLETE = "226 Transfer complete."
    TRANSFER_ABORTED = "426 Connection closed; transfer aborted."
    CANT_OPEN_DATA_CONN = "425 Can't open data connection."
    FILE_UNAVAILABLE = "550 File unavailable."
    TYPE_NOT_IMPLEMENTED = "502 Command not implemented (supported: TYPE A, TYPE I)."
    SYNTAX_ERROR_CMD = "500 Syntax error, command unrecognized."
    SYNTAX_ERROR_PARAMS = "501 Syntax error in parameters."
    NOT_IMPLEMENTED = "502 Command not implemented."
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
    def status(session):
        return (f"211 user={session.username!r} cwd={session.cwd} "
                f"mode={session.data_mode} type={session.type_mode}")
