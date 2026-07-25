# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hybrid FTP client/server built from scratch on Python's standard `socket` module only (no third-party dependencies, no frameworks), for a socket-programming assignment (`../Project1_SocketProgramming_2026.pdf`, one directory up from this repo). TCP control channel + UDP data channel. Implements **Basic Level** (USER/PASS auth, ASCII upload/download, one fixed data-connection mechanism, single-threaded), all four **Advanced Level** rubric items (binary transfer, directory tree, Active/Passive mode switching, multi-threaded concurrency), and all three **Excellent Level** items (custom Go-Back-N reliable-UDP layer, sliding-window flow control, end-to-end MD5/SHA-256 integrity verification) — see the spec's Section 1.3 for the exact tier definitions if adding more features (e.g. Selective Repeat as an alternative to Go-Back-N, or adaptive/AIMD congestion control, both deliberately not implemented — see README.md).

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

To exercise Excellent Level paths: set `[reliability] mode = gbn` identically in **both** ends' `config.ini` (no negotiation handshake exists for this, unlike `data_mode`), and `[integrity] verify = true` for automatic post-transfer hash comparison. Loopback/LAN rarely drops packets on its own, so proving GBN actually recovers from loss (not just that it runs with zero drops) needs either a real lossy path or a throwaway test harness that wraps a UDP socket's `sendto()` to randomly drop packets — that's how this layer was originally verified (isolated `gbn_send`/`gbn_receive` trials at up to 50% simultaneous bidirectional loss, checked for zero false-positive successes, not just correctness on the happy path).

Credentials are hardcoded in `server.py`'s `USERS` dict (`alice/password123`, `bob/hunter2`). Server storage is `server_storage/`; downloaded files land in `client_downloads/`.

If a server restart fails with `Address already in use`, a previous instance is still bound to the ports: `lsof -nP -iTCP:2121 -iUDP:2122` then `kill <PID>`.

## Configuration (`config.py` / `config.ini`)

Both `server.py` and `client.py` load `config.ini` through `config.py` at import time (`from config import CONFIG`, a `configparser.ConfigParser`). Every key defaults to Basic Level behavior (`threading = single`, `data_mode = fixed`, `[reliability] mode = none`, `[integrity] verify = false`) — **the app must behave identically with `config.ini` deleted**; that invariant is the whole point of the config layer (the user explicitly wants alternative techniques to be opt-in, not replacements). When adding a new selectable technique (e.g. Selective Repeat as an alternative to Go-Back-N), add a config key with a default that preserves current behavior rather than branching on a hardcoded flag.

`[reliability] mode` is the one config value that has no protocol-level negotiation — unlike `data_mode` (negotiated live via `PORT`/`PASV` control-channel commands), both `server.py` and `client.py`'s `config.ini` must be set to the same value ahead of time for a transfer to work, the same way two TCP stacks must speak the same protocol version. Don't add a `MODE`-style command to negotiate it; the spec's approved command table (Section 2.2) doesn't include a slot for this, and RDT being transparent to the application layer is itself a deliberate parallel to how TCP works.

## Architecture

- **Control channel — TCP, port `CONTROL_PORT` (2121)**: one command/reply per line, CRLF-terminated, three-digit FTP-style reply codes. `common.recv_line`/`send_line` handle line framing; `common.Reply` centralizes every fixed-text server reply (add new replies there, not as inline strings in `server.py`).
- **Data channel — UDP**: file payload, plus (Advanced Level) directory-listing text sent through the same framing. Every datagram is `common.make_packet`/`parse_packet`-framed: a 9-byte header (`type` 1B, `seq` 4B, `crc32` 4B) followed by up to `CHUNK_SIZE` (1024) bytes of payload. Three packet types: `PKT_HELLO`, `PKT_DATA`, `PKT_FIN`.
- **Three data-channel modes, one seam**: `Session` (server) and `FTPClient` (client) both carry a `data_mode` attribute (`common.DataMode`: `FIXED`/`ACTIVE`/`PASSIVE`). The server always resolves where to send/receive through `Session.resolve_data_endpoint()` → `(socket, client_address)`; every data handler (`handle_stor`, `handle_retr`, `handle_list`, `_send_over_data_channel`) goes through that one method rather than touching `session.data_sock`/`session.client_data_addr` directly. When adding a new mode or transfer type, extend this seam — don't special-case call sites.
  - **FIXED** (Basic Level, default): client `HELLO`s the server's well-known `DATA_PORT` (2122) right after `PASS`; server learns the client's address from that one datagram and reuses the *shared, server-wide* `udp_sock` for the whole session. This is the one part of the system that is NOT per-session-isolated — see the concurrency caveat below.
  - **PASSIVE**: client sends `PASV`; `handle_pasv()` opens a fresh per-session ephemeral UDP socket, replies with `227 ... (h1,h2,h3,h4,p1,p2)` (encoded via `common.format_port_arg`/`parse_pasv_reply`), then waits for the client's `HELLO` on that new socket exactly like FIXED mode does — same learning mechanism, private port.
  - **ACTIVE**: client sends `PORT h1,h2,h3,h4,p1,p2` (its own address); `handle_port()` already knows the client's address without needing a `HELLO`, but *also* opens a per-session socket and reports its port back via `Reply.port_ok()` — a deliberate, commented deviation from RFC 959 (which doesn't need this because TCP's server-initiated `connect()` is bidirectional; our UDP data channel is connectionless, so the upload direction needs an explicit destination).
