"""Hybrid FTP client — Basic Level. Interactive CLI.

Usage:
    python3 client.py <server_host>

Commands:
    user <name>              PASS <name>
    pass <password>
    put <local_file> [remote_name]     upload (STOR)
    get <remote_name> [local_file]     download (RETR)
    pwd
    size <filename>
    noop
    help
    quit
"""

import os
import sys
import socket

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, DataMode,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, recv_line, send_line,
)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_downloads")


class FTPClient:
    def __init__(self, host, control_port=CONTROL_PORT, data_port=DATA_PORT):
        self.host = host
        self.data_port = data_port
        self.data_mode = DataMode.FIXED   # only mode implemented at Basic Level
        self.authenticated = False

        self.conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.conn.connect((host, control_port))

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("", 0))  # ephemeral port; server discovers it via our HELLO packet

        print(self._read_reply())

    def _read_reply(self):
        line = recv_line(self.conn)
        if line is None:
            raise ConnectionError("Server closed the connection.")
        return line

    def command(self, text):
        send_line(self.conn, text)
        return self._read_reply()

    def login(self, username, password):
        """Send PASS and, on success, register our UDP address with a HELLO packet.

        Assumes USER has already been sent for `username` (the REPL sends it
        as soon as the user types `user <name>`, before `pass` is typed).
        """
        reply = self.command(f"PASS {password}")
        print(reply)
        if reply.startswith("230"):
            self.authenticated = True
            if self.data_mode == DataMode.FIXED:
                # Register our UDP address with the server for this session.
                self.udp_sock.sendto(make_packet(PKT_HELLO, 0, username.encode("ascii")),
                                      (self.host, self.data_port))
            else:
                raise NotImplementedError(f"data mode {self.data_mode!r} not implemented at Basic Level")
        return self.authenticated

    def put(self, local_path, remote_name=None):
        if not os.path.isfile(local_path):
            print(f"[!] Local file not found: {local_path}")
            return
        remote_name = remote_name or os.path.basename(local_path)
        reply = self.command(f"STOR {remote_name}")
        print(reply)
        if not reply.startswith("150"):
            return
        with open(local_path, "rb") as f:
            data = f.read()
        seq = 0
        for i in range(0, len(data), CHUNK_SIZE):
            chunk = data[i:i + CHUNK_SIZE]
            self.udp_sock.sendto(make_packet(PKT_DATA, seq, chunk), (self.host, self.data_port))
            seq += 1
        self.udp_sock.sendto(make_packet(PKT_FIN, seq), (self.host, self.data_port))
        print(self._read_reply())

    def get(self, remote_name, local_path=None):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        local_path = local_path or os.path.join(DOWNLOAD_DIR, remote_name)
        reply = self.command(f"RETR {remote_name}")
        print(reply)
        if not reply.startswith("150"):
            return
        chunks = {}
        self.udp_sock.settimeout(SOCK_TIMEOUT)
        try:
            while True:
                raw, _addr = self.udp_sock.recvfrom(CHUNK_SIZE + 64)
                pkt_type, seq, payload, valid = parse_packet(raw)
                if not valid:
                    print(f"[!] Corrupt packet seq={seq} dropped.")
                    continue
                if pkt_type == PKT_FIN:
                    break
                if pkt_type == PKT_DATA:
                    chunks[seq] = payload
        except socket.timeout:
            print("[!] Data transfer timed out.")
            return
        with open(local_path, "wb") as f:
            for seq in sorted(chunks):
                f.write(chunks[seq])
        print(f"[+] Saved to {local_path} ({sum(len(c) for c in chunks.values())} bytes)")
        print(self._read_reply())

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass
        self.udp_sock.close()


def repl(host):
    client = FTPClient(host)
    try:
        while True:
            try:
                line = input("ftp> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "user":
                client._pending_user = arg
                print(client.command(f"USER {arg}"))
            elif cmd == "pass":
                username = getattr(client, "_pending_user", None)
                if not username:
                    print("[!] Run 'user <name>' first.")
                    continue
                client.login(username, arg)
            elif cmd == "put":
                bits = arg.split(maxsplit=1)
                if not bits:
                    print("Usage: put <local_file> [remote_name]")
                    continue
                client.put(bits[0], bits[1] if len(bits) > 1 else None)
            elif cmd == "get":
                bits = arg.split(maxsplit=1)
                if not bits:
                    print("Usage: get <remote_name> [local_file]")
                    continue
                client.get(bits[0], bits[1] if len(bits) > 1 else None)
            elif cmd == "pwd":
                print(client.command("PWD"))
            elif cmd == "size":
                print(client.command(f"SIZE {arg}"))
            elif cmd == "noop":
                print(client.command("NOOP"))
            elif cmd == "help":
                print(client.command("HELP"))
            elif cmd == "quit":
                print(client.command("QUIT"))
                break
            else:
                print(f"[!] Unknown local command: {cmd}")
    finally:
        client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 client.py <server_host>")
        sys.exit(1)
    repl(sys.argv[1])
