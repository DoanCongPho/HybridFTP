"""Hybrid FTP client — Basic + Advanced Level. Interactive CLI.

Usage:
    python3 client.py <server_host>

Commands:
    user <name>                        PASS <name>
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
    hash <filename>                    server-side MD5/SHA-256 of a file
    pwd
    size <filename>
    noop
    help
    quit
"""

import os
import re
import sys
import socket
import threading
import time
import ssl

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, DataMode,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, parse_pasv_reply, format_port_arg, recv_line, send_line,
    gbn_send, gbn_receive, compute_hash, drain_stale_packets,
    encode_ascii_transfer, decode_ascii_transfer,
)
from config import CONFIG

DOWNLOAD_DIR_CFG = CONFIG.get("client", "download_dir", fallback="client_downloads")
DOWNLOAD_DIR = (DOWNLOAD_DIR_CFG if os.path.isabs(DOWNLOAD_DIR_CFG)
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)), DOWNLOAD_DIR_CFG))

_DEFAULT_DATA_MODE = CONFIG.get("client", "data_mode", fallback="fixed").strip().lower()
_MODE_MAP = {"fixed": DataMode.FIXED, "active": DataMode.ACTIVE, "passive": DataMode.PASSIVE}

# Excellent Level: Go-Back-N reliable-UDP layer. "none" (default) preserves
# the exact best-effort framing above; must match server.py's [reliability]
# setting to interoperate — see the comment above gbn_send() in common.py.
RELIABILITY_MODE = CONFIG.get("reliability", "mode", fallback="none").strip().lower()
GBN_WINDOW_SIZE = CONFIG.getint("reliability", "window_size", fallback=4)
GBN_RTO = CONFIG.getint("reliability", "rto_ms", fallback=300) / 1000.0
GBN_MAX_RETRIES = CONFIG.getint("reliability", "max_retries", fallback=30)

# Excellent Level: end-to-end MD5/SHA-256 hash verification.
VERIFY_HASH = CONFIG.getboolean("integrity", "verify", fallback=False)
HASH_ALGORITHM = CONFIG.get("integrity", "algorithm", fallback="sha256").strip().lower()
if HASH_ALGORITHM not in ("sha256", "md5"):
    HASH_ALGORITHM = "sha256"

# PASSIVE mode only: NAT/router UDP mappings are typically torn down after
# some seconds of no traffic on that (local-port, remote-addr) pair — far
# shorter than CONTROL_IDLE_TIMEOUT (300s). Below this, we're the side that
# created the mapping (our HELLO to the server's passive port), so we're
# also the side responsible for refreshing it while the session is
# otherwise idle. 0 (or absent) disables this entirely. Purely additive:
# the server never needs to know about these packets (see
# FTPClient._keepalive_loop()), so no config key is needed server-side.
KEEPALIVE_INTERVAL = CONFIG.getint("client", "keepalive_interval", fallback=15)

TLS_ENABLE = CONFIG.getboolean("tls", "enable", fallback=False)
TLS_CAFILE = CONFIG.get(
    "tls", "certfile",
    fallback=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs&keys", "cert.pem")
)

def _build_tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(TLS_CAFILE)
    return ctx

tls_context = _build_tls_context() if TLS_ENABLE else None


