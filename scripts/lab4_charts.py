"""Діаграми ЛР4: X(W) з кривою закону (крок 6), черга і завершення за секунду
під прогоном з падіннями і сценарієм Ф3 (крок 9), дублі (кроки 3-4) і Д5 (крок 10).

Потрібен matplotlib: pip install -r requirements-dev.txt

    python -m scripts.lab4_charts
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252

import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys_path = Path(__file__).resolve().parent.parent
if str(sys_path) not in _sys.path:
    _sys.path.insert(0, str(sys_path))

from loadgen.usl import fit, model  # noqa: E402

ROOT = sys_path
LAB4 = ROOT / "results" / "lab4"
OUT = LAB4 / "charts"
WLIST = (1, 2, 4)
BLUE, RED, GREEN, ORANGE = "#4c72b0", "#c44e52", "#55a868", "#dd8452"


def load(name):
    p = LAB4 / f"{name}.json"
    return json.load(io.open(p, encoding="utf-8")) if p.exists() else None


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=140)
    fig.savefig(OUT / f"{name}.svg")
    plt.close(fig)
    print(f"-> results/lab4/charts/{name}.png", flush=True)


# ---------------------------------------------------------------- крок 6

def chart_x_w(tag="_long"):
    """X(W) за середніми кількох прогонів; вуса — розкид МІЖ прогонами.

    Розкид усередині прогону (посекундний) тут не годиться: між прогонами того самого W
    пропускна здатність різниться сильніше, ніж усередині одного, і саме ця величина
    визначає, наскільки можна довіряти підбору.
    """
    import glob
    import os
    import statistics as st
    acc = {}
    seen = set()
    for d in (LAB4, LAB4 / "v1"):
        for fpath in sorted(glob.glob(str(d / "step6*_W*.json"))):
            n = os.path.basename(fpath)[:-5]
            if n.endswith("_gen"):
                continue
            r = json.load(io.open(fpath, encoding="utf-8"))
            X = r.get("X") or {}
            if not X or not r.get("pre_queued"):
                continue
            if r["pre_queued"] < 1000 or X["window_s"] < 30:
                continue
            key = (r["workers"], r["pre_queued"], round(X["X_rps"], 4))
            if key in seen:
                continue
            seen.add(key)
            acc.setdefault(r["workers"], []).append(X["X_rps"])
    if 1 not in acc:
        return
    pts = {w: st.fmean(v) for w, v in acc.items()}
    sds = {w: (st.stdev(v) if len(v) > 1 else 0.0) for w, v in acc.items()}
    counts = {w: len(v) for w, v in acc.items()}
    f = fit(pts)

    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    ws = sorted(pts)
    ax.errorbar(ws, [pts[w] for w in ws], yerr=[sds[w] for w in ws], fmt="o",
                color=BLUE, capsize=5, markersize=7,
                label="середнє X(W) ± розкид між прогонами")
    top = max(max(ws) + 1.2, (f.get("N_star") or 0) + 0.8)
    grid = [1 + 0.05 * k for k in range(int((top - 1) / 0.05) + 1)]
    ax.plot(grid, [model(w, f["lambda"], f["sigma"], f["kappa"]) for w in grid],
            color=RED, label=(f"закон: σ = {f['sigma']:.4f}, κ = {f['kappa']:.4f}"
                              .replace(".", ",")))
    ax.plot(grid, [f["lambda"] * w for w in grid], color="#999999", linestyle=":",
            label="лінійне зростання λ·W")
    if f.get("N_star") and f["N_star"] <= top:
        ax.axvline(f["N_star"], color=GREEN, linestyle="--",
                   label=f"W* = {f['N_star']:.2f}".replace(".", ","))
    for w in ws:
        ax.annotate(f"{pts[w]:.1f}".replace(".", ","), (w, pts[w]),
                    textcoords="offset points", xytext=(8, 6), fontsize=9)
    ax.set_title("Пропускна здатність пулу воркерів X(W)\n"
                 "непорожня черга, вікно 30 с; "
                 + ", ".join(f"W={w}: {counts[w]} прогонів"
                             for w in sorted(counts)))
    ax.set_xlabel("кількість воркерів W")
    ax.set_ylabel("задач за секунду")
    ax.set_xlim(0.8, top)
    ax.set_ylim(0, max(pts.values()) * 1.35)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=9)
    fig.tight_layout()
    save(fig, "x_w")


# ---------------------------------------------------------------- крок 9

def series(prom_dump, name):
    s = (prom_dump or {}).get("series", {}).get(name) or []
    if not s:
        return [], []
    vals = s[0]["values"]
    return [t for t, _ in vals], [v for _, v in vals]


def chart_step9(name="step9_poison_lo"):
    d = load(name)
    if not d:
        return
    t0 = d["run_start_wall"]
    pr = d.get("prom")
    fig, ax = plt.subplots(figsize=(8.4, 4.6))
    for metric, color, label in (("tasks_queued", BLUE, "tasks_queued"),
                                 ("tasks_running", GREEN, "tasks_running")):
        ts, vs = series(pr, metric)
        if ts:
            ax.plot([t - t0 for t in ts], vs, color=color, label=label)
    per = d.get("per_second_done_db") or d.get("per_second_done") or []
    ax.plot([r["t_s"] for r in per], [r["done"] for r in per], color=ORANGE,
            alpha=0.55, linewidth=1.0, label="завершено задач за секунду")
    if d.get("poison_at_wall"):
        ax.axvline(d["poison_at_wall"] - t0, color=RED, linestyle="--",
                   label="сценарій Ф3: 10 задач із seed = 0")
    ax.set_title("Крок 9: черга і завершення задач під FAIL_RATE = 0,15\n"
                 f"W = {d['workers']}, λ = {d['rate']:.1f} задач/с"
                 .replace(".", ","))
    ax.set_xlabel("секунда прогону")
    ax.set_ylabel("задач")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=9)
    fig.tight_layout()
    save(fig, "step9_queue" if name == "step9_poison_lo" else f"{name}_queue")


# ---------------------------------------------------------------- кроки 3 і 4

def chart_duplicates():
    labels, vals, colors = [], [], []
    for poll in (50, 1):
        d = load(f"step3_poll{poll}")
        if d:
            labels.append(f"наївне\nT_poll = {poll} мс")
            vals.append(100 * d["D_by_results"])
            colors.append(RED)
    for mode, name in (("skip_locked", "FOR UPDATE\nSKIP LOCKED"),
                       ("for_update", "FOR UPDATE\nбез SKIP LOCKED")):
        d = load(f"step4_{mode}")
        if d:
            labels.append(f"{name}\nT_poll = 1 мс")
            vals.append(100 * d["D_by_results"])
            colors.append(GREEN)
    if not vals:
        return
    fig, ax = plt.subplots(figsize=(7.4, 4.2))
    bars = ax.bar(labels, vals, color=colors)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + max(vals) * 0.02,
                f"{v:.2f} %".replace(".", ","), ha="center", fontsize=9)
    ax.set_title("Частка дублів D за режимом захоплення, W = 4, 300 задач")
    ax.set_ylabel("D, %")
    ax.set_ylim(0, max(vals) * 1.25 if max(vals) else 1)
    ax.grid(alpha=0.25, axis="y")
    fig.tight_layout()
    save(fig, "duplicates")


# ---------------------------------------------------------------- крок 7

def chart_little():
    d = load("step7")
    if not d:
        return
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.0))
    for ax, (tag, title) in zip(axes, (("low", "λ = 0,7 · X(R)"),
                                       ("high", "λ = 1,3 · X(R)"))):
        r = d.get(tag)
        if not r:
            continue
        vals = r.get("L_q_series") or []
        ax.plot(range(len(vals)), vals, color=BLUE, marker="o", markersize=3,
                label="tasks_queued")
        if r.get("L_q_mean") is not None:
            ax.axhline(r["L_q_mean"], color=GREEN, linestyle="--",
                       label=f"середнє L_q = {r['L_q_mean']:.2f}".replace(".", ","))
        if r.get("little_lambda_Wq") is not None:
            ax.axhline(r["little_lambda_Wq"], color=RED, linestyle=":",
                       label=f"λ · W_q = {r['little_lambda_Wq']:.2f}".replace(".", ","))
        ax.set_title(title)
        ax.set_xlabel("знімок у вікні (крок 5 с)")
        ax.set_ylabel("задач у черзі")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8)
    fig.suptitle("Крок 7: довжина черги у вікні аналізу і перевірка L_q = λ · W_q")
    fig.tight_layout()
    save(fig, "little")


# ---------------------------------------------------------------- крок 10

def chart_d5():
    d = load("step10")
    if not d:
        return
    order = [k for k in ("poll100", "listen") if k in d]
    if not order:
        return
    names = {"poll100": "опитування 100 мс", "listen": "LISTEN/NOTIFY"}
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.0))
    p50 = [1000 * d[k]["wait"]["p50_s"] for k in order]
    p99 = [1000 * d[k]["wait"]["p99_s"] for k in order]
    x = range(len(order))
    axes[0].bar([i - 0.18 for i in x], p50, width=0.36, color=BLUE, label="p50")
    axes[0].bar([i + 0.18 for i in x], p99, width=0.36, color=RED, label="p99")
    for i, (a, b) in enumerate(zip(p50, p99)):
        axes[0].text(i - 0.18, a, f"{a:.0f}", ha="center", va="bottom", fontsize=9)
        axes[0].text(i + 0.18, b, f"{b:.0f}", ha="center", va="bottom", fontsize=9)
    axes[0].set_xticks(list(x))
    axes[0].set_xticklabels([names[k] for k in order])
    axes[0].set_ylabel("started_at − created_at, мс")
    axes[0].set_title(f"Час очікування задачі, λ = {d[order[0]]['rate']:g} задач/с")
    axes[0].legend(fontsize=9)
    axes[0].grid(alpha=0.25, axis="y")

    q = [d[k]["idle"]["db_queries_per_s"] for k in order]
    bars = axes[1].bar([names[k] for k in order], q, color=GREEN)
    for b, v in zip(bars, q):
        axes[1].text(b.get_x() + b.get_width() / 2, v, f"{v:.1f}".replace(".", ","),
                     ha="center", va="bottom", fontsize=9)
    axes[1].set_ylabel("запитів до бази за секунду")
    axes[1].set_title("Запити до бази за порожньої черги, W = 4")
    axes[1].grid(alpha=0.25, axis="y")
    fig.suptitle("Крок 10 (Д5): опитування проти LISTEN/NOTIFY")
    fig.tight_layout()
    save(fig, "d5_listen")


if __name__ == "__main__":
    chart_x_w()
    chart_step9("step9_poison_lo")
    chart_step9("step9_poison")
    chart_duplicates()
    chart_little()
    chart_d5()
