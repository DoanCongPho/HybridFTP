"""Benchmark harness for the Go-Back-N reliable-UDP layer (common.gbn_send/gbn_receive).

Does not modify server.py/client.py/common.py. It wraps a loopback UDP socket pair's
sendto() so outbound datagrams are randomly dropped (loss injection), then
drives the exact same gbn_send()/gbn_receive() functions server.py and
client.py use for `[reliability] mode = gbn` through a sweep of
window_size / rto_ms / loss_rate, recording throughput, transfer time, and
retransmit overhead for each combination. A best-effort (`mode = none`)
baseline is reproduced locally (mirroring, not calling, the inline logic in
server.py/client.py) to quantify what GBN actually buys over the default.

Usage:
    python3 benchmarks/gbn_benchmark.py
    python3 benchmarks/gbn_benchmark.py --sweep window --trials 5
    python3 benchmarks/gbn_benchmark.py --out results.csv --payload-kb 128

Then plot (needs matplotlib):
    python3 benchmarks/plot_results.py results.csv
"""

import argparse
import csv
import os
import random
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import CHUNK_SIZE, PKT_DATA, PKT_FIN, gbn_send, gbn_receive, make_packet, parse_packet  # noqa: E402

# Mirrors config.ini's [reliability] defaults, so the sweep brackets the
# values this project actually ships with.
DEFAULT_RTO_MS = 300
DEFAULT_WINDOW = 4

FIELDNAMES = [
    "sweep", "mode", "window_size", "rto_ms", "loss_rate", "payload_bytes",
    "success", "correct", "elapsed_s", "throughput_KBps",
    "retransmit_events", "retransmit_packets",
    "sender_packets_sent", "sender_packets_dropped",
    "receiver_packets_sent", "receiver_packets_dropped",
    "overhead_ratio", "bytes_received", "completeness_ratio",
]


class LossySocket:
    """Wraps a bound UDP socket so sendto() randomly drops outbound
    datagrams. gbn_send()/gbn_receive() only ever call sendto/recvfrom/
    settimeout on the socket object they're given, so they can't tell this
    isn't a real lossy network — no changes to common.py needed."""

    def __init__(self, sock, loss_rate):
        self._sock = sock
        self.loss_rate = loss_rate
        self.sent = 0
        self.dropped = 0

    def sendto(self, data, addr):
        self.sent += 1
        if random.random() < self.loss_rate:
            self.dropped += 1
            return len(data)   # pretend it went out — caller has no way to know otherwise
        return self._sock.sendto(data, addr)

    def recvfrom(self, bufsize):
        return self._sock.recvfrom(bufsize)

    def settimeout(self, t):
        self._sock.settimeout(t)


