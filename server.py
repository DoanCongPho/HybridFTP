"""Hybrid FTP server — Basic Level.

Control channel: TCP, fixed port (see common.CONTROL_PORT).
Data channel:    UDP, fixed port (see common.DATA_PORT), single client at a time.

Scope (Basic Level only): USER/PASS auth, ASCII upload/download of a single
file, one fixed data-channel mechanism, single-threaded (one session at a
time). See README.md for what is deliberately not implemented yet.
"""

import functools
import os
import socket

print = functools.partial(print, flush=True)  # keep server log visible even when output is redirected

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, DataMode,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, recv_line, send_line,
)

STORAGE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_storage")

USERS = {
    "alice": "password123",
    "bob": "hunter2",
}

# Commands defined by the spec but not implemented at Basic Level. Listed
# explicitly (rather than falling through to a generic "unknown command")
# so the dispatcher already has a named slot for each when it's time to
# build the Advanced/Excellent behavior.
NOT_IMPLEMENTED = {
    "CWD", "CDUP", "MKD", "RMD", "LIST", "NLST", "STAT", "MDTM", "MODE",
    "STOU", "APPE", "DELE", "RNFR", "RNTO", "HASH", "ABOR",
}


def safe_path(filename):
    """Basic Level: single flat storage directory, no directory traversal."""
    name = os.path.basename(filename)
    return os.path.join(STORAGE_ROOT, name)


class Session:
    def __init__(self, conn, addr, udp_sock):
        self.conn = conn
        self.addr = addr
        self.udp_sock = udp_sock
        self.username = None
        self.authenticated = False
        self.type_mode = "A"
        self.data_mode = DataMode.FIXED   # only mode implemented at Basic Level
        self.client_data_addr = None      # learned from the client's HELLO datagram

    def resolve_data_endpoint(self):
        """Single seam for finding out where to send/receive file data.

        Basic Level only implements DataMode.FIXED (the address the client
        announced via HELLO right after login). Advanced Level active/passive
        mode switching would add branches here — nowhere else needs to change.
        """
        if self.data_mode == DataMode.FIXED:
            return self.client_data_addr
        raise NotImplementedError(f"data mode {self.data_mode!r} not implemented at Basic Level")


def handle_stor(session, filename):
    if not filename:
        send_line(session.conn, "501 Syntax error in parameters.")
        return
    path = safe_path(filename)
    send_line(session.conn, "150 File status okay, opening data connection.")
    chunks = {}
    session.udp_sock.settimeout(SOCK_TIMEOUT)
    try:
        while True:
            raw, _addr = session.udp_sock.recvfrom(CHUNK_SIZE + 64)
            pkt_type, seq, payload, valid = parse_packet(raw)
            if not valid:
                print(f"[!] Corrupt packet seq={seq} dropped.")
                continue
            if pkt_type == PKT_FIN:
                break
            if pkt_type == PKT_DATA:
                chunks[seq] = payload
    except socket.timeout:
        send_line(session.conn, "426 Connection closed; transfer aborted.")
        return
    with open(path, "wb") as f:
        for seq in sorted(chunks):
            f.write(chunks[seq])
    size = sum(len(c) for c in chunks.values())
    print(f"[+] Stored '{filename}' ({size} bytes) from user '{session.username}'")
    send_line(session.conn, "226 Transfer complete.")


def handle_retr(session, filename):
    if not filename:
        send_line(session.conn, "501 Syntax error in parameters.")
        return
    path = safe_path(filename)
    if not os.path.isfile(path):
        send_line(session.conn, "550 File unavailable.")
        return
    endpoint = session.resolve_data_endpoint()
    if endpoint is None:
        send_line(session.conn, "425 Can't open data connection.")
        return
    send_line(session.conn, "150 File status okay, opening data connection.")
    with open(path, "rb") as f:
        data = f.read()
    seq = 0
    for i in range(0, len(data), CHUNK_SIZE):
        chunk = data[i:i + CHUNK_SIZE]
        session.udp_sock.sendto(make_packet(PKT_DATA, seq, chunk), endpoint)
        seq += 1
    session.udp_sock.sendto(make_packet(PKT_FIN, seq), endpoint)
    print(f"[+] Sent '{filename}' ({len(data)} bytes) to user '{session.username}'")
    send_line(session.conn, "226 Transfer complete.")


