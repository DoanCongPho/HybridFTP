# Hybrid FTP

A Hybrid FTP client/server: TCP control channel + UDP data channel,
implemented from scratch with Python's standard `socket` module only.

Implements **Basic Level** end to end, plus all four **Advanced Level**
rubric items — binary transfer, a real directory tree, Active/Passive mode
switching, and a concurrent (multi-threaded) server. Advanced techniques are
**opt-in via `config.ini`**: with no config file (or its defaults), the
server and client behave exactly like Basic Level. See "Configuration"
below.

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
```

`data_mode` only sets the client's *default* at login — it can still be
switched live in the REPL with `active` / `passive` / `fixed`, satisfying
the rubric's "Active/Passive mode switching" requirement as a genuine
runtime choice, not just a static setting.

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

## Deliberately not implemented (Excellent Level scope)

Left out on purpose — see the assignment's Excellent Level tier:
- Full reliable-UDP layer: no ACK/timeout/retransmit, no sliding window /
  congestion control. Packets carry a checksum and sequence number (dropped
  if corrupt, reordered on receipt) but a lost packet is **not** recovered.
- End-to-end hash verification (`HASH`), `STOU`, `APPE`, `DELE`,
  `RNFR`/`RNTO`, `MODE B`/`MODE C`

## Known limitation

Because there is no retransmission, a UDP packet lost in transit (rare on
loopback/LAN, more likely over a lossy real-world path) will silently
truncate a transfer. Demonstrated and tested over loopback and a real
network path (Azure VM); no losses observed in testing, but this is not a
guarantee — see "Deliberately not implemented" above.
