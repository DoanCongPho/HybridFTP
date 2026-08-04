"""Hybrid FTP server — Basic + Advanced Level.

Control channel: TCP, fixed port (see common.CONTROL_PORT).
Data channel:    UDP, fixed port (see common.DATA_PORT) — Basic Level's one
                  fixed data-channel connection mechanism.

Basic Level (always on): USER/PASS auth, ASCII upload/download of a single
file, one fixed data-channel mechanism, single-threaded by default.

Advanced Level (opt-in): binary transfer (TYPE I) without corrupting the
bytes that TYPE A's ascii_mask() would otherwise alter, and a real
directory tree (CWD/CDUP/MKD/RMD/LIST/NLST/STAT/MDTM).
"""

import functools
import os
import posixpath
import socket
import time

print = functools.partial(print, flush=True)  # keep server log visible even when output is redirected

from common import (
    CONTROL_PORT, DATA_PORT, CHUNK_SIZE, SOCK_TIMEOUT, CONTROL_IDLE_TIMEOUT, DataMode, Reply,
    PKT_HELLO, PKT_DATA, PKT_FIN,
    make_packet, parse_packet, recv_line, send_line, drain_stale_packets, ascii_mask,
)
from config import CONFIG

_storage_root_cfg = CONFIG.get("server", "storage_root", fallback="server_storage")
if os.path.isabs(_storage_root_cfg):
    STORAGE_ROOT = _storage_root_cfg
else:
    STORAGE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), _storage_root_cfg)

USERS = {
    "alice": "password123",
    "bob": "hunter2",
}


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
    def __init__(self, conn, addr, fixed_udp_sock):
        self.conn = conn
        self.addr = addr
        self.username = None
        self.authenticated = False
        self.type_mode = "A"
        self.cwd = "/"

        # Data-channel state. FIXED mode (Basic Level default) reuses the
        # one shared, server-wide UDP socket bound at DATA_PORT — see
        # resolve_data_endpoint() below.
        self.data_mode = DataMode.FIXED
        self.data_sock = fixed_udp_sock
        self.client_data_addr = None      # learned from the client's HELLO datagram

    def resolve_data_endpoint(self):
        """Single seam for finding out where/how to send or receive file
        data for this session: (socket_to_use, client_address)."""
        return self.data_sock, self.client_data_addr


def _send_over_data_channel(session, data):
    """Send `data` to the session's registered client address. Used by RETR
    and by LIST/NLST, which stream their listing text over the same UDP data
    channel rather than the control channel."""
    sock, endpoint = session.resolve_data_endpoint()
    if endpoint is None:
        return False
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
    # client could start firing data the instant it sees "150".
    drain_stale_packets(sock)
    send_line(session.conn, Reply.FILE_STATUS_OK)

    chunks = {}
    sock.settimeout(SOCK_TIMEOUT)
    try:
        while True:
            raw, addr = sock.recvfrom(CHUNK_SIZE + 64)
            if addr != expected_addr:
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


def cleanup_session(session):
    """Always run when a session ends — clean QUIT, abrupt disconnect, or an
    unhandled error — so sockets are closed and state doesn't linger."""
    try:
        session.conn.close()
    except OSError:
        pass
    print(f"[*] Session for {session.addr} (user={session.username!r}) closed.")


def handle_client(conn, addr, fixed_udp_sock):
    session = Session(conn, addr, fixed_udp_sock)
    print(f"[+] Connection from {addr}")
    # Idle timeout on the control channel: if a client dies without ever
    # sending FIN/RST (power loss, network drop, VM freeze — not just a
    # killed process, which the OS still closes cleanly), recv() would
    # otherwise block here forever. NOOP exists to renew this timer.
    conn.settimeout(CONTROL_IDLE_TIMEOUT)
    try:
        send_line(conn, Reply.SERVICE_READY)
        while True:
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
                        # HELLO datagram so we learn its UDP address. This is
                        # what makes FIXED a genuine "connection" instead of
                        # just a well-known port nobody ever addresses.
                        session.data_sock.settimeout(SOCK_TIMEOUT)
                        try:
                            raw, caddr = session.data_sock.recvfrom(1024)
                            pkt_type, _, _, valid = parse_packet(raw)
                            if valid and pkt_type == PKT_HELLO:
                                session.client_data_addr = caddr
                        except socket.timeout:
                            pass
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

                elif cmd == "SIZE":
                    fs_path = safe_path(session, arg) if arg else None
                    if fs_path and os.path.isfile(fs_path):
                        send_line(conn, Reply.size(os.path.getsize(fs_path)))
                    else:
                        send_line(conn, Reply.FILE_UNAVAILABLE)

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

                elif cmd == "HELP":
                    send_line(conn, Reply.HELP_TEXT)

                elif cmd == "QUIT":
                    send_line(conn, Reply.GOODBYE)
                    break

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
    tcp_sock.listen(1)
    print(f"[*] Hybrid FTP server listening on TCP {CONTROL_PORT}, UDP data port {DATA_PORT}")
    print(f"[*] Storage root: {STORAGE_ROOT}")

    try:
        while True:
            conn, addr = tcp_sock.accept()
            handle_client(conn, addr, udp_sock)   # single-threaded: one client at a time (Basic Level default)
    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        tcp_sock.close()
        udp_sock.close()


if __name__ == "__main__":
    main()