def handle_client(conn, addr, udp_sock):
    session = Session(conn, addr, udp_sock)
    print(f"[+] Connection from {addr}")
    send_line(conn, "220 Service ready.")
    try:
        while True:
            line = recv_line(conn)
            if line is None:
                print(f"[-] {addr} disconnected.")
                break
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper()
            arg = parts[1].strip() if len(parts) > 1 else ""
            print(f"[{addr}] {line}")

            if cmd == "USER":
                if not arg:
                    send_line(conn, "501 Syntax error in parameters.")
                    continue
                session.username = arg
                session.authenticated = False
                send_line(conn, "331 Username OK, need password.")

            elif cmd == "PASS":
                if session.username and USERS.get(session.username) == arg:
                    session.authenticated = True
                    send_line(conn, "230 Login successful.")
                    # Fixed data-channel handshake: wait for the client's
                    # HELLO datagram so we learn its UDP address.
                    udp_sock.settimeout(SOCK_TIMEOUT)
                    try:
                        raw, caddr = udp_sock.recvfrom(1024)
                        pkt_type, _, _, _ = parse_packet(raw)
                        if pkt_type == PKT_HELLO:
                            session.client_data_addr = caddr
                    except socket.timeout:
                        pass
                else:
                    send_line(conn, "530 Not logged in.")

            elif cmd == "NOOP":
                send_line(conn, "200 Command OK.")

            elif cmd == "PWD":
                send_line(conn, f'257 "{STORAGE_ROOT}"')

            elif cmd == "TYPE":
                mode = arg.upper()
                if mode == "A":
                    session.type_mode = "A"
                    send_line(conn, "200 Command OK.")
                else:
                    send_line(conn, "502 Command not implemented (Basic Level supports TYPE A only).")

            elif cmd == "SIZE":
                path = safe_path(arg)
                if arg and os.path.isfile(path):
                    send_line(conn, f"213 {os.path.getsize(path)}")
                else:
                    send_line(conn, "550 File unavailable.")

            elif cmd == "HELP":
                send_line(conn, "214 Commands: USER PASS QUIT NOOP PWD TYPE SIZE STOR RETR HELP")

            elif cmd in ("PORT", "PASV"):
                send_line(conn, "502 Command not implemented (reserved for Advanced Level active/passive mode).")

            elif cmd == "STOR":
                if not session.authenticated:
                    send_line(conn, "530 Not logged in.")
                else:
                    handle_stor(session, arg)

            elif cmd == "RETR":
                if not session.authenticated:
                    send_line(conn, "530 Not logged in.")
                else:
                    handle_retr(session, arg)

            elif cmd == "QUIT":
                send_line(conn, "221 Goodbye.")
                break

            elif cmd in NOT_IMPLEMENTED:
                send_line(conn, "502 Command not implemented.")

            else:
                send_line(conn, "500 Syntax error, command unrecognized.")
    finally:
        conn.close()


def main():
    host = "0.0.0.0"
    os.makedirs(STORAGE_ROOT, exist_ok=True)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind((host, DATA_PORT))

    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind((host, CONTROL_PORT))
    tcp_sock.listen(1)
    print(f"[*] Hybrid FTP server listening on TCP {CONTROL_PORT}, UDP data port {DATA_PORT}")
    print(f"[*] Storage root: {STORAGE_ROOT}")

    try:
        while True:
            conn, addr = tcp_sock.accept()
            handle_client(conn, addr, udp_sock)   # single-threaded: one client at a time
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        tcp_sock.close()
        udp_sock.close()


if __name__ == "__main__":
    main()
