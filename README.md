# Hybrid FTP — Basic Level

A minimal Hybrid FTP client/server: TCP control channel + UDP data channel,
implemented from scratch with Python's standard `socket` module only.

This implements the **Basic Level** of the assignment rubric:
- Basic user identification and access verification (`USER`/`PASS`)
- ASCII text file handling
- Upload and download of a single file (`STOR` / `RETR`)
- A single, fixed data-channel connection mechanism (no active/passive switching)
- Single-threaded server (one client session at a time)

## Running

Start the server:
```
python3 server.py
```
Listens on TCP port `2121` (control) and UDP port `2122` (data), storing files
under `server_storage/`.

Start the client (in another terminal / machine):
```
python3 client.py <server_host>
```

### Client commands
```
user <name>
pass <password>
put <local_file> [remote_name]     upload
get <remote_name> [local_file]     download (saved to client_downloads/)
pwd
size <filename>
noop
help
quit
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

## Architecture

- **Control channel (TCP, port 2121)**: every command/response is a single
  line, terminated with `\r\n`, using standard three-digit FTP reply codes.
- **Data channel (UDP, port 2122)**: file payload only. Each datagram has a
  9-byte header (`type`, `seq`, `crc32 checksum`) followed by up to 1024 bytes
  of payload. The client announces its UDP address to the server with one
  `HELLO` packet right after login; the server reuses that address for every
  transfer in the session — this is the "single, fixed data-channel
  connection mechanism" required at Basic Level.

### Extensibility seam (not implemented yet)
Both `Session` (server) and `FTPClient` (client) carry a `data_mode`
attribute (`common.DataMode`, currently always `FIXED`), and address
resolution goes through one method, `Session.resolve_data_endpoint()`. This
is the seam where Active/Passive mode switching (Advanced Level) would plug
in later — the `PORT`/`PASV` commands are already recognized in the
dispatcher and reply `502 Command not implemented` rather than falling
through to "unknown command", so no restructuring is needed to add real
mode-switching logic later.

## Deliberately not implemented (Basic Level scope)

These are Advanced/Excellent-Level rubric items, left out on purpose:
- Binary file transfer (`TYPE I`) — only `TYPE A` (ASCII) is accepted
- Directory navigation/tree operations (`CWD`, `CDUP`, `MKD`, `RMD`, `LIST`,
  `NLST`, `STAT`)
- Active/Passive mode switching (`PORT`, `PASV`) — see extensibility seam above
- Concurrent/multi-client sessions — server handles one client at a time
- Full reliable-UDP layer: no ACK/timeout/retransmit, no sliding window /
  congestion control. Packets carry a checksum and sequence number (dropped
  if corrupt, reordered on receipt) but a lost packet is **not** recovered.
- End-to-end hash verification (`HASH`), `STOU`, `APPE`, `DELE`, `RNFR`/`RNTO`,
  `MDTM`, `MODE`, `ABOR`

## Known limitation

Because there is no retransmission, a UDP packet lost in transit (rare on
loopback/LAN, more likely over a lossy real-world path) will silently
truncate a transfer. Demonstrated and tested over loopback and a real
network path (Azure VM); no losses observed in testing, but this is not a
guarantee — see "Deliberately not implemented" above.
