"""Діаграми ЛР5 за ``results/lab5/summary.json``.

Запуск: python -m scripts.lab5_charts
"""

import argparse
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLOR_W = "#2b6cb0"
COLOR_R = "#c05621"


def save(fig, out_dir, name):
    os.makedirs(out_dir, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}.png, {name}.svg")


def chart_throughput(s, out_dir):
    """X_w і X_r за трьома способами синхронізації."""
    labels = ["Глобальний\nзамок", "RWLock,\nперевага читача", "RWLock,\nперевага писця"]
    keys = ["global", "rw", "rw_writer"]
    xw = [s[k]["write"]["X"] for k in keys]
    xr = [s[k]["read"]["X"] for k in keys]
    pos = range(len(labels))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.bar([p - 0.19 for p in pos], xw, width=0.38, label="запис X_w", color=COLOR_W)
    ax.bar([p + 0.19 for p in pos], xr, width=0.38, label="читання X_r", color=COLOR_R)
    for p, (a, b) in enumerate(zip(xw, xr)):
        ax.text(p - 0.19, a, f"{a:,.0f}".replace(",", " "), ha="center",
                va="bottom", fontsize=8)
        ax.text(p + 0.19, b, f"{b:,.0f}".replace(",", " "), ha="center",
                va="bottom", fontsize=8)
    ax.axhline(s["x1_global"], color="#555", ls="--", lw=1)
    ax.text(len(labels) - 0.5, s["x1_global"], f"  X_1 = {s['x1_global']:,.0f} оп/с"
            .replace(",", " "), va="center", fontsize=8, color="#555")
    ax.set_xticks(list(pos))
    ax.set_xticklabels(labels)
    ax.set_ylabel("операцій за секунду")
    ax.set_title("ЛР5, варіант 19: швидкість запису і читання (n = 6, дек С9)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    save(fig, out_dir, "throughput")


def chart_threads(s, out_dir):
    """Операції кожного потоку: рівномірність доступу до замка."""
    g = {t["thread"]: t["ops"] for t in s["global"]["per_thread"]}
    r = {t["thread"]: t["ops"] for t in s["rw"]["per_thread"]}
    tids = sorted(g)
    pos = range(len(tids))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.bar([p - 0.19 for p in pos], [g[t] for t in tids], width=0.38,
           label=f"глобальний замок, cv = {s['global']['total']['cv']:.3f}", color="#4a5568")
    ax.bar([p + 0.19 for p in pos], [r[t] for t in tids], width=0.38,
           label=f"RWLock, cv = {s['rw']['total']['cv']:.3f}", color="#38a169")
    ax.set_xticks(list(pos))
    ax.set_xticklabels([f"{t}\n{'запис' if t % 2 else 'читання'}" for t in tids])
    ax.set_ylabel("операцій за D = 10 с")
    ax.set_title("ЛР5: операції кожного потоку за вимірювання")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    save(fig, out_dir, "threads")


def chart_tail(s, out_dir):
    """p50 і p99 тривалості операції за ролями і способами синхронізації."""
    groups = [
        ("Глобальний,\nзапис", s["global"]["write"]),
        ("Глобальний,\nчитання", s["global"]["read"]),
        ("RWLock,\nзапис", s["rw"]["write"]),
        ("RWLock,\nчитання", s["rw"]["read"]),
    ]
    pos = range(len(groups))
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.bar([p - 0.19 for p in pos], [x[1]["p50_us"] for x in groups], width=0.38,
           label="p50", color="#718096")
    ax.bar([p + 0.19 for p in pos], [x[1]["p99_us"] for x in groups], width=0.38,
           label="p99", color="#9b2c2c")
    for p, (_, st) in enumerate(groups):
        ax.text(p + 0.19, st["p99_us"], f" h = {st['h']:.0f}", ha="center",
                va="bottom", fontsize=8)
    ax.set_yscale("log")
    ax.set_xticks(list(pos))
    ax.set_xticklabels([x[0] for x in groups])
    ax.set_ylabel("мкс, логарифмічна шкала")
    ax.set_title("ЛР5: тривалість операції, медіана і хвіст")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    save(fig, out_dir, "latency")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Діаграми ЛР5")
    ap.add_argument("--summary", default="results/lab5/summary.json")
    ap.add_argument("--out-dir", default="results/lab5/charts")
    a = ap.parse_args(argv)
    with open(a.summary, encoding="utf-8") as f:
        s = json.load(f)
    print("діаграми:")
    chart_throughput(s, a.out_dir)
    chart_threads(s, a.out_dir)
    chart_tail(s, a.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
