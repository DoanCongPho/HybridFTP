"""Hybrid FTP client — Basic + Advanced Level. Interactive CLI.

Usage:
    python3 client.py <server_host>

Commands:
    user <name>
    pass <password>
    put <local_file> [remote_name]     upload (STOR)
    get <remote_name> [local_file]     download (RETR)
    cwd <path>                         change server directory
    cdup                               go to parent directory
    mkd <dirname>                      create a directory
    rmd <dirname>                      remove an (empty) directory
    ls / list [path]                   detailed directory listing
    nlst [path]                        name-only directory listing
    stat [path]                        server/session or path status
    mdtm <filename>                    last-modified timestamp
    type {A|I}                         ASCII or binary transfer type
    active                             switch to Active mode (PORT)
    passive                            switch to Passive mode (PASV)
    fixed                              switch back to Basic Level fixed mode
    pwd
    size <filename>
    noop
    help
    quit
"""

import os
import re
import socket
import sys
import time

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, DataMode,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, parse_pasv_reply, format_port_arg, recv_line, send_line,
    drain_stale_packets, ascii_mask,
)
from config import CONFIG

DOWNLOAD_DIR_CFG = CONFIG.get("client", "download_dir", fallback="client_downloads")
DOWNLOAD_DIR = (DOWNLOAD_DIR_CFG if os.path.isabs(DOWNLOAD_DIR_CFG)
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)), DOWNLOAD_DIR_CFG))

_DEFAULT_DATA_MODE = CONFIG.get("client", "data_mode", fallback="fixed").strip().lower()
_MODE_MAP = {"fixed": DataMode.FIXED, "active": DataMode.ACTIVE, "passive": DataMode.PASSIVE}


class FTPClient:
    def __init__(self, host, control_port=CONTROL_PORT, data_port=DATA_PORT):
        # Resolve to a numeric IP once, up front — recvfrom() always hands
        # back a resolved numeric IP, so keeping `host` as a hostname would
        # make later address comparisons never match.
        self.host = socket.gethostbyname(host)
        self.data_port = data_port
        self.authenticated = False

        # Data-channel mode. FIXED reproduces Basic Level exactly (HELLO to
        # the server's well-known DATA_PORT). ACTIVE/PASSIVE are negotiated
        # live via PORT/PASV — see set_active()/set_passive()/set_fixed().
        self.data_mode = _MODE_MAP.get(_DEFAULT_DATA_MODE, DataMode.FIXED)
        self.type_mode = "A"   # RFC 959 default
        self.active_server_port = None   # learned from the server's PORT reply
        self.passive_target = None       # learned from the server's PASV reply

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

    def _data_target(self):
        if self.data_mode == DataMode.FIXED:
            return (self.host, self.data_port)
        if self.data_mode == DataMode.ACTIVE:
            return (self.host, self.active_server_port)
        if self.data_mode == DataMode.PASSIVE:
            return self.passive_target
        raise NotImplementedError(f"data mode {self.data_mode!r} not implemented")

    def set_fixed(self):
        """Switch back to Basic Level's fixed data-channel mechanism."""
        self.data_mode = DataMode.FIXED
        self.udp_sock.sendto(make_packet(PKT_HELLO, 0, b"HELLO"), (self.host, self.data_port))
        print(f"[*] Data mode: FIXED ({self.host}:{self.data_port})")

    def set_active(self):
        """Advanced Level: tell the server our address via PORT; it opens a
        dedicated per-session socket and reports its port back (needed for
        the upload direction, since UDP has no server-initiated connect())."""
        local_ip = self.conn.getsockname()[0]
        local_port = self.udp_sock.getsockname()[1]
        reply = self.command(f"PORT {format_port_arg(local_ip, local_port)}")
        print(reply)
        if not reply.startswith("200"):
            return False
        m = re.search(r"data port (\d+)", reply)
        if not m:
            print("[!] Server did not report a data port; staying on the previous mode.")
            return False
        self.active_server_port = int(m.group(1))
        self.data_mode = DataMode.ACTIVE
        print(f"[*] Data mode: ACTIVE (server will use port {self.active_server_port})")
        return True

    def set_passive(self):
        """Advanced Level: ask the server to open a per-session socket via
        PASV, then HELLO it so it learns our address (same mechanism FIXED
        mode uses, just on a private port)."""
        reply = self.command("PASV")
        print(reply)
        if not reply.startswith("227"):
            return False
        parsed = parse_pasv_reply(reply)
        if parsed is None:
            print("[!] Could not parse PASV reply.")
            return False
        self.passive_target = parsed
        self.data_mode = DataMode.PASSIVE
        self.udp_sock.sendto(make_packet(PKT_HELLO, 0, b"HELLO"), self.passive_target)
        print(f"[*] Data mode: PASSIVE (server at {self.passive_target[0]}:{self.passive_target[1]})")
        return True

    def login(self, username, password):
        """Send PASS and, on success, establish the data channel using
        whichever mode is currently selected (config default, or a prior
        `active`/`passive` REPL command)."""
        reply = self.command(f"PASS {password}")
        print(reply)
        if reply.startswith("230"):
            self.authenticated = True
            if self.data_mode == DataMode.ACTIVE:
                self.set_active()
            elif self.data_mode == DataMode.PASSIVE:
                self.set_passive()
            else:
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
        target = self._data_target()
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

    def list_dir(self, path="", name_only=False):
        cmd = "NLST" if name_only else "LIST"
        drain_stale_packets(self.udp_sock)
        reply = self.command(f"{cmd} {path}".strip())
        print(reply)
        if not reply.startswith("150"):
            return
        data = self._recv_data_payload()
        if data is None:
            print(self._read_reply())  # drain the pending final reply — see get()'s comment
            return
        text = data.decode("utf-8", errors="replace")
        print(text if text else "(empty)")
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
            elif cmd in ("ls", "list"):
                client.list_dir(arg, name_only=False)
            elif cmd == "nlst":
                client.list_dir(arg, name_only=True)
            elif cmd == "cwd":
                print(client.command(f"CWD {arg}"))
            elif cmd == "cdup":
                print(client.command("CDUP"))
            elif cmd == "mkd":
                print(client.command(f"MKD {arg}"))
            elif cmd == "rmd":
                print(client.command(f"RMD {arg}"))
            elif cmd == "stat":
                print(client.command(f"STAT {arg}".strip()))
            elif cmd == "mdtm":
                reply = client.command(f"MDTM {arg}")
                print(reply)
                # The wire format (YYYYMMDDhhmmss) is the exact one the spec
                # requires for MDTM — reformat only for display, don't touch
                # what's sent/received on the wire.
                m = re.match(r"213 (\d{14})", reply)
                if m:
                    t = time.strptime(m.group(1), "%Y%m%d%H%M%S")
                    print(f"[+] Last modified: {time.strftime('%Y-%m-%d %H:%M:%S', t)} UTC")
            elif cmd == "type":
                mode = (arg or "A").upper()
                reply = client.command(f"TYPE {mode}")
                print(reply)
                if reply.startswith("200"):
                    client.type_mode = mode
            elif cmd == "active":
                client.set_active()
            elif cmd == "passive":
                client.set_passive()
            elif cmd == "fixed":
                client.set_fixed()
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
