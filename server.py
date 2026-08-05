"""Hybrid FTP server — Basic + Advanced Level.

Control channel: TCP, fixed port (see common.CONTROL_PORT).
Data channel:    UDP, fixed port (see common.DATA_PORT) for FIXED mode, or a
                  per-session ephemeral UDP socket for ACTIVE/PASSIVE mode.

Basic Level (always on): USER/PASS auth, ASCII upload/download of a single
file, one fixed data-channel mechanism, single-threaded by default.

Advanced Level (opt-in via config.ini, see config.py): binary transfer
(TYPE I), a real directory tree (CWD/CDUP/MKD/RMD/LIST/NLST/STAT/MDTM),
Active/Passive data-mode switching (PORT/PASV), and a multi-threaded server
with an active-session table. See README.md for the full command list and
what is still deliberately not implemented (Excellent Level scope).
"""

import functools
import itertools
import os
import posixpath
import socket
import threading
import time

print = functools.partial(print, flush=True)  # keep server log visible even when output is redirected

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, CONTROL_IDLE_TIMEOUT, DataMode, Reply,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, parse_port_arg, recv_line, send_line,
    gbn_send, gbn_receive, compute_hash, drain_stale_packets, ascii_mask,
)
from config import CONFIG

_storage_root_cfg = CONFIG.get("server", "storage_root", fallback="server_storage")
if os.path.isabs(_storage_root_cfg):
    STORAGE_ROOT = _storage_root_cfg
else:
    STORAGE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), _storage_root_cfg)

THREADING_MODE = CONFIG.get("server", "threading", fallback="single")   # single | thread
ADVERTISE_IP = CONFIG.get("server", "advertise_ip", fallback="").strip()

# Port range for per-session ACTIVE/PASSIVE UDP sockets. Blank (the default)
# means "let the OS pick any free ephemeral port" — fine on a LAN/loopback,
# but behind a cloud NSG/firewall that only opens a couple of fixed ports,
# a random port gets silently dropped before it reaches this process. Setting
# both bounds restricts binding to that range so only it needs to be opened
# on the firewall once (mirrors vsftpd's pasv_min_port/pasv_max_port).
_pasv_min_raw = CONFIG.get("server", "passive_port_min", fallback="").strip()
_pasv_max_raw = CONFIG.get("server", "passive_port_max", fallback="").strip()
PASSIVE_PORT_RANGE = (
    (int(_pasv_min_raw), int(_pasv_max_raw)) if _pasv_min_raw and _pasv_max_raw else None
)

# Excellent Level: Go-Back-N reliable-UDP layer. "none" (default) preserves
# the exact best-effort framing above; both server and client must agree on
# this, so it's read from the same [reliability] section by both files.
RELIABILITY_MODE = CONFIG.get("reliability", "mode", fallback="none").strip().lower()
GBN_WINDOW_SIZE = CONFIG.getint("reliability", "window_size", fallback=4)
GBN_RTO = CONFIG.getint("reliability", "rto_ms", fallback=300) / 1000.0
GBN_MAX_RETRIES = CONFIG.getint("reliability", "max_retries", fallback=30)

HASH_ALGORITHM = CONFIG.get("integrity", "algorithm", fallback="sha256").strip().lower()
if HASH_ALGORITHM not in ("sha256", "md5"):
    HASH_ALGORITHM = "sha256"

USERS = {
    "alice": "password123",
    "bob": "hunter2",
}

# Commands defined by the spec but still not implemented (Excellent Level
# scope: append/rename/unique-store features not needed to demonstrate RDT,
# congestion control, or integrity verification). Listed explicitly so the
# dispatcher has a named slot for each rather than falling through to a
# generic "unknown command".
NOT_IMPLEMENTED = {"STOU", "APPE", "DELE", "RNFR", "RNTO", "ABOR"}

_session_id_lock = threading.Lock()
_session_id_counter = itertools.count(1)

_session_table_lock = threading.Lock()
_active_sessions = {}


