# Hybrid FTP

A file-transfer client/server built **from scratch on Python's standard `socket` module only**
— no frameworks, no third-party networking libraries. TCP control channel + UDP data channel,
with a hand-rolled reliable-UDP layer instead of just relying on TCP.

**🟢 Live demo running now:** `dcpho.site` (TCP `2121` / UDP `2122`) — see [Connect to the live
server](#connect-to-the-live-server) below.

## Highlights

- **Custom reliable-UDP transport** — Go-Back-N from scratch (sliding window, cumulative ACK,
  timeout-triggered retransmission), stress-tested with a purpose-built packet-loss injection
  harness at up to 50% simulated bidirectional loss with zero false-positive successes.
- **TLS on the control channel** — real certificate verification (not `CERT_NONE`), per-connection
  handshake isolation so a bad handshake can't take the server down.
- **Concurrent server** — one thread per client, live session table, safe under concurrent
  Active/Passive-mode transfers.
- **RFC 959-style Active/Passive mode negotiation** (`PORT`/`PASV`) and a real server-side
  directory tree with path-escape protection.
- **End-to-end integrity verification** — SHA-256/MD5, checked server-side on every upload; a
  corrupted transfer is deleted and rejected, not silently accepted.
- Every technique above is **opt-in via `config.ini`** — deleting the config file reproduces
  plain USER/PASS + upload/download behavior exactly, nothing is hardcoded to "always on."

Full protocol design, config reference, and command list: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Connect to the live server

```
git clone <this-repo> && cd hybrid_ftp
mkdir -p "certs&keys"
echo | openssl s_client -connect dcpho.site:2121 -servername dcpho.site 2>/dev/null \
    | openssl x509 -out "certs&keys/cert.pem"
openssl x509 -in "certs&keys/cert.pem" -noout -fingerprint -sha256
```
Confirm the printed fingerprint matches:
```
F2:AA:95:74:00:14:5D:A6:57:EE:CB:B9:6E:11:D6:6B:A0:45:E0:35:3E:53:64:A3:99:31:8D:0B:B2:64:99:6E
```
(This is a trust-on-first-use check — same idea as verifying a new SSH host key — so a
man-in-the-middle can't hand you a different certificate.)

Add to `config.ini`:
```ini
[tls]
enable = true
certfile = certs&keys/cert.pem
```
Then connect (must use the hostname, not the raw IP — the cert is only valid for `dcpho.site`):
```
python3 client.py dcpho.site
```
```
ftp> user alice
ftp> pass password123
ftp> put hello.txt
ftp> get hello.txt
ftp> quit
```
Login: `alice/password123` or `bob/hunter2`.

## Run it yourself

```
python3 server.py                 # TCP 2121 (control) + UDP 2122 (data)
python3 client.py <server_host>
```
No `config.ini` needed for the baseline (plain USER/PASS, upload/download, one client at a
time). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full command list and every
opt-in config preset (concurrency, Active/Passive mode, Go-Back-N, integrity verification, TLS).
