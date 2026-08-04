"""Shared constants and helpers for the Hybrid FTP control (TCP) and data (UDP) channels."""

CONTROL_PORT = 2121
CONTROL_IDLE_TIMEOUT = 300.0  # seconds of TCP control-channel silence before the (single-
                               # threaded) server gives up on a client that never sends QUIT —
                               # covers a hard crash / lost network where no FIN or RST ever
                               # arrives, which recv() alone can't detect. NOOP resets this timer.


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
