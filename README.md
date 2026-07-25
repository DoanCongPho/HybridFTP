# Hybrid FTP

A Hybrid FTP client/server: TCP control channel + UDP data channel,
implemented from scratch with Python's standard `socket` module only.

Implements **Basic Level** end to end, all four **Advanced Level** rubric
items — binary transfer, a real directory tree, Active/Passive mode
switching, and a concurrent (multi-threaded) server — and all three
**Excellent Level** items: a custom Go-Back-N reliable-UDP layer, sliding-
window flow control, and end-to-end MD5/SHA-256 integrity verification.
Every technique past Basic Level is **opt-in via `config.ini`**: with no
config file (or its defaults), the server and client behave exactly like
Basic Level. See "Configuration" below.

## Running

Start the server:
```
python3 server.py
```
Listens on TCP port `2121` (control) and UDP port `2122` (data — FIXED mode
only; Active/Passive sessions get their own ephemeral port), storing files
under `server_storage/`.

Start the client (in another terminal / machine):
```
python3 client.py <server_host>
```

### Client commands

**Basic Level** — always available, no config needed:
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

**Advanced Level** — directory tree, binary transfer, Active/Passive mode:
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

**Excellent Level** — reliable-UDP transfer and integrity verification:
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
`config.py`). Every key is optional and defaults to Basic Level behavior, so
deleting `config.ini` (or any key in it) reproduces the original Basic Level
app exactly — config only lets you *opt into* Advanced Level techniques
without touching code:

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
```

`data_mode` only sets the client's *default* at login — it can still be
switched live in the REPL with `active` / `passive` / `fixed`, satisfying
the rubric's "Active/Passive mode switching" requirement as a genuine
runtime choice, not just a static setting.

`[reliability] mode` must be set the same way on **both** `server.py` and
`client.py`'s `config.ini` — there's no negotiation handshake for it (unlike
`data_mode`, which is negotiated live via `PORT`/`PASV`), the same way two
real TCP stacks have to speak the same protocol version. `hash <filename>`
and `[integrity] verify` work independently of `[reliability] mode` — you
can run hash verification over either the best-effort or the GBN transport.

### Config presets by level

Copy whichever block below into `config.ini` for the demo you're running.
Commands like `mkd`/`cwd`/`active`/`passive`/`type I` always work regardless
of `config.ini` — this file only controls the server's concurrency model and
the client's data-mode *default* at login, not which commands exist.

**Basic Level demo** — reproduces the original single-client, fixed-mode
behavior exactly (this is also what you get with no `config.ini` at all):
```ini
[server]
threading = single

[client]
data_mode = fixed
```

**Advanced Level demo** — multi-threaded server + Passive mode by default
(recommended over Active mode for a live demo: it needs no server-side
`advertise_ip` tuning unless the server sits behind NAT):
```ini
[server]
threading = thread
storage_root = server_storage
advertise_ip =           ; set to the server's public IP if it's behind NAT (e.g. a cloud VM)

[client]
data_mode = passive
```

**Advanced Level demo, Active mode variant** — same as above but with the
client defaulting to `active` instead of `passive`; useful for showing both
mode-switch directions in the oral defense (`active`/`passive`/`fixed` also
work live in the REPL regardless of this default):
```ini
[client]
data_mode = active
```

**Excellent Level demo** — everything above, plus Go-Back-N and automatic
hash verification. Set identically on both server and client:
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
```
To actually *see* GBN recovering from loss rather than just running with
zero drops (loopback/LAN rarely drops packets on its own), demo it across a
real lossy path (e.g. the Azure VM setup in `docs/GENAI_LOG.md`) or narrate
the retransmit counters — every retransmitted window is a real timeout
firing, not simulated.

## Architecture

- **Control channel (TCP, port 2121)**: every command/response is a single
  line, terminated with `\r\n`, using standard three-digit FTP reply codes.
- **Data channel (UDP)**: file payload only, plus (Advanced Level)
  directory-listing text sent the same way. Each datagram has a 9-byte
  header (`type`, `seq`, `crc32` checksum) followed by up to 1024 bytes of
  payload.

### Data-channel modes