def _make_bound_pair():
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    a.bind(("127.0.0.1", 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    b.bind(("127.0.0.1", 0))
    return a, b


def run_gbn_trial(payload, window_size, rto_ms, loss_rate, max_retries, sweep_name):
    sender_raw, receiver_raw = _make_bound_pair()
    sender_addr, receiver_addr = sender_raw.getsockname(), receiver_raw.getsockname()
    sender_sock = LossySocket(sender_raw, loss_rate)
    receiver_sock = LossySocket(receiver_raw, loss_rate)

    retransmits = {"events": 0, "packets": 0}

    def on_retransmit(base, next_seq, _retries):
        retransmits["events"] += 1
        retransmits["packets"] += next_seq - base

    send_result, recv_result = {}, {}

    def sender_thread():
        start = time.monotonic()
        ok = gbn_send(sender_sock, receiver_addr, payload, window_size=window_size,
                       rto=rto_ms / 1000, max_retries=max_retries, on_retransmit=on_retransmit)
        send_result["ok"] = ok
        send_result["elapsed"] = time.monotonic() - start

    def receiver_thread():
        recv_result["data"] = gbn_receive(receiver_sock, sender_addr, rto=rto_ms / 1000,
                                           max_retries=max_retries)

    t_recv = threading.Thread(target=receiver_thread)
    t_send = threading.Thread(target=sender_thread)
    t_recv.start()
    t_send.start()
    t_send.join()
    t_recv.join()
    sender_raw.close()
    receiver_raw.close()

    ideal_packets = (len(payload) + CHUNK_SIZE - 1) // CHUNK_SIZE + 1  # DATA chunks + FIN
    elapsed = send_result.get("elapsed") or 0
    return {
        "sweep": sweep_name,
        "mode": "gbn",
        "window_size": window_size,
        "rto_ms": rto_ms,
        "loss_rate": loss_rate,
        "payload_bytes": len(payload),
        "success": bool(send_result.get("ok")) and recv_result.get("data") is not None,
        "correct": recv_result.get("data") == payload,
        "elapsed_s": round(elapsed, 4),
        "throughput_KBps": round((len(payload) / 1024) / elapsed, 2) if elapsed else 0.0,
        "retransmit_events": retransmits["events"],
        "retransmit_packets": retransmits["packets"],
        "sender_packets_sent": sender_sock.sent,
        "sender_packets_dropped": sender_sock.dropped,
        "receiver_packets_sent": receiver_sock.sent,
        "receiver_packets_dropped": receiver_sock.dropped,
        "overhead_ratio": round(sender_sock.sent / ideal_packets, 3) if ideal_packets else None,
        "bytes_received": len(recv_result["data"]) if recv_result.get("data") else 0,
        "completeness_ratio": None,
    }


# --- Best-effort (mode=none) baseline -----------------------------------------
# Mirrors — does not call — the inline mode=none path described in
# CLAUDE.md/server.py/client.py: fire every chunk once, no ACK/retransmit,
# receiver reassembles whatever arrived (sorted by seq) and gives up after
# one idle timeout. Reimplemented here rather than imported because that
# path isn't factored into a reusable common.py function (it's inline in
# handle_stor()/_recv_data_payload()); this benchmark only needs to
# reproduce its *behavior*, not share its exact code.

def _best_effort_send(sock, dest_addr, data, chunk_size=CHUNK_SIZE):
    seq = 0
    for i in range(0, len(data), chunk_size):
        sock.sendto(make_packet(PKT_DATA, seq, data[i:i + chunk_size]), dest_addr)
        seq += 1
    sock.sendto(make_packet(PKT_FIN, seq), dest_addr)


def _best_effort_receive(sock, expected_addr, timeout):
    chunks = {}
    sock.settimeout(timeout)
    while True:
        try:
            raw, addr = sock.recvfrom(CHUNK_SIZE + 64)
        except socket.timeout:
            break
        if addr != expected_addr:
            continue
        pkt_type, seq, payload, valid = parse_packet(raw)
        if not valid:
            continue
        if pkt_type == PKT_DATA:
            chunks[seq] = payload
        elif pkt_type == PKT_FIN:
            break
    return b"".join(chunks[k] for k in sorted(chunks))


def run_best_effort_trial(payload, loss_rate, timeout=1.0):
    sender_raw, receiver_raw = _make_bound_pair()
    sender_addr, receiver_addr = sender_raw.getsockname(), receiver_raw.getsockname()
    sender_sock = LossySocket(sender_raw, loss_rate)
    receiver_sock = LossySocket(receiver_raw, loss_rate)

    received = {}

    def receiver_thread():
        received["data"] = _best_effort_receive(receiver_sock, sender_addr, timeout)

    t_recv = threading.Thread(target=receiver_thread)
    t_recv.start()
    start = time.monotonic()
    _best_effort_send(sender_sock, receiver_addr, payload)
    t_recv.join()
    elapsed = time.monotonic() - start
    sender_raw.close()
    receiver_raw.close()

    got = received.get("data", b"")
    return {
        "sweep": "baseline",
        "mode": "best_effort",
        "window_size": None,
        "rto_ms": None,
        "loss_rate": loss_rate,
        "payload_bytes": len(payload),
        "success": got == payload,
        "correct": got == payload,
        "elapsed_s": round(elapsed, 4),
        "throughput_KBps": round((len(got) / 1024) / elapsed, 2) if elapsed else 0.0,
        "retransmit_events": 0,
        "retransmit_packets": 0,
        "sender_packets_sent": sender_sock.sent,
        "sender_packets_dropped": sender_sock.dropped,
        "receiver_packets_sent": receiver_sock.sent,
        "receiver_packets_dropped": receiver_sock.dropped,
        "overhead_ratio": 1.0,
        "bytes_received": len(got),
        "completeness_ratio": round(len(got) / len(payload), 3) if payload else None,
    }


def _log(row):
    tag = f"win={row['window_size']} rto={row['rto_ms']}ms" if row["mode"] == "gbn" else "best-effort"
    status = "OK" if row["success"] else "FAIL"
    print(f"  [{row['sweep']:>8}] {row['mode']:>11} loss={row['loss_rate']:.2f} {tag:<18} "
          f"-> {status:<4} {row['elapsed_s']:.3f}s {row['throughput_KBps']:.1f}KB/s "
          f"retransmits={row['retransmit_events']}")


def sweep_window(payload, trials, max_retries, rows):
    print("== Sweep: window_size (rto fixed at %dms) ==" % DEFAULT_RTO_MS)
    for window_size in (1, 2, 4, 8, 16):
        for loss_rate in (0.0, 0.1, 0.2, 0.5):
            for _ in range(trials):
                row = run_gbn_trial(payload, window_size, DEFAULT_RTO_MS, loss_rate, max_retries, "window")
                rows.append(row)
                _log(row)


def sweep_rto(payload, trials, max_retries, rows):
    print("== Sweep: rto_ms (window fixed at %d, loss fixed at 0.2) ==" % DEFAULT_WINDOW)
    for rto_ms in (100, 200, 300, 500, 1000):
        for _ in range(trials):
            row = run_gbn_trial(payload, DEFAULT_WINDOW, rto_ms, 0.2, max_retries, "rto")
            rows.append(row)
            _log(row)


def sweep_baseline(payload, trials, max_retries, rows):
    print("== Sweep: best-effort vs GBN across loss_rate ==")
    for loss_rate in (0.0, 0.05, 0.1, 0.2, 0.5):
        for _ in range(trials):
            row = run_best_effort_trial(payload, loss_rate)
            rows.append(row)
            _log(row)
        for _ in range(trials):
            row = run_gbn_trial(payload, DEFAULT_WINDOW, DEFAULT_RTO_MS, loss_rate, max_retries, "baseline")
            rows.append(row)
            _log(row)


def main():
    parser = argparse.ArgumentParser(description="Benchmark harness for common.gbn_send/gbn_receive.")
    parser.add_argument("--sweep", choices=["window", "rto", "baseline", "all"], default="all")
    parser.add_argument("--payload-kb", type=int, default=64, help="payload size per trial, in KB")
    parser.add_argument("--trials", type=int, default=3, help="repeats per parameter combination")
    parser.add_argument("--max-retries", type=int, default=30, help="GBN max_retries per trial")
    parser.add_argument("--seed", type=int, default=None, help="random seed, for reproducible loss patterns")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results.csv"))
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    payload = os.urandom(args.payload_kb * 1024)
    rows = []
    start = time.monotonic()

    if args.sweep in ("window", "all"):
        sweep_window(payload, args.trials, args.max_retries, rows)
    if args.sweep in ("rto", "all"):
        sweep_rto(payload, args.trials, args.max_retries, rows)
    if args.sweep in ("baseline", "all"):
        sweep_baseline(payload, args.trials, args.max_retries, rows)

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    elapsed = time.monotonic() - start
    print(f"\n{len(rows)} trials in {elapsed:.1f}s -> {args.out}")
    print(f"Plot with: python3 benchmarks/plot_results.py {args.out}")


if __name__ == "__main__":
    main()