class FTPClient:
    def __init__(self, host, control_port=CONTROL_PORT, data_port=DATA_PORT):
        # Resolve to a numeric IP once, up front. gbn_send() matches incoming
        # ACKs against this same tuple via `addr == dest_addr`, and
        # recvfrom() always hands back a resolved numeric IP — so if `host`
        # were kept as a hostname (e.g. "localhost"), that comparison would
        # never match, every real ACK would be silently dropped, and a
        # transfer would look like 100% packet loss until max_retries gives up.
        self.host = socket.gethostbyname(host)
        self.data_port = data_port
        self.authenticated = False

        # Data-channel mode. FIXED reproduces Basic Level exactly (HELLO to
        # the server's well-known DATA_PORT). ACTIVE/PASSIVE are negotiated
        # live via PORT/PASV — see set_active()/set_passive()/set_fixed().
        self.data_mode = _MODE_MAP.get(_DEFAULT_DATA_MODE, DataMode.FIXED)
        self.type_mode = "A"   # RFC 959 default; kept in sync with the server via TYPE's 200 reply
        self.active_server_port = None   # learned from the server's PORT reply
        self.passive_target = None       # learned from the server's PASV reply

        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.connect((self.host, control_port))

        if TLS_ENABLE:
            try:
                self.conn = tls_context.wrap_socket(raw_sock, server_hostname=host)
            except ssl.SSLCertVerificationError as e:
                raw_sock.close()
                raise SystemExit(f"[TLS] Cert verify failed — is cert.pem the right one? {e}")
            except ssl.SSLError as e:
                raw_sock.close()
                raise SystemExit(f"[TLS] Handshake failed: {e}")
        else:
            self.conn = raw_sock

        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("", 0))  # ephemeral port; server discovers it via our HELLO packet

        # PASSIVE-mode NAT keepalive — see KEEPALIVE_INTERVAL and
        # _keepalive_loop(). _data_busy pauses keepalives during a real
        # transfer so they don't interleave with GBN's own ACK bookkeeping.
        self._keepalive_thread = None
        self._keepalive_stop = threading.Event()
        self._data_busy = False

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

    def _keepalive_loop(self):
        """Background thread body: while in PASSIVE mode and not mid-transfer,
        periodically re-send the same HELLO packet used to establish the
        mapping in the first place, purely to keep our own NAT/router's UDP
        translation from expiring during a long idle stretch between
        commands. The server doesn't need to treat these specially — it
        either hasn't started listening on this socket yet (queues up in the
        OS buffer, harmless) or is between transfers (drain_stale_packets()
        clears it before the next STOR/RETR/LIST)."""
        while not self._keepalive_stop.wait(KEEPALIVE_INTERVAL):
            if self.data_mode != DataMode.PASSIVE or self.passive_target is None or self._data_busy:
                continue
            try:
                self.udp_sock.sendto(make_packet(PKT_HELLO, 0, b"KEEPALIVE"), self.passive_target)
            except OSError:
                pass

    def _start_keepalive(self):
        if KEEPALIVE_INTERVAL <= 0 or (self._keepalive_thread and self._keepalive_thread.is_alive()):
            return
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def _stop_keepalive(self):
        self._keepalive_stop.set()
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=1)
        self._keepalive_thread = None

    def set_fixed(self):
        """Switch back to Basic Level's fixed data-channel mechanism. Sends
        FIXED so the server also reverts its session state — without this
        round trip the server would keep using whatever per-session
        ACTIVE/PASSIVE socket PORT/PASV last set up, while we listen on the
        shared FIXED port instead, and every transfer after switching back
        would silently fail (wrong source port on both ends)."""
        self._stop_keepalive()
        reply = self.command("FIXED")
        print(reply)
        if not reply.startswith("200"):
            return False
        self.data_mode = DataMode.FIXED
        self.udp_sock.sendto(make_packet(PKT_HELLO, 0, b"HELLO"), (self.host, self.data_port))
        print(f"[*] Data mode: FIXED ({self.host}:{self.data_port})")
        return True

    def set_active(self):
        """Advanced Level: tell the server our address via PORT; it opens a
        dedicated per-session socket and reports its port back (needed for
        the upload direction, since UDP has no server-initiated connect())."""
        self._stop_keepalive()
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
        self._start_keepalive()
        return True

    def login(self, username, password):
        """Send PASS and, on success, establish the data channel using
        whichever mode is currently selected (config default, or a prior
        `active`/`passive` REPL command).

        Assumes USER has already been sent for `username` (the REPL sends it
        as soon as the user types `user <name>`, before `pass` is typed).
        """
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
        reassemble it in sequence order — reliably via Go-Back-N if
        [reliability] mode=gbn, otherwise the original best-effort framing.
        Used by both get() (saved to a file) and list_dir() (printed to
        stdout) — RETR/LIST/NLST all use the same UDP framing server-side.

        Callers must drain_stale_packets() themselves *before* sending the
        RETR/LIST/NLST request (not here) — the server may start sending the
        instant it sees our request, so draining at this point risks
        discarding this transfer's own first packets on a fast/local network.

        Sets _data_busy for the duration so the PASSIVE-mode keepalive
        thread (see _keepalive_loop()) doesn't interleave a stray HELLO
        into this transfer's packet stream."""
        self._data_busy = True
        try:
            if RELIABILITY_MODE == "gbn":
                target = self._data_target()

                def _log_reack(expected_seq, got_seq):
                    print(f"[GBN] out-of-order/duplicate/corrupt from {target} "
                          f"(expected seq={expected_seq}, got={got_seq}) — re-ACKing {expected_seq - 1}")
                data = gbn_receive(self.udp_sock, target, rto=GBN_RTO,
                                    max_retries=GBN_MAX_RETRIES, on_reack=_log_reack)
                if data is None:
                    print("[!] Data transfer timed out.")
                return data
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
        finally:
            self._data_busy = False

    def put(self, local_path, remote_name=None):
        if not os.path.isfile(local_path):
            print(f"[!] Local file not found: {local_path}")
            return
        remote_name = remote_name or os.path.basename(local_path)
        with open(local_path, "rb") as f:
            data = f.read()
        try:
            wire_data = encode_ascii_transfer(data) if self.type_mode == "A" else data
        except UnicodeDecodeError:
            print("[!] TYPE A requires a 7-bit ASCII text file; use 'type I' for binary data.")
            return

        stor_cmd = f"STOR {remote_name}"
        if VERIFY_HASH:
            # Hash what the server will actually end up writing to disk (the
            # post-ASCII-round-trip bytes for TYPE A) so it matches what
            # handle_stor() hashes server-side after decode_ascii_transfer().
            stored_preview = decode_ascii_transfer(wire_data) if self.type_mode == "A" else data
            stor_cmd += f" {HASH_ALGORITHM} {compute_hash(stored_preview, HASH_ALGORITHM)}"

        reply = self.command(stor_cmd)
        print(reply)
        if not reply.startswith("150"):
            return
        target = self._data_target()
        self._data_busy = True
        try:
            if RELIABILITY_MODE == "gbn":
                def _log_retransmit(base, next_seq, retries):
                    print(f"[GBN] retransmit window seq={base}..{next_seq - 1} "
                          f"(retry #{retries}) to {target} — real packet loss detected")
                if not gbn_send(self.udp_sock, target, wire_data, window_size=GBN_WINDOW_SIZE,
                                 rto=GBN_RTO, max_retries=GBN_MAX_RETRIES, on_retransmit=_log_retransmit):
                    print("[!] Upload failed: server did not acknowledge (timed out).")
                    return
            else:
                seq = 0
                for i in range(0, len(wire_data), CHUNK_SIZE):
                    chunk = wire_data[i:i + CHUNK_SIZE]
                    self.udp_sock.sendto(make_packet(PKT_DATA, seq, chunk), target)
                    seq += 1
                self.udp_sock.sendto(make_packet(PKT_FIN, seq), target)
        finally:
            self._data_busy = False
        # The server already ran the integrity check itself when VERIFY_HASH
        # sent a hash above (deleting the file and replying 552 on mismatch)
        # — its own reply below (226 or 552) is the verdict, no separate
        # client-side round trip needed for uploads. get() still verifies
        # client-side since the client is the one persisting the download.
        print(self._read_reply())

    def get(self, remote_name, local_path=None):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        local_path = local_path or os.path.join(DOWNLOAD_DIR, remote_name)
        # Drain BEFORE asking — once the server sees RETR it may start
        # sending immediately, so draining any later risks discarding this
        # transfer's own first packets on a fast/local network.
        drain_stale_packets(self.udp_sock)
        reply = self.command(f"RETR {remote_name}")
        print(reply)
        if not reply.startswith("150"):
            return
        wire_data = self._recv_data_payload()
        if wire_data is None:
            # The server still sends a final control-channel reply
            # regardless of whether our UDP receive succeeded (unconditionally
            # in best-effort mode; 426 once its own retry budget is exhausted
            # in GBN mode) — read and discard it now, or it gets misread as
            # the reply to whatever command runs next, desyncing the session.
            print(self._read_reply())
            return
        try:
            data = decode_ascii_transfer(wire_data) if self.type_mode == "A" else wire_data
        except (UnicodeDecodeError, ValueError) as exc:
            print(f"[!] Invalid TYPE A data received: {exc}")
            print(self._read_reply())
            return
        with open(local_path, "wb") as f:
            f.write(data)
        print(f"[+] Saved to {local_path} ({len(data)} bytes)")
        print(self._read_reply())
        if VERIFY_HASH:
            self._verify_hash(remote_name, data)

    def hash_remote(self, remote_name):
        """`hash` REPL command — always works standalone regardless of the
        [integrity] verify config, unlike _verify_hash() which is only the
        automatic post-transfer check."""
        return self.command(f"HASH {remote_name}")

    def _verify_hash(self, remote_name, local_data):
        """Excellent Level: compare a locally-known hash against the
        server's HASH reply for the same file — used right after put() (we
        already have the bytes we just sent) and get() (the bytes we just
        saved)."""
        local_hash = compute_hash(local_data, HASH_ALGORITHM)
        reply = self.hash_remote(remote_name)
        m = re.match(r"213 (\S+) ([0-9a-fA-F]+)", reply)
        if not m or m.group(1).lower() != HASH_ALGORITHM:
            print(f"[!] Could not verify integrity: unexpected HASH reply {reply!r}")
            return
        if m.group(2).lower() == local_hash.lower():
            print(f"[+] Integrity verified ({HASH_ALGORITHM}): MATCH ({local_hash})")
        else:
            print(f"[!] INTEGRITY CHECK FAILED ({HASH_ALGORITHM}): "
                  f"local={local_hash} server={m.group(2)}")

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
        self._stop_keepalive()
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
            elif cmd == "hash":
                if not arg:
                    print("Usage: hash <filename>")
                    continue
                print(client.hash_remote(arg))
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