def _next_session_id():
    with _session_id_lock:
        return next(_session_id_counter)


def _register_session(session):
    with _session_table_lock:
        _active_sessions[session.id] = session
    _print_session_table()


def _unregister_session(session):
    with _session_table_lock:
        _active_sessions.pop(session.id, None)
    _print_session_table()


def _print_session_table():
    with _session_table_lock:
        rows = sorted(_active_sessions.values(), key=lambda s: s.id)
    print("[*] Active sessions:")
    if not rows:
        print("      (none)")
    for s in rows:
        print(f"      #{s.id}  {s.addr[0]}:{s.addr[1]}  user={s.username!r}  "
              f"mode={s.data_mode}  cwd={s.cwd}")


def resolve_path(session, arg):
    """Resolve an FTP-space path (relative to session.cwd, or absolute if it
    starts with '/') to a filesystem path confined under STORAGE_ROOT.

    Returns (ftp_path, fs_path), or (None, None) if arg would escape
    STORAGE_ROOT (e.g. via '..').
    """
    raw = arg if arg else session.cwd
    ftp_path = raw if raw.startswith("/") else posixpath.join(session.cwd, raw)
    ftp_path = posixpath.normpath(ftp_path)
    if not ftp_path.startswith("/"):
        ftp_path = "/" + ftp_path
    fs_path = os.path.normpath(os.path.join(STORAGE_ROOT, ftp_path.lstrip("/")))
    root = os.path.normpath(STORAGE_ROOT)
    if fs_path != root and not fs_path.startswith(root + os.sep):
        return None, None
    return ftp_path, fs_path


def safe_path(session, filename):
    """Filesystem path for a STOR/RETR/SIZE filename, resolved against the
    session's current directory and confined to STORAGE_ROOT. None if the
    name would escape STORAGE_ROOT."""
    _, fs_path = resolve_path(session, filename)
    return fs_path


class Session:
    def __init__(self, session_id, conn, addr, fixed_udp_sock):
        self.id = session_id
        self.conn = conn
        self.addr = addr
        self.username = None
        self.authenticated = False
        self.type_mode = "A"
        self.cwd = "/"

        # Data-channel state. FIXED mode (Basic Level default) reuses the
        # one shared, server-wide UDP socket bound at DATA_PORT; ACTIVE and
        # PASSIVE (Advanced Level) each get a dedicated per-session ephemeral
        # socket so concurrent sessions don't collide — see
        # resolve_data_endpoint() and handle_port()/handle_pasv() below.
        self.data_mode = DataMode.FIXED
        self.data_sock = fixed_udp_sock
        self.owns_data_sock = False
        self.client_data_addr = None      # learned from the client's HELLO datagram, or from PORT

    def resolve_data_endpoint(self):
        """Single seam for finding out where/how to send or receive file
        data for this session: (socket_to_use, client_address)."""
        return self.data_sock, self.client_data_addr

    def close_data_socket(self):
        if self.owns_data_sock and self.data_sock is not None:
            try:
                self.data_sock.close()
            except OSError:
                pass


def _send_over_data_channel(session, data):
    """Send `data` to the session's registered client address — reliably via
    Go-Back-N if [reliability] mode=gbn, otherwise the original best-effort
    framing (checksummed/sequenced, no ACK/retransmit). Used by RETR and by
    LIST/NLST, which stream their listing text over the same UDP data
    channel rather than the control channel."""
    sock, endpoint = session.resolve_data_endpoint()
    if endpoint is None:
        return False
    if RELIABILITY_MODE == "gbn":
        def _log_retransmit(base, next_seq, retries):
            print(f"[GBN] retransmit window seq={base}..{next_seq - 1} "
                  f"(retry #{retries}) to {endpoint} — real packet loss detected")
        return gbn_send(sock, endpoint, data, window_size=GBN_WINDOW_SIZE,
                         rto=GBN_RTO, max_retries=GBN_MAX_RETRIES, on_retransmit=_log_retransmit)
    seq = 0
    for i in range(0, len(data), CHUNK_SIZE):
        chunk = data[i:i + CHUNK_SIZE]
        sock.sendto(make_packet(PKT_DATA, seq, chunk), endpoint)
        seq += 1
    sock.sendto(make_packet(PKT_FIN, seq), endpoint)
    return True


