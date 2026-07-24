# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hybrid FTP client/server built from scratch on Python's standard `socket` module only (no third-party dependencies, no frameworks), for a socket-programming assignment (`../Project1_SocketProgramming_2026.pdf`, one directory up from this repo). TCP control channel + UDP data channel. Implements **Basic Level** (USER/PASS auth, ASCII upload/download, one fixed data-connection mechanism, single-threaded) plus all four **Advanced Level** rubric items (binary transfer, directory tree, Active/Passive mode switching, multi-threaded concurrency) — see the spec's Section 1.3 for the exact tier definitions if adding more features, since Excellent Level (reliable-UDP/RDT, congestion control, hash verification) is explicitly out of scope for now.

Do not add dependencies or a build system — this is intentionally flat files (`common.py`, `config.py`, `server.py`, `client.py`) with no package manager.

## Running

```
python3 server.py                 # listens on TCP 2121 (control) + UDP 2122 (data, FIXED mode)
python3 client.py <server_host>   # interactive REPL
```

There is no test suite, linter, or build step configured. Verify changes by driving a real client/server session, e.g.:

```
python3 server.py &
printf 'user alice\npass password123\nput /tmp/hello.txt\nget hello.txt\nquit\n' | python3 client.py 127.0.0.1
```

To exercise Advanced Level paths: edit `config.ini` (`threading = thread`, `data_mode = active`/`passive`), or drive them live from the REPL (`passive`, `active`, `mkd`, `cwd`, `ls`, etc. — see README.md for the full command list). Test concurrency by backgrounding two client REPL pipelines against the same server at once; test binary integrity by diffing a transferred file against its source (`diff` after `put`+`get` round-trip), not just checking exit codes.

Credentials are hardcoded in `server.py`'s `USERS` dict (`alice/password123`, `bob/hunter2`). Server storage is `server_storage/`; downloaded files land in `client_downloads/`.

If a server restart fails with `Address already in use`, a previous instance is still bound to the ports: `lsof -nP -iTCP:2121 -iUDP:2122` then `kill <PID>`.

## Configuration (`config.py` / `config.ini`)

Both `server.py` and `client.py` load `config.ini` through `config.py` at import time (`from config import CONFIG`, a `configparser.ConfigParser`). Every key defaults to Basic Level behavior (`threading = single`, `data_mode = fixed`) — **the app must behave identically with `config.ini` deleted**; that invariant is the whole point of the config layer (the user explicitly wants alternative techniques to be opt-in, not replacements). When adding a new selectable technique (e.g. a future RDT variant for Excellent Level), add a config key with a default that preserves current behavior rather than branching on a hardcoded flag.

## Architecture

- **Control channel — TCP, port `CONTROL_PORT` (2121)**: one command/reply per line, CRLF-terminated, three-digit FTP-style reply codes. `common.recv_line`/`send_line` handle line framing; `common.Reply` centralizes every fixed-text server reply (add new replies there, not as inline strings in `server.py`).
- **Data channel — UDP**: file payload, plus (Advanced Level) directory-listing text sent through the same framing. Every datagram is `common.make_packet`/`parse_packet`-framed: a 9-byte header (`type` 1B, `seq` 4B, `crc32` 4B) followed by up to `CHUNK_SIZE` (1024) bytes of payload. Three packet types: `PKT_HELLO`, `PKT_DATA`, `PKT_FIN`.
- **Three data-channel modes, one seam**: `Session` (server) and `FTPClient` (client) both carry a `data_mode` attribute (`common.DataMode`: `FIXED`/`ACTIVE`/`PASSIVE`). The server always resolves where to send/receive through `Session.resolve_data_endpoint()` → `(socket, client_address)`; every data handler (`handle_stor`, `handle_retr`, `handle_list`, `_send_over_data_channel`) goes through that one method rather than touching `session.data_sock`/`session.client_data_addr` directly. When adding a new mode or transfer type, extend this seam — don't special-case call sites.
  - **FIXED** (Basic Level, default): client `HELLO`s the server's well-known `DATA_PORT` (2122) right after `PASS`; server learns the client's address from that one datagram and reuses the *shared, server-wide* `udp_sock` for the whole session. This is the one part of the system that is NOT per-session-isolated — see the concurrency caveat below.
  - **PASSIVE**: client sends `PASV`; `handle_pasv()` opens a fresh per-session ephemeral UDP socket, replies with `227 ... (h1,h2,h3,h4,p1,p2)` (encoded via `common.format_port_arg`/`parse_pasv_reply`), then waits for the client's `HELLO` on that new socket exactly like FIXED mode does — same learning mechanism, private port.
  - **ACTIVE**: client sends `PORT h1,h2,h3,h4,p1,p2` (its own address); `handle_port()` already knows the client's address without needing a `HELLO`, but *also* opens a per-session socket and reports its port back via `Reply.port_ok()` — a deliberate, commented deviation from RFC 959 (which doesn't need this because TCP's server-initiated `connect()` is bidirectional; our UDP data channel is connectionless, so the upload direction needs an explicit destination).
