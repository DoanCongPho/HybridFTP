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
    HELP_TEXT = "214 Commands: USER PASS QUIT NOOP HELP"
    SYNTAX_ERROR_CMD = "500 Syntax error, command unrecognized."
    SYNTAX_ERROR_PARAMS = "501 Syntax error in parameters."
    NOT_IMPLEMENTED = "502 Command not implemented."
    GOODBYE = "221 Goodbye."