def handle_stor(session, filename):
    if not filename:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    fs_path = safe_path(session, filename)
    if fs_path is None:
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    sock, expected_addr = session.resolve_data_endpoint()
    if expected_addr is None:
        send_line(session.conn, Reply.CANT_OPEN_DATA_CONN)
        return
    # Drain BEFORE telling the client to start sending (FILE_STATUS_OK) — the
    # client could start firing data the instant it sees "150", so draining
    # any later point risks discarding this transfer's own first packets on
    # a fast/local network. See common.gbn_receive()'s docstring.
    drain_stale_packets(sock)
    send_line(session.conn, Reply.FILE_STATUS_OK)

    if RELIABILITY_MODE == "gbn":
        def _log_reack(expected_seq, got_seq):
            print(f"[GBN] out-of-order/duplicate/corrupt from {expected_addr} "
                  f"(expected seq={expected_seq}, got={got_seq}) — re-ACKing {expected_seq - 1}")
        data = gbn_receive(sock, expected_addr, rto=GBN_RTO, max_retries=GBN_MAX_RETRIES,
                            on_reack=_log_reack)
        if data is None:
            send_line(session.conn, Reply.TRANSFER_ABORTED)
            return
    else:
        chunks = {}
        sock.settimeout(SOCK_TIMEOUT)
        try:
            while True:
                raw, addr = sock.recvfrom(CHUNK_SIZE + 64)
                if addr != expected_addr:
                    # Stray traffic (scan noise, or — under concurrency —
                    # another session sharing this socket in FIXED mode);
                    # not this transfer's data, ignore and keep waiting.
                    continue
                pkt_type, seq, payload, valid = parse_packet(raw)
                if not valid:
                    print(f"[!] Corrupt packet seq={seq} dropped.")
                    continue
                if pkt_type == PKT_FIN:
                    break
                if pkt_type == PKT_DATA:
                    chunks[seq] = payload
        except socket.timeout:
            send_line(session.conn, Reply.TRANSFER_ABORTED)
            return
        data = b"".join(chunks[seq] for seq in sorted(chunks))

    with open(fs_path, "wb") as f:
        f.write(data)
    print(f"[+] Stored '{filename}' ({len(data)} bytes) from user '{session.username}'")
    send_line(session.conn, Reply.TRANSFER_COMPLETE)