- **Concurrency**: `main()` spawns one `threading.Thread` per accepted connection when `config.ini`'s `[server] threading = thread`; `single` (default) preserves the exact old synchronous accept loop. A lock-protected global table (`_active_sessions` in `server.py`) is updated on every connect/disconnect/mode-change and printed to the server log — this satisfies the assignment's "server log displays connected client IPs... and active session table" checklist item (Section 4.5). **PASSIVE/ACTIVE sessions are safely isolated under concurrency** (dedicated per-session socket); **FIXED-mode sessions are not** (shared socket, demuxed only by best-effort source-address filtering in `handle_stor`) — this is disclosed in README.md, not silently broken. Don't "fix" FIXED-mode concurrency by making it look isolated; the honest fix is recommending Active/Passive mode for concurrent demos.
- **Directory tree**: `Session.cwd` is an FTP-space path (starts at `/`, changed by `CWD`/`CDUP`). `resolve_path(session, arg)` in `server.py` is the one place that turns an FTP-space argument into a filesystem path confined under `STORAGE_ROOT` (rejects `..` escapes) — `STOR`/`RETR`/`SIZE`/`MDTM`/`MKD`/`RMD`/`LIST`/`NLST`/`STAT` all go through it (via `safe_path()` for the file-only cases). Don't add a second path-resolution helper; extend this one.
- **No reliable-UDP layer**: packets carry a sequence number and CRC32 checksum, and out-of-order chunks are reassembled by `sorted(chunks)` before being written/sent, but there is no ACK/retransmit/sliding-window. A dropped datagram silently truncates the transfer. This is a documented, deliberate limitation (Excellent Level scope) — do not "fix" it piecemeal.
- **Session crash isolation**: `handle_client()` wraps each command in try/except so one bad session can't crash its thread/the process; `main()`'s `serve()` closure also catches per-session exceptions. Stray/malformed UDP traffic hitting a data socket must never propagate as an unhandled exception (see the `parse_packet` short-datagram bug fixed in `docs/GENAI_LOG.md` Entry 9, from before Advanced Level features existed but the same principle applies to every new socket path added since).
- **Control-channel idle timeout**: `conn.settimeout(CONTROL_IDLE_TIMEOUT)` in `handle_client()` so a client that vanishes without FIN/RST doesn't block a thread (or, in single-threaded mode, the whole server) forever; `NOOP` resets it.

## Deliberately out of scope (Excellent Level)

Do not implement these unless the user explicitly asks to move beyond Advanced Level — see `README.md` and the project spec's Section 1.3 "Excellent Level" for the full rationale:
- Custom reliable-UDP layer (ACK/timeout/retransmit — Stop-and-Wait/Go-Back-N/Selective Repeat)
- Congestion/flow control (sliding window or equivalent)
- End-to-end MD5/SHA-256 hash verification (`HASH`)
- `STOU`, `APPE`, `DELE`, `RNFR`/`RNTO`, `MODE B`/`MODE C`, `ABOR`
- Shell-style quoting in the client REPL's `put`/`get` argument parsing (plain `str.split()` is intentional; avoid paths with spaces instead of adding `shlex`)

## `docs/GENAI_LOG.md`

Required assignment appendix (project spec Section 2.4 item 6 / Section 4.3) documenting AI-assisted debugging sessions verbatim, with the student's critical analysis of what was accepted/rejected. When you (Claude Code) make a nontrivial fix, design decision, or diagnosis in this repo as part of coursework, the user may ask you to help produce a new dated entry in this same format (prompt transcript → raw AI output summary → refinement/critical-analysis notes on what was verified, kept, changed, or rejected, and why — including deliberate deviations like `Reply.port_ok()`'s non-RFC-959 behavior, which is exactly the kind of thing the oral viva will probe). Don't add entries unprompted.