| Mode | How the address is learned | Socket used |
|---|---|---|
| **FIXED** (Basic Level) | Client sends one `HELLO` datagram to the server's well-known `DATA_PORT` (2122) right after login; the server reuses that address for the rest of the session. | One shared, server-wide socket. |
| **PASSIVE** (Advanced) | Client sends `PASV`; server opens a fresh per-session UDP socket and replies with its address (`227 ... (h1,h2,h3,h4,p1,p2)`); client `HELLO`s that address so the server learns the client's port too. | New ephemeral socket per session. |
| **ACTIVE** (Advanced) | Client sends `PORT h1,h2,h3,h4,p1,p2` with its own address; server already knows where to send. Because UDP is connectionless (unlike TCP's server-initiated `connect()` in real RFC 959 active mode), the server also opens its own per-session socket and reports *its* port back in the `200` reply, so the client knows where to send uploads. This is a deliberate, disclosed deviation from strict RFC 959 semantics — see the comment on `Reply.port_ok()` in `common.py`. | New ephemeral socket per session. |

Both `Session` (server) and `FTPClient` (client) carry a `data_mode`
attribute (`common.DataMode`), and the server resolves where to send/receive
through one method, `Session.resolve_data_endpoint()` — the seam the
original Basic Level design left for this.

**Concurrency caveat:** PASSIVE and ACTIVE sessions each get an isolated
per-session socket, so they're safe under `threading = thread`. FIXED-mode
sessions still share the one server-wide socket (that's what "single, fixed
data-channel connection mechanism" means at Basic Level) — running two
concurrent FIXED-mode transfers can cross-talk. Use Active/Passive mode for
the concurrency demo.

### Directory tree

`Session.cwd` tracks an FTP-space path (starting at `/`); `resolve_path()`
resolves `CWD`/`MKD`/`RMD`/`LIST`/`NLST`/`STAT`/`MDTM`/`STOR`/`RETR`/`SIZE`
arguments against it and confines the result under `STORAGE_ROOT` (rejecting
any path that would escape it via `..`). `LIST`/`NLST` send their listing
text through the same UDP data-channel framing as file transfers rather than
the control channel, matching RFC 959's "listings go over the data
connection" model.

### Concurrency

`main()`'s accept loop spawns one `threading.Thread` per connection when
`threading = thread`; each `Session` is independent, and a global
lock-protected table (`_active_sessions`) tracks who's connected — printed
to the server log on every connect/disconnect, satisfying the "server log
displays connected client IPs... and active session table" requirement.

### Reliable-UDP layer (Go-Back-N)

Opt-in via `[reliability] mode = gbn` (default `none` = the original
best-effort framing: checksummed and sequenced, but no ACK/retransmit — a
lost packet silently truncates the transfer). The whole state machine lives
once in `common.py`'s `gbn_send()`/`gbn_receive()`, reused identically by
both `server.py` (STOR/RETR/LIST/NLST) and `client.py` (`put`/`get`/`ls`) —
both ends of a transfer must run the same mode to interoperate, the same way
two TCP stacks must speak the same protocol version.

- **Sender**: a sliding window of up to `window_size` unacknowledged packets
  in flight (the flow-control knob — the rubric's separate "Sliding Window"
  requirement, satisfied by the same mechanism as the RDT requirement) and a
  single timer for the oldest unacked packet. On timeout, the **whole**
  in-flight window is retransmitted — this is what makes it Go-Back-N rather
  than Selective Repeat.
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
the client hashes the bytes it just sent/received locally, asks the server
for its hash of the same file, and prints `MATCH`/`INTEGRITY CHECK FAILED`.
This works independently of `[reliability] mode` — hash verification and
the transport reliability mode are orthogonal opt-in techniques.

## Deliberately not implemented

Left out on purpose:
- **Selective Repeat** as an alternative to Go-Back-N — more bandwidth-
  efficient (only the lost packet is retransmitted, not the whole window)
  but requires receiver-side out-of-order buffering and per-packet ACK
  tracking; Go-Back-N was chosen for a coherent, single mechanism that
  satisfies both the RDT and sliding-window rubric items with less state to
  get right and explain live in the oral defense.
- **Adaptive/dynamic congestion control** (TCP-Reno-style slow start, AIMD,
  timeout-based window shrinking) — `window_size` is a fixed, configurable
  knob (satisfies "Sliding Window **or equivalent** mechanism to prevent
  network flooding" per the spec), not a self-tuning algorithm.
- `STOU`, `APPE`, `DELE`, `RNFR`/`RNTO`, `MODE B`/`MODE C`, `ABOR`

## Known limitations

- With the default `[reliability] mode = none`, a UDP packet lost in transit
  (rare on loopback/LAN, more likely over a lossy real-world path) still
  silently truncates a transfer — this is the documented Basic/Advanced
  Level behavior, unchanged unless `mode = gbn` is opted into.
- Go-Back-N's whole-window retransmission is less bandwidth-efficient under
  loss than Selective Repeat would be (see "Deliberately not implemented"
  above) — a deliberate simplicity/efficiency trade-off, not an oversight.
- `[reliability] mode` has no negotiation handshake (unlike `data_mode`,
  negotiated live via `PORT`/`PASV`) — both ends must be configured with the
  same value ahead of time, or a GBN-speaking peer talking to a best-effort
  peer will simply see its ACKs/retransmits go unrecognized and time out.
