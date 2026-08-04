"""Hybrid FTP client — Basic Level. Interactive CLI.

Usage:
    python3 client.py <server_host>

Commands:
    user <name>
    pass <password>
    fixed                              (re-)announce our address on the fixed data channel
    noop
    help
    quit
"""

import socket
import sys

from common import CONTROL_PORT, DATA_PORT, PKT_HELLO, make_packet, recv_line, send_line


class FTPClient:
    def __init__(self, host, control_port=CONTROL_PORT, data_port=DATA_PORT):
        # Resolve to a numeric IP once, up front — recvfrom() always hands
        # back a resolved numeric IP, so keeping `host` as a hostname would
        # make later address comparisons never match.
        self.host = socket.gethostbyname(host)
        self.data_port = data_port
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
