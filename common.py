"""Shared constants and helpers for the Hybrid FTP control (TCP) and data (UDP) channels."""

import re
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
                 "SIZE MDTM TYPE MODE PORT PASV STOR RETR HELP")
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
