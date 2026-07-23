"""Shared constants and helpers for the Hybrid FTP control (TCP) and data (UDP) channels."""

import struct
import zlib

CONTROL_PORT = 2121
DATA_PORT = 2122          # fixed server-side UDP data port (Basic Level: single fixed mechanism)
CHUNK_SIZE = 1024         # payload bytes per UDP data packet
SOCK_TIMEOUT = 5.0        # seconds to wait for a UDP packet before declaring the transfer failed

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
    """Returns (pkt_type, seq, payload, checksum_valid)."""
    pkt_type, seq, checksum = struct.unpack(HEADER_FMT, raw[:HEADER_SIZE])
    payload = raw[HEADER_SIZE:]
    valid = (zlib.crc32(payload) & 0xFFFFFFFF) == checksum
    return pkt_type, seq, payload, valid


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
