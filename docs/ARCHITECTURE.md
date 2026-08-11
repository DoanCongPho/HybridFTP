# Hybrid FTP — Technical Reference

Full protocol/design reference and command list. See the top-level `README.md` for the quick
pitch, setup, and how to connect to the live demo server.

## Client commands

**Baseline** — always available, no config needed:
```
user <name>
pass <password>
put <local_file> [remote_name]     upload (STOR)
get <remote_name> [local_file]     download (RETR, saved to client_downloads/)
pwd
size <filename>
noop
help
quit
```

**Directory tree, binary transfer, Active/Passive mode:**
```
cwd <path>                         change server directory
cdup                               go to parent directory
mkd <dirname>                      create a directory
rmd <dirname>                      remove an (empty) directory
ls / list [path]                   detailed directory listing
nlst [path]                        name-only directory listing
stat [path]                        server/session or path status
mdtm <filename>                    last-modified timestamp
type {A|I}                         ASCII (default) or binary transfer type
active                             switch to Active mode (PORT)
passive                            switch to Passive mode (PASV)
fixed                              switch back to Basic Level fixed mode
```

**Reliable-UDP transfer and integrity verification:**
```
hash <filename>                    server-side MD5/SHA-256 of a file
```
(`put`/`get` also run this automatically and print MATCH/MISMATCH when
`config.ini`'s `[integrity] verify = true`.)

### Example session
```
ftp> user alice
331 Username OK, need password.
ftp> pass password123
230 Login successful.
ftp> put hello.txt
150 File status okay, opening data connection.
226 Transfer complete.
ftp> get hello.txt
150 File status okay, opening data connection.
[+] Saved to client_downloads/hello.txt (13 bytes)
226 Transfer complete.
ftp> quit
221 Goodbye.
```

Credentials are hardcoded in `server.py` (`USERS` dict): `alice/password123`,
`bob/hunter2`.

## Configuration

Both `server.py` and `client.py` read `config.ini` at startup (via
`config.py`). Every key is optional and defaults to the minimal baseline, so
deleting `config.ini` (or any key in it) reproduces that baseline exactly —
config only lets you *opt into* every technique below without touching code:

```ini
[server]
threading = single      ; single | thread — server concurrency model
storage_root = server_storage
advertise_ip =          ; PASV-announced IP override, for NAT/cloud VMs

[client]
data_mode = fixed        ; fixed | active | passive — data-channel default
download_dir = client_downloads

[reliability]
mode = none               ; none | gbn — MUST match between server and client
window_size = 4           ; GBN sliding-window size (flow-control knob)
rto_ms = 300               ; GBN per-packet retransmit timeout
max_retries = 30           ; GBN consecutive-timeout cap before giving up

[integrity]
verify = false             ; auto MD5/SHA-256 compare after every put/get
algorithm = sha256          ; sha256 | md5

[tls]
enable = false              ; wrap the TCP control channel in TLS — MUST match between server and client
certfile = certs&keys/cert.pem
keyfile = certs&keys/key.pem   ; server-side only; client only needs certfile
```

`data_mode` only sets the client's *default* at login — it can still be
switched live in the REPL with `active` / `passive` / `fixed`, a genuine
runtime choice, not just a static setting.

`[reliability] mode` must be set the same way on **both** `server.py` and
`client.py`'s `config.ini` — there's no negotiation handshake for it (unlike
`data_mode`, which is negotiated live via `PORT`/`PASV`), the same way two
real TCP stacks have to speak the same protocol version. Same rule applies to
`[tls] enable`. `hash <filename>` and `[integrity] verify` work independently
of `[reliability] mode` — you can run hash verification over either the
best-effort or the GBN transport.

### Config presets

Copy whichever block below into `config.ini` for the demo you're running.
Commands like `mkd`/`cwd`/`active`/`passive`/`type I` always work regardless
of `config.ini` — this file only controls the server's concurrency model and
the client's data-mode *default* at login, not which commands exist.

**Minimal** — single-client, fixed-mode behavior (also what you get with no
`config.ini` at all):
```ini
[server]
threading = single

[client]
data_mode = fixed
```

**Concurrent + Passive mode** (recommended over Active mode: needs no
server-side `advertise_ip` tuning unless the server sits behind NAT):
```ini
[server]
threading = thread
storage_root = server_storage
advertise_ip =           ; set to the server's public IP if it's behind NAT (e.g. a cloud VM)

[client]
data_mode = passive
```

**Active mode variant** — same as above but the client defaults to `active`
instead of `passive`; `active`/`passive`/`fixed` also work live in the REPL
regardless of this default:
```ini
[client]
data_mode = active
```

**Full stack** — reliable UDP (Go-Back-N), automatic hash verification, TLS.
Set identically on both server and client:
```ini
[server]
threading = thread

[client]
data_mode = passive

[reliability]
mode = gbn
window_size = 4
rto_ms = 300
max_retries = 30

[integrity]
verify = true
algorithm = sha256

[tls]
enable = true
certfile = certs&keys/cert.pem
```
To actually *see* GBN recovering from loss rather than just running with
zero drops (loopback/LAN rarely drops packets on its own), demo it across a
real lossy path or narrate the retransmit counters — every retransmitted
window is a real timeout firing, not simulated.

## Architecture

- **Control channel (TCP, port 2121)**: every command/response is a single
  line, terminated with `\r\n`, using standard three-digit FTP reply codes.
  Optionally wrapped in TLS (`[tls] enable = true`, stdlib `ssl`) — every
  accepted connection is individually wrapped server-side
  (`PROTOCOL_TLS_SERVER`, handshake failures logged and dropped
  per-connection rather than crashing the server), and the client verifies
  the server's certificate against a locally trusted copy
  (`PROTOCOL_TLS_CLIENT` + `load_verify_locations`, never `CERT_NONE`)
  rather than skipping verification. The UDP data channel is unencrypted —
  encrypting it would need a symmetric cipher Python's stdlib doesn't
  provide, tracked as a future idea (`docs/Idea_development.md`), not
  implemented.
- **Data channel (UDP)**: file payload only, plus directory-listing text
  sent the same way. Each datagram has a 9-byte header (`type`, `seq`,
  `crc32` checksum) followed by up to 1024 bytes of payload.

### Data-channel modes

| Mode | How the address is learned | Socket used |
|---|---|---|
| **FIXED** (baseline) | Client sends one `HELLO` datagram to the server's well-known `DATA_PORT` (2122) right after login; the server reuses that address for the rest of the session. | One shared, server-wide socket. |
| **PASSIVE** | Client sends `PASV`; server opens a fresh per-session UDP socket and replies with its address (`227 ... (h1,h2,h3,h4,p1,p2)`); client `HELLO`s that address so the server learns the client's port too. | New ephemeral socket per session. |
| **ACTIVE** | Client sends `PORT h1,h2,h3,h4,p1,p2` with its own address; server already knows where to send. Because UDP is connectionless (unlike TCP's server-initiated `connect()` in real RFC 959 active mode), the server also opens its own per-session socket and reports *its* port back in the `200` reply, so the client knows where to send uploads. This is a deliberate, disclosed deviation from strict RFC 959 semantics — see the comment on `Reply.port_ok()` in `common.py`. | New ephemeral socket per session. |

Both `Session` (server) and `FTPClient` (client) carry a `data_mode`
attribute (`common.DataMode`), and the server resolves where to send/receive
through one method, `Session.resolve_data_endpoint()`.

**Concurrency caveat:** PASSIVE and ACTIVE sessions each get an isolated
per-session socket, so they're safe under `threading = thread`. FIXED-mode
sessions still share one server-wide socket — running two concurrent
FIXED-mode transfers can cross-talk. Use Active/Passive mode for the
concurrency demo.

### Directory tree

`Session.cwd` tracks an FTP-space path (starting at `/`); `resolve_path()`
resolves `CWD`/`MKD`/`RMD`/`LIST`/`NLST`/`STAT`/`MDTM`/`STOR`/`RETR`/`SIZE`
arguments against it and confines the result under `STORAGE_ROOT` (rejecting
any path that would escape it via `..`). `LIST`/`NLST` send their listing
text through the same UDP data-channel framing as file transfers rather than
the control channel.

### Concurrency

`main()`'s accept loop spawns one `threading.Thread` per connection when
`threading = thread`; each `Session` is independent, and a global
lock-protected table (`_active_sessions`) tracks who's connected — printed
to the server log on every connect/disconnect.

### Reliable-UDP layer (Go-Back-N)

Opt-in via `[reliability] mode = gbn` (default `none` = the original
best-effort framing: checksummed and sequenced, but no ACK/retransmit — a
lost packet silently truncates the transfer). The whole state machine lives
once in `common.py`'s `gbn_send()`/`gbn_receive()`, reused identically by
both `server.py` (STOR/RETR/LIST/NLST) and `client.py` (`put`/`get`/`ls`) —
both ends of a transfer must run the same mode to interoperate.

- **Sender**: a sliding window of up to `window_size` unacknowledged packets
  in flight (the flow-control knob) and a single timer for the oldest
  unacked packet. On timeout, the **whole** in-flight window is
  retransmitted — this is what makes it Go-Back-N rather than Selective
  Repeat.
- **Receiver**: only ever advances on the next expected in-order sequence
  number; anything else (corrupt, out-of-order, or a duplicate retransmit
  because the sender never saw our earlier ACK) gets the last good
  cumulative ACK re-sent, never buffered.
- The closing `PKT_FIN` is packet number *N* and rides the same reliable
  pipeline as the data chunks, so "transfer complete" is itself acknowledged
  — unlike the best-effort path, where a lost FIN just means the receiver
  waits out `SOCK_TIMEOUT` with no way to tell "done" from "still coming".
- If `max_retries` consecutive timeouts elapse without progress, the sender
  gives up and the transfer is reported as aborted (`426`) rather than
  hanging forever or silently claiming success — verified with a throwaway
  test harness that randomly drops both DATA and ACK packets at up to 50%
  simultaneous loss and confirmed the layer never reports success on
  corrupted/incomplete data, only ever a correctly-reassembled transfer or
  an honest failure.

### Data integrity verification

`HASH <filename>` (`common.compute_hash`, SHA-256 by default, MD5 available
via `[integrity] algorithm`) computes a digest of the file as stored on the
server. The `hash` REPL command always works standalone; `[integrity]
verify = true` additionally runs it automatically after every `put`/`get` —
and on `put`, the hash is checked *server-side* before the transfer is even
acknowledged: the client sends its hash alongside `STOR`, and the server
deletes the file and replies an error instead of `226` if what it wrote
doesn't match, rather than trusting the client's own after-the-fact
comparison. This works independently of `[reliability] mode` — hash
verification and the transport reliability mode are orthogonal opt-in
techniques.

## Deliberately not implemented

Left out on purpose:
- **Selective Repeat** as an alternative to Go-Back-N — more bandwidth-
  efficient (only the lost packet is retransmitted, not the whole window)
  but requires receiver-side out-of-order buffering and per-packet ACK
  tracking; Go-Back-N was chosen for a coherent, single mechanism that
  satisfies both the RDT and sliding-window requirements with less state to
  get right.
- **Adaptive/dynamic congestion control** (TCP-Reno-style slow start, AIMD,
  timeout-based window shrinking) — `window_size` is a fixed, configurable
  knob, not a self-tuning algorithm.
- **UDP data-channel encryption** — would need a symmetric cipher outside
  Python's stdlib; see `docs/Idea_development.md`.
- `STOU`, `APPE`, `DELE`, `RNFR`/`RNTO`, `MODE B`/`MODE C`, `ABOR`

## Known limitations

- With the default `[reliability] mode = none`, a UDP packet lost in transit
  (rare on loopback/LAN, more likely over a lossy real-world path) still
  silently truncates a transfer — unchanged unless `mode = gbn` is opted
  into.
- Go-Back-N's whole-window retransmission is less bandwidth-efficient under
  loss than Selective Repeat would be — a deliberate simplicity/efficiency
  trade-off, not an oversight.
- `[reliability] mode` and `[tls] enable` have no negotiation handshake
  (unlike `data_mode`, negotiated live via `PORT`/`PASV`) — both ends must
  be configured with the same value ahead of time.
