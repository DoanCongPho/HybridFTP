"""Hybrid FTP client — Basic Level. Interactive CLI.

Usage:
    python3 client.py <server_host>

Commands:
    user <name>
    pass <password>
    put <local_file> [remote_name]     upload (STOR)
    get <remote_name> [local_file]     download (RETR)
    type A                             ASCII transfer type (default)
    fixed                              (re-)announce our address on the fixed data channel
    noop
    help
    quit
"""

import os
import socket
import sys

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, recv_line, send_line, drain_stale_packets, ascii_mask,
)
from config import CONFIG

DOWNLOAD_DIR_CFG = CONFIG.get("client", "download_dir", fallback="client_downloads")
DOWNLOAD_DIR = (DOWNLOAD_DIR_CFG if os.path.isabs(DOWNLOAD_DIR_CFG)
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)), DOWNLOAD_DIR_CFG))


class FTPClient:
    def __init__(self, host, control_port=CONTROL_PORT, data_port=DATA_PORT):
        # Resolve to a numeric IP once, up front — recvfrom() always hands
        # back a resolved numeric IP, so keeping `host` as a hostname would
        # make later address comparisons never match.
        self.host = socket.gethostbyname(host)
        self.data_port = data_port
        self.authenticated = False
        self.type_mode = "A"   # RFC 959 default

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

    def set_fixed(self):
        """Basic Level's fixed data-channel mechanism: HELLO the server's
        well-known UDP port so it learns our address."""
        self.udp_sock.sendto(make_packet(PKT_HELLO, 0, b"HELLO"), (self.host, self.data_port))
        print(f"[*] Data mode: FIXED ({self.host}:{self.data_port})")

    def login(self, username, password):
        reply = self.command(f"PASS {password}")
        print(reply)
        if reply.startswith("230"):
            self.authenticated = True
            self.udp_sock.sendto(make_packet(PKT_HELLO, 0, username.encode("ascii")),
                                  (self.host, self.data_port))
        return self.authenticated

    def _recv_data_payload(self):
        """Receive one data-channel transfer (PKT_DATA... PKT_FIN) and
        reassemble it in sequence order. Callers must drain_stale_packets()
        themselves *before* sending the RETR request — the server may start
        sending the instant it sees our request, so draining at this point
        risks discarding this transfer's own first packets."""
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
            return None
        return b"".join(chunks[s] for s in sorted(chunks))

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
        wire_data = ascii_mask(data) if self.type_mode == "A" else data
        target = (self.host, self.data_port)
        seq = 0
        for i in range(0, len(wire_data), CHUNK_SIZE):
            chunk = wire_data[i:i + CHUNK_SIZE]
            self.udp_sock.sendto(make_packet(PKT_DATA, seq, chunk), target)
            seq += 1
        self.udp_sock.sendto(make_packet(PKT_FIN, seq), target)
        print(self._read_reply())

    def get(self, remote_name, local_path=None):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        local_path = local_path or os.path.join(DOWNLOAD_DIR, remote_name)
        # Drain BEFORE asking — once the server sees RETR it may start
        # sending immediately, so draining any later risks discarding this
        # transfer's own first packets.
        drain_stale_packets(self.udp_sock)
        reply = self.command(f"RETR {remote_name}")
        print(reply)
        if not reply.startswith("150"):
            return
        data = self._recv_data_payload()
        if data is None:
            print(self._read_reply())
            return
        with open(local_path, "wb") as f:
            f.write(data)
        print(f"[+] Saved to {local_path} ({len(data)} bytes)")
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
            elif cmd == "type":
                mode = (arg or "A").upper()
                reply = client.command(f"TYPE {mode}")
                print(reply)
                if reply.startswith("200"):
                    client.type_mode = mode
            elif cmd == "fixed":
                client.set_fixed()
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