- **Concurrency**: `main()` spawns one `threading.Thread` per accepted connection when `config.ini`'s `[server] threading = thread`; `single` (default) preserves the exact old synchronous accept loop. A lock-protected global table (`_active_sessions` in `server.py`) is updated on every connect/disconnect/mode-change and printed to the server log — this satisfies the assignment's "server log displays connected client IPs... and active session table" checklist item (Section 4.5). **PASSIVE/ACTIVE sessions are safely isolated under concurrency** (dedicated per-session socket); **FIXED-mode sessions are not** (shared socket, demuxed only by best-effort source-address filtering in `handle_stor`) — this is disclosed in README.md, not silently broken. Don't "fix" FIXED-mode concurrency by making it look isolated; the honest fix is recommending Active/Passive mode for concurrent demos.
- **Directory tree**: `Session.cwd` is an FTP-space path (starts at `/`, changed by `CWD`/`CDUP`). `resolve_path(session, arg)` in `server.py` is the one place that turns an FTP-space argument into a filesystem path confined under `STORAGE_ROOT` (rejects `..` escapes) — `STOR`/`RETR`/`SIZE`/`MDTM`/`MKD`/`RMD`/`LIST`/`NLST`/`STAT` all go through it (via `safe_path()` for the file-only cases). Don't add a second path-resolution helper; extend this one.
- **Best-effort UDP (default, `[reliability] mode = none`)**: packets carry a sequence number and CRC32 checksum, and out-of-order chunks are reassembled by `sorted(chunks)` before being written/sent, but there is no ACK/retransmit/sliding-window. A dropped datagram silently truncates the transfer. This is a documented, deliberate Basic/Advanced Level behavior — do not "fix" it piecemeal; that's what the opt-in GBN layer below is for.
- **Go-Back-N reliable-UDP layer (`mode = gbn`)**: `common.gbn_send()`/`gbn_receive()` are the *only* implementation — both `server.py` (STOR/RETR/LIST/NLST via `_send_over_data_channel`/`handle_stor`) and `client.py` (`put`/`get`/`list_dir` via `_recv_data_payload`) call these exact functions rather than each having their own copy, so the two ends can't drift out of sync. Sliding window (`window_size`, the flow-control knob) + cumulative ACK (`PKT_ACK`) + a single retransmit timer for the oldest unacked packet, which on timeout retransmits the **whole** in-flight window (the defining trait vs. Selective Repeat, which was considered and rejected — see README.md). `PKT_FIN` is packet number *N*, folded into the same reliable stream, so "done" is itself acknowledged. `max_retries` consecutive timeouts → honest failure (`426`/`None`), never a false "success" on incomplete data — this was the property actually verified (isolated `gbn_send`/`gbn_receive` trials with a `sendto()`-dropping test harness at up to 50% simulated loss), not just the happy path. When touching this code, preserve that no-false-positive guarantee above all else.
- **Data integrity (`HASH`, `common.compute_hash`)**: `handle_hash()` in `server.py` and `FTPClient.hash_remote()`/`_verify_hash()` in `client.py` both go through the one shared `compute_hash(data, algorithm)` helper in `common.py` rather than each calling `hashlib` directly, so SHA-256/MD5 selection (`[integrity] algorithm`) stays consistent. `_verify_hash()` is only the automatic post-`put`/`get` check gated by `[integrity] verify`; `hash <filename>` in the REPL always works standalone regardless of that setting.
- **Session crash isolation**: `handle_client()` wraps each command in try/except so one bad session can't crash its thread/the process; `main()`'s `serve()` closure also catches per-session exceptions. Stray/malformed UDP traffic hitting a data socket must never propagate as an unhandled exception (see the `parse_packet` short-datagram bug fixed in `docs/GENAI_LOG.md` Entry 9, from before Advanced Level features existed but the same principle applies to every new socket path added since).
- **Control-channel idle timeout**: `conn.settimeout(CONTROL_IDLE_TIMEOUT)` in `handle_client()` so a client that vanishes without FIN/RST doesn't block a thread (or, in single-threaded mode, the whole server) forever; `NOOP` resets it.

## Deliberately out of scope

Do not implement these unless the user explicitly asks — see `README.md`'s "Deliberately not implemented" section for the full rationale:
- **Selective Repeat** as an alternative/addition to Go-Back-N — more bandwidth-efficient under loss, but needs receiver-side out-of-order buffering and per-packet ACK tracking; GBN was the deliberate choice for one coherent mechanism covering both the RDT and sliding-window rubric items.
- **Adaptive/dynamic congestion control** (TCP-Reno-style slow start, AIMD, timeout-driven window resizing) — `[reliability] window_size` is a fixed, configurable knob, which is what the spec's "Sliding Window **or equivalent**" wording allows; don't add auto-tuning without being asked.
- A negotiation command for `[reliability] mode` — no slot exists for it in the spec's approved command table (Section 2.2), and RDT being config-only/transparent to the FTP command layer is a deliberate parallel to TCP.
- `STOU`, `APPE`, `DELE`, `RNFR`/`RNTO`, `MODE B`/`MODE C`, `ABOR`
- Shell-style quoting in the client REPL's `put`/`get` argument parsing (plain `str.split()` is intentional; avoid paths with spaces instead of adding `shlex`)

## `docs/GENAI_LOG.md`

Required assignment appendix (project spec Section 2.4 item 6 / Section 4.3) documenting AI-assisted debugging sessions verbatim, with the student's critical analysis of what was accepted/rejected. When you (Claude Code) make a nontrivial fix, design decision, or diagnosis in this repo as part of coursework, the user may ask you to help produce a new dated entry in this same format (prompt transcript → raw AI output summary → refinement/critical-analysis notes on what was verified, kept, changed, or rejected, and why — including deliberate deviations like `Reply.port_ok()`'s non-RFC-959 behavior, which is exactly the kind of thing the oral viva will probe). Don't add entries unprompted.
