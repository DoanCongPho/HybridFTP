# Idea Development — Post-Assignment Extensions (CV/Portfolio Angle)

This document tracks feature ideas considered *after* the assignment's Basic/Advanced/Excellent
Level rubric is fully satisfied, specifically evaluated for how much they'd strengthen this project
as a CV/portfolio piece — not for coursework credit. Nothing here is required by
`Project1_SocketProgramming_2026.pdf`; treat this as a backlog, not a plan in progress.

The guiding principle: a CV reviewer for a networking/backend/systems role cares more about
*measured, explained engineering depth* ("I built X and proved it does Y under Z conditions") than
about surface polish. The ideas below are ranked with that lens.

---

## Recommended

### 1. Quantified reliability-layer benchmarking

**What:** A small benchmark harness (reusing the loss-injection test approach already used to
validate `gbn_send()`/`gbn_receive()` — see `docs/GENAI_LOG.md` Entry 12) that sweeps
`window_size`, `rto_ms`, and simulated packet-loss rate, and records throughput / total transfer
time / retransmit count for each combination. Plot the results (e.g. throughput vs. window size at
several loss rates; GBN vs. `mode=none` best-effort as a baseline).

**Effort/tradeoff:** low-to-medium. The loss-injection harness already exists as scratch code from
verifying the Excellent Level implementation; this mostly reuses it, wraps it in a sweep loop, and
adds plotting (matplotlib). No changes needed to `server.py`/`client.py`/`common.py` themselves.

### 2. Transport-layer security (TLS on the control channel, optionally the data channel)

**What:** Wrap the TCP control-channel socket in `ssl.wrap_socket`/`SSLContext` (self-signed cert
for a portfolio project is fine), so `USER`/`PASS` and all commands aren't sent in cleartext. A
further step would be encrypting the UDP data-channel payload (e.g. per-packet AES-GCM) — more
involved since it interacts with the GBN framing (checksum vs. AEAD tag) and would need its own
design note before touching `common.py`'s packet format.


**Effort/tradeoff:** control-channel TLS alone is low effort (stdlib `ssl`, no protocol/packet
format changes, control channel is already line-based TCP). Data-channel encryption is
medium-to-high effort and would need a deliberate design decision (documented the same way the
project already documents `Reply.port_ok()`'s RFC 959 deviation) before implementing, since it
touches the packet header/framing that `gbn_send()`/`gbn_receive()`/`compute_hash()` all depend on.