def handle_retr(session, filename):
    if not filename:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    fs_path = safe_path(session, filename)
    if fs_path is None or not os.path.isfile(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    _, endpoint = session.resolve_data_endpoint()
    if endpoint is None:
        send_line(session.conn, Reply.CANT_OPEN_DATA_CONN)
        return
    send_line(session.conn, Reply.FILE_STATUS_OK)
    with open(fs_path, "rb") as f:
        data = f.read()
    # TYPE A only affects what goes on the wire, never the file on disk —
    # mask a throwaway copy, keep `data` (used below for the log line) as
    # the true byte count of the stored file.
    wire_data = ascii_mask(data) if session.type_mode == "A" else data
    if not _send_over_data_channel(session, wire_data):
        send_line(session.conn, Reply.TRANSFER_ABORTED)
        return
    print(f"[+] Sent '{filename}' ({len(data)} bytes) to user '{session.username}'")
    send_line(session.conn, Reply.TRANSFER_COMPLETE)


def handle_cwd(session, arg):
    if not arg:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    ftp_path, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.isdir(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    session.cwd = ftp_path
    send_line(session.conn, Reply.cwd_ok(ftp_path))


def handle_mkd(session, arg):
    if not arg:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    ftp_path, fs_path = resolve_path(session, arg)
    if fs_path is None:
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    try:
        os.mkdir(fs_path)
    except OSError:
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    send_line(session.conn, Reply.dir_created(ftp_path))


def handle_rmd(session, arg):
    if not arg:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    ftp_path, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.isdir(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    try:
        os.rmdir(fs_path)
    except OSError:
        send_line(session.conn, Reply.FILE_UNAVAILABLE)  # e.g. not empty
        return
    send_line(session.conn, Reply.COMMAND_OK)


def handle_list(session, arg, name_only):
    ftp_path, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.isdir(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    _, endpoint = session.resolve_data_endpoint()
    if endpoint is None:
        send_line(session.conn, Reply.CANT_OPEN_DATA_CONN)
        return
    entries = sorted(os.listdir(fs_path))
    lines = []
    for name in entries:
        if name_only:
            lines.append(name)
            continue
        full = os.path.join(fs_path, name)
        is_dir = os.path.isdir(full)
        kind = "d" if is_dir else "-"
        perms = "rwxr-xr-x" if is_dir else "rw-r--r--"
        size = 0 if is_dir else os.path.getsize(full)
        lines.append(f"{kind}{perms} {size:>10} {name}")
    body = ("\n".join(lines) + "\n" if lines else "").encode("utf-8")
    send_line(session.conn, Reply.FILE_STATUS_OK)
    if not _send_over_data_channel(session, body):
        send_line(session.conn, Reply.TRANSFER_ABORTED)
        return
    send_line(session.conn, Reply.TRANSFER_COMPLETE)


def handle_stat(session, arg):
    if not arg:
        send_line(session.conn, Reply.status(session))
        return
    _, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.exists(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    st = os.stat(fs_path)
    kind = "directory" if os.path.isdir(fs_path) else "file"
    send_line(session.conn, f"213 {kind} {st.st_size} bytes")


def handle_mdtm(session, arg):
    if not arg:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    _, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.isfile(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(os.path.getmtime(fs_path)))
    send_line(session.conn, f"213 {ts}")


def handle_hash(session, arg):
    """Excellent Level: end-to-end integrity verification."""
    if not arg:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    _, fs_path = resolve_path(session, arg)
    if fs_path is None or not os.path.isfile(fs_path):
        send_line(session.conn, Reply.FILE_UNAVAILABLE)
        return
    with open(fs_path, "rb") as f:
        data = f.read()
    send_line(session.conn, f"213 {HASH_ALGORITHM} {compute_hash(data, HASH_ALGORITHM)}")


def _bind_session_data_socket(session):
    """Open a fresh UDP socket for a per-session (ACTIVE/PASSIVE) data
    channel, bound either to an OS-assigned ephemeral port (default) or to
    a free port within PASSIVE_PORT_RANGE if configured. Returns None if
    every port in a configured range is already taken."""
    ip = session.conn.getsockname()[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if PASSIVE_PORT_RANGE is None:
        sock.bind((ip, 0))
        return sock
    lo, hi = PASSIVE_PORT_RANGE
    for port in range(lo, hi + 1):
        try:
            sock.bind((ip, port))
            return sock
        except OSError:
            continue  # port already in use by another concurrent session, try the next one
    sock.close()
    return None


def handle_pasv(session):
    """PASV: open a fresh per-session UDP socket, tell the client where it
    lives, then wait for the client's HELLO on it to learn the client's
    address — the same handshake FIXED mode uses, just on a private port
    instead of the shared DATA_PORT, which is what gives concurrent sessions
    isolated data channels."""
    session.close_data_socket()
    sock = _bind_session_data_socket(session)
    if sock is None:
        send_line(session.conn, Reply.CANT_OPEN_DATA_CONN)
        return
    session.data_sock = sock
    session.owns_data_sock = True
    session.data_mode = DataMode.PASSIVE
    session.client_data_addr = None

    ip = ADVERTISE_IP or sock.getsockname()[0]
    port = sock.getsockname()[1]
    send_line(session.conn, Reply.pasv(ip, port))

    sock.settimeout(SOCK_TIMEOUT)
    try:
        raw, caddr = sock.recvfrom(1024)
        pkt_type, _, _, valid = parse_packet(raw)
        if valid and pkt_type == PKT_HELLO:
            session.client_data_addr = caddr
    except socket.timeout:
        pass


def handle_port(session, arg):
    """PORT: the client already told us its address, so no HELLO wait is
    needed — but our UDP data channel is connectionless, so (unlike real
    RFC 959 active mode) we still open our own dedicated per-session socket
    and report its port back to the client for the upload (STOR) direction;
    see Reply.port_ok()."""
    parsed = parse_port_arg(arg)
    if parsed is None:
        send_line(session.conn, Reply.SYNTAX_ERROR_PARAMS)
        return
    session.close_data_socket()
    sock = _bind_session_data_socket(session)
    if sock is None:
        send_line(session.conn, Reply.CANT_OPEN_DATA_CONN)
        return
    session.data_sock = sock
    session.owns_data_sock = True
    session.data_mode = DataMode.ACTIVE
    session.client_data_addr = parsed
    send_line(session.conn, Reply.port_ok(sock.getsockname()[1]))


def cleanup_session(session):
    """Always run when a session ends — clean QUIT, abrupt disconnect, or an
    unhandled error — so sockets are closed and state doesn't linger.

    Guaranteed via try/finally in handle_client(), so a client that
    disconnects without sending QUIT (crash, Ctrl+C, lost network) is torn
    down exactly the same way as one that logs out properly.
    """
    session.close_data_socket()
    try:
        session.conn.close()
    except OSError:
        pass
    _unregister_session(session)
    print(f"[*] Session #{session.id} for {session.addr} (user={session.username!r}) closed.")


def handle_client(conn, addr, fixed_udp_sock):
    session = Session(_next_session_id(), conn, addr, fixed_udp_sock)
    _register_session(session)
    print(f"[+] Connection #{session.id} from {addr}")
    # Idle timeout on the control channel: if a client dies without ever
    # sending FIN/RST (power loss, network drop, VM freeze — not just a
    # killed process, which the OS still closes cleanly), recv() would
    # otherwise block here forever. In single-threaded mode that would
    # freeze the server for every other client; in threaded mode it would
    # merely leak one thread — either way NOOP exists to renew this timer.
    conn.settimeout(CONTROL_IDLE_TIMEOUT)
    try:
        send_line(conn, Reply.SERVICE_READY)
        while True:
            # Every read AND every reply for this command lives inside one
            # try/except: if the client vanishes mid-command (process
            # killed, network drop) instead of sending QUIT, recv/send will
            # raise a connection-level error here. We treat that exactly
            # like a normal disconnect — log it and fall through to
            # cleanup_session() in the outer finally — rather than letting
            # it propagate as an unhandled crash.
            try:
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
                        send_line(conn, Reply.SYNTAX_ERROR_PARAMS)
                        continue
                    session.username = arg
                    session.authenticated = False
                    send_line(conn, Reply.USER_OK_NEED_PASS)

                elif cmd == "PASS":
                    if session.username and USERS.get(session.username) == arg:
                        session.authenticated = True
                        send_line(conn, Reply.LOGIN_SUCCESS)
                        # Fixed data-channel handshake: wait for the client's
                        # HELLO datagram so we learn its UDP address. Only
                        # attempted if the client is actually staying on
                        # FIXED mode (the default) — a client configured for
                        # ACTIVE/PASSIVE never sends this HELLO and will
                        # instead PORT/PASV right after PASS, so blocking
                        # here for SOCK_TIMEOUT would just stall reading that
                        # next command for no reason.
                        if session.data_mode == DataMode.FIXED:
                            session.data_sock.settimeout(SOCK_TIMEOUT)
                            try:
                                raw, caddr = session.data_sock.recvfrom(1024)
                                pkt_type, _, _, valid = parse_packet(raw)
                                if valid and pkt_type == PKT_HELLO:
                                    session.client_data_addr = caddr
                            except socket.timeout:
                                pass
                        _print_session_table()
                    else:
                        send_line(conn, Reply.NOT_LOGGED_IN)

                elif cmd == "NOOP":
                    send_line(conn, Reply.COMMAND_OK)

                elif cmd == "PWD":
                    send_line(conn, Reply.pwd(session.cwd))

                elif cmd == "CWD":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_cwd(session, arg)

                elif cmd == "CDUP":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_cwd(session, "..")

                elif cmd == "MKD":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_mkd(session, arg)

                elif cmd == "RMD":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_rmd(session, arg)

                elif cmd == "LIST":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_list(session, arg, name_only=False)

                elif cmd == "NLST":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_list(session, arg, name_only=True)

                elif cmd == "STAT":
                    handle_stat(session, arg)

                elif cmd == "MDTM":
                    handle_mdtm(session, arg)

                elif cmd == "HASH":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_hash(session, arg)

                elif cmd == "TYPE":
                    mode = arg.upper()
                    if mode in ("A", "I"):
                        session.type_mode = mode
                        send_line(conn, Reply.COMMAND_OK)
                    else:
                        send_line(conn, Reply.TYPE_NOT_IMPLEMENTED)

                elif cmd == "MODE":
                    if arg.upper() == "S":
                        send_line(conn, Reply.COMMAND_OK)
                    else:
                        send_line(conn, Reply.MODE_NOT_IMPLEMENTED)

                elif cmd == "SIZE":
                    fs_path = safe_path(session, arg) if arg else None
                    if fs_path and os.path.isfile(fs_path):
                        send_line(conn, Reply.size(os.path.getsize(fs_path)))
                    else:
                        send_line(conn, Reply.FILE_UNAVAILABLE)

                elif cmd == "HELP":
                    send_line(conn, Reply.HELP_TEXT)

                elif cmd == "PORT":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_port(session, arg)
                        _print_session_table()

                elif cmd == "PASV":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_pasv(session)
                        _print_session_table()

                elif cmd == "STOR":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_stor(session, arg)

                elif cmd == "RETR":
                    if not session.authenticated:
                        send_line(conn, Reply.NOT_LOGGED_IN)
                    else:
                        handle_retr(session, arg)

                elif cmd == "QUIT":
                    send_line(conn, Reply.GOODBYE)
                    break

                elif cmd in NOT_IMPLEMENTED:
                    send_line(conn, Reply.NOT_IMPLEMENTED)

                else:
                    send_line(conn, Reply.SYNTAX_ERROR_CMD)

            except socket.timeout:
                print(f"[-] {addr} idle for {CONTROL_IDLE_TIMEOUT:.0f}s with no NOOP/command, closing session.")
                break
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                print(f"[-] {addr} disconnected abruptly: {e!r}")
                break
    finally:
        cleanup_session(session)


def main():
    host = CONFIG.get("server", "host", fallback="0.0.0.0")
    os.makedirs(STORAGE_ROOT, exist_ok=True)

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind((host, DATA_PORT))

    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind((host, CONTROL_PORT))
    tcp_sock.listen(5 if THREADING_MODE == "thread" else 1)
    print(f"[*] Hybrid FTP server listening on TCP {CONTROL_PORT}, UDP data port {DATA_PORT}")
    print(f"[*] Storage root: {STORAGE_ROOT}")
    print(f"[*] Concurrency mode: {THREADING_MODE}")

    def serve(conn, addr):
        try:
            handle_client(conn, addr, udp_sock)
        except Exception as e:
            # A single bad session (malformed input, unexpected client
            # behavior, etc.) must not take the whole server down.
            print(f"[!] Session with {addr} crashed: {e!r}")

    try:
        while True:
            conn, addr = tcp_sock.accept()
            if THREADING_MODE == "thread":
                threading.Thread(target=serve, args=(conn, addr), daemon=True).start()
            else:
                serve(conn, addr)   # single-threaded: one client at a time (Basic Level default)
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        tcp_sock.close()
        udp_sock.close()


if __name__ == "__main__":
    main()
