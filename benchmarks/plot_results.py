"""Plots the CSV produced by gbn_benchmark.py. Optional — only needed for
the graphs, not for running the benchmark itself.

Usage:
    python3 benchmarks/plot_results.py results.csv [--out-dir plots]
"""

import argparse
import csv
import os
import sys

try:
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("matplotlib not installed. Run: pip install matplotlib")


def load_rows(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    for row in rows:
        for key in ("window_size", "rto_ms", "retransmit_events", "retransmit_packets",
                    "sender_packets_sent", "sender_packets_dropped"):
            row[key] = int(row[key]) if row[key] not in ("", None) else None
        for key in ("loss_rate", "elapsed_s", "throughput_KBps", "overhead_ratio", "completeness_ratio"):
            row[key] = float(row[key]) if row[key] not in ("", None) else None
        row["success"] = row["success"] == "True"
    return rows


def avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def plot_window_sweep(rows, out_dir):
    data = [r for r in rows if r["sweep"] == "window"]
    if not data:
        return
    loss_rates = sorted({r["loss_rate"] for r in data})
    window_sizes = sorted({r["window_size"] for r in data})

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    for loss_rate in loss_rates:
        xs, throughput, overhead = [], [], []
        for w in window_sizes:
            group = [r for r in data if r["window_size"] == w and r["loss_rate"] == loss_rate]
            succeeded = [r for r in group if r["success"]]
            xs.append(w)
            throughput.append(avg(r["throughput_KBps"] for r in succeeded))
            overhead.append(avg(r["overhead_ratio"] for r in group))
        ax1.plot(xs, throughput, marker="o", label=f"loss={loss_rate:.0%}")
        ax2.plot(xs, overhead, marker="o", label=f"loss={loss_rate:.0%}")

    ax1.set_xlabel("window_size")
    ax1.set_ylabel("throughput (KB/s, log scale)")
    ax1.set_yscale("log")
    ax1.set_title("Throughput vs window size")
    ax1.legend()
    ax2.set_xlabel("window_size")
    ax2.set_ylabel("overhead ratio (packets sent / ideal)")
    ax2.set_title("Retransmit overhead vs window size")
    ax2.axhline(1.0, color="gray", linestyle="--", linewidth=1)
    ax2.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "window_sweep.png"), dpi=150)
    print("wrote", os.path.join(out_dir, "window_sweep.png"))


def plot_rto_sweep(rows, out_dir):
    data = [r for r in rows if r["sweep"] == "rto"]
    if not data:
        return
    rto_values = sorted({r["rto_ms"] for r in data})
    throughput, overhead = [], []
    for rto_ms in rto_values:
        group = [r for r in data if r["rto_ms"] == rto_ms]
        succeeded = [r for r in group if r["success"]]
        throughput.append(avg(r["throughput_KBps"] for r in succeeded))
        overhead.append(avg(r["overhead_ratio"] for r in group))

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    ax1.plot(rto_values, throughput, marker="o", color="tab:blue")
    ax1.set_xlabel("rto_ms")
    ax1.set_ylabel("throughput (KB/s)")
    ax1.set_title("Throughput vs RTO (loss=20%, window=4)")
    ax2.plot(rto_values, overhead, marker="o", color="tab:orange")
    ax2.set_xlabel("rto_ms")
    ax2.set_ylabel("overhead ratio")
    ax2.set_title("Retransmit overhead vs RTO")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "rto_sweep.png"), dpi=150)
    print("wrote", os.path.join(out_dir, "rto_sweep.png"))


def plot_baseline(rows, out_dir):
    data = [r for r in rows if r["sweep"] == "baseline"]
    if not data:
        return
    loss_rates = sorted({r["loss_rate"] for r in data})
    gbn_success, best_effort_success, best_effort_completeness = [], [], []
    for loss_rate in loss_rates:
        gbn_group = [r for r in data if r["mode"] == "gbn" and r["loss_rate"] == loss_rate]
        be_group = [r for r in data if r["mode"] == "best_effort" and r["loss_rate"] == loss_rate]
        gbn_success.append(100 * sum(r["success"] for r in gbn_group) / len(gbn_group) if gbn_group else None)
        best_effort_success.append(100 * sum(r["success"] for r in be_group) / len(be_group) if be_group else None)
        best_effort_completeness.append(100 * avg(r["completeness_ratio"] for r in be_group) if be_group else None)

    # Same metric, both modes: did the receiver end up with the byte-exact
    # file (r["success"]), not a partial-credit average. best-effort's
    # completeness_ratio is plotted too (dashed) to show *why* it's
    # dangerous: it looks "almost done" while never actually being correct.
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot([lr * 100 for lr in loss_rates], gbn_success, marker="o", color="tab:blue",
            label="GBN: % trials byte-exact correct")
    ax.plot([lr * 100 for lr in loss_rates], best_effort_success, marker="s", color="tab:red",
            label="best-effort: % trials byte-exact correct")
    ax.plot([lr * 100 for lr in loss_rates], best_effort_completeness, marker="s", color="tab:orange",
            linestyle="--", alpha=0.6, label="best-effort: avg % bytes received (looks OK, isn't)")
    ax.set_xlabel("simulated loss rate (%)")
    ax.set_ylabel("%")
    ax.set_ylim(-5, 105)
    ax.set_title("GBN vs best-effort under packet loss (same metric)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "baseline_comparison.png"), dpi=150)
    print("wrote", os.path.join(out_dir, "baseline_comparison.png"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "plots"))
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    rows = load_rows(args.csv_path)

    plot_window_sweep(rows, args.out_dir)
    plot_rto_sweep(rows, args.out_dir)
    plot_baseline(rows, args.out_dir)


if __name__ == "__main__":
    main()
