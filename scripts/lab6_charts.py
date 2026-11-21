"""Діаграми ЛР6 за ``results/lab6/summary.json``.

Запуск: python -m scripts.lab6_charts --out-dir results/lab6/charts
"""

import argparse
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from lab6.host import utf8_console  # noqa: E402
from lab6.train import VARIANT  # noqa: E402

COLOR = "#2b6cb0"
COLOR2 = "#c05621"
GREY = "#555555"


def spaced(v, digits=0):
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def save(fig, out_dir, name):
    os.makedirs(out_dir, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(os.path.join(out_dir, f"{name}.{ext}"), dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  {name}.png, {name}.svg")


def chart_strong(s, out_dir):
    """X(P) strong scaling проти ідеальної прямої P · X(1) (крок 3 умови)."""
    ps = sorted(int(p) for p in s["strong"])
    x = [s["strong"][str(p)]["X"] for p in ps]
    x1 = s["strong"]["1"]["X"]
    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    ax.plot(ps, [p * x1 for p in ps], ls="--", color=GREY, marker="o", ms=4,
            label="ідеальна пряма P · X(1)")
    ax.plot(ps, x, color=COLOR, marker="o", ms=6, label="виміряна X(P)")
    for p, v in zip(ps, x):
        ax.annotate(spaced(v), (p, v), textcoords="offset points", xytext=(0, -14),
                    ha="center", fontsize=8, color=COLOR)
    for p in ps:
        ax.annotate(spaced(p * x1), (p, p * x1), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=8, color=GREY)
    ax.set_xlabel("процесів P")
    ax.set_ylabel("X, прикладів за секунду")
    ax.set_title(f"Strong scaling, B = {VARIANT['batch']} (варіант {VARIANT['number']})")
    ax.set_xticks(ps)
    ax.set_ylim(0, max(max(x), ps[-1] * x1) * 1.15)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    save(fig, out_dir, "strong_scaling")


def chart_step_parts(s, out_dir):
    """Час кроку: обчислення і не перекритий обчисленням обмін."""
    labels = ["1 процес,\nбатч B", "P_max,\nручна\nсинхронізація", "P_max,\nDDP",
              "P_max,\nDDP, no_sync"]
    keys = ["single_B", "manual", "ddp", "ddp_nosync"]
    total = [s["speed"][k]["t_step_ms"] for k in keys]
    comm = [0.0, s["speed"]["manual"]["t_sync_ms"], s["t_comm_exp_ms"], 0.0]
    base = [t - c for t, c in zip(total, comm)]
    pos = range(len(labels))
    fig, ax = plt.subplots(figsize=(7.0, 4.0))
    ax.bar(pos, base, width=0.55, color=COLOR, label="крок без обміну")
    ax.bar(pos, comm, width=0.55, bottom=base, color=COLOR2,
           label="обмін, не перекритий обчисленням")
    for p, (t, c) in enumerate(zip(total, comm)):
        ax.text(p, t, f"{spaced(t, 2)} мс", ha="center", va="bottom", fontsize=8)
        if not c:
            continue
        if c / t > 0.05:  # смуга обміну достатньо висока, щоб підписати всередині
            ax.text(p, t - c / 2, spaced(c, 2), ha="center", va="center",
                    fontsize=8, color="white")
        else:            # 0,16 мс усередині стовпця не видно — підпис збоку зі стрілкою
            ax.annotate(f"{spaced(c, 2)} мс", (p + 0.28, t - c / 2),
                        xytext=(p + 0.52, t * 0.74), fontsize=8, color=COLOR2,
                        ha="left", va="center",
                        arrowprops=dict(arrowstyle="->", color=COLOR2, lw=0.9))
    ax.set_xticks(list(pos))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("t_step, мс")
    ax.set_title("Час кроку і частка обміну")
    ax.set_ylim(0, max(total) * 1.18)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper right", fontsize=9)
    save(fig, out_dir, "step_parts")


def chart_weak(s, out_dir):
    """S_w(P) weak scaling проти ідеальної одиниці ефективності."""
    ps = sorted(int(p) for p in s["weak"])
    sw = [s["weak"][str(p)]["S_w"] for p in ps]
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    ax.plot(ps, ps, ls="--", color=GREY, marker="o", ms=4, label="ідеальне S_w = P")
    ax.plot(ps, sw, color=COLOR, marker="o", ms=6, label="виміряне S_w")
    for p, v in zip(ps, sw):
        ax.annotate(spaced(v, 3), (p, v), textcoords="offset points", xytext=(0, -14),
                    ha="center", fontsize=8, color=COLOR)
    ax.set_xlabel("процесів P")
    ax.set_ylabel("S_w")
    ax.set_title(f"Weak scaling, b = {VARIANT['batch'] // VARIANT['p_max']}")
    ax.set_xticks(ps)
    ax.set_ylim(0, ps[-1] * 1.15)
    ax.grid(axis="y", alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    save(fig, out_dir, "weak_scaling")


def chart_loss(out_dir, results_dir):
    """Втрата на кроці: прогін з відмовою проти прогону без відмови.

    Втрати обох спроб прогону з відмовою беруться з журналу подій: у `result.json`
    лишається історія лише останньої спроби, бо процес першої завершився викликом
    os._exit(1).
    """
    fail_dir = os.path.join(results_dir, "s5_failure")
    ok_path = os.path.join(results_dir, "s2_train_ddp.json")
    events = os.path.join(fail_dir, "events-rank0.jsonl")
    if not (os.path.exists(events) and os.path.exists(ok_path)):
        return
    with open(ok_path, encoding="utf-8") as f:
        ok = [(h["step"], h["loss"]) for h in json.load(f)["loss_history"]]
    attempts = {}
    for line in open(events, encoding="utf-8"):
        e = json.loads(line)
        if e["kind"] == "step":
            attempts.setdefault(e["attempt"], []).append((e["step"], e["loss"]))
    s_f = VARIANT["fail_step"]
    ck = VARIANT["ckpt_interval"]
    ck_step = ((s_f - 1) // ck) * ck  # остання контрольна точка перед відмовою

    fig, ax = plt.subplots(figsize=(7.4, 4.2))
    ax.plot([p[0] for p in ok], [p[1] for p in ok], color=GREY, lw=2.2, alpha=0.45,
            label="без відмови")
    if 0 in attempts:
        ax.plot([p[0] for p in attempts[0]], [p[1] for p in attempts[0]], color=COLOR2,
                lw=1.2, label="з відмовою, спроба 1 (втрачена)")
    if 1 in attempts:
        ax.plot([p[0] for p in attempts[1]], [p[1] for p in attempts[1]], color=COLOR,
                lw=1.2, ls=":", label="з відмовою, після відновлення")
    top = ax.get_ylim()[1]
    ax.axvline(ck_step, color="#2f855a", lw=1, ls="--")
    ax.annotate(f"контрольна точка,\nкрок {ck_step}", (ck_step, top),
                textcoords="offset points", xytext=(-6, -8), ha="right", va="top",
                fontsize=8, color="#2f855a")
    ax.axvline(s_f, color="#9b2c2c", lw=1, ls="--")
    ax.annotate(f"відмова,\nкрок s_f = {s_f}", (s_f, top), textcoords="offset points",
                xytext=(6, -8), ha="left", va="top", fontsize=8, color="#9b2c2c")
    ax.set_xlabel("крок оптимізатора")
    ax.set_ylabel("втрата на глобальному батчі")
    ax.set_title("Втрата на кроці: відмова і відновлення з контрольної точки")
    ax.set_xlim(0, 80)
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=9)
    save(fig, out_dir, "failure_loss")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results-dir", default="results/lab6")
    ap.add_argument("--out-dir", default="results/lab6/charts")
    a = ap.parse_args(argv)
    utf8_console()
    with open(os.path.join(a.results_dir, "summary.json"), encoding="utf-8") as f:
        s = json.load(f)
    print("діаграми:")
    chart_strong(s, a.out_dir)
    chart_weak(s, a.out_dir)
    chart_step_parts(s, a.out_dir)
    chart_loss(a.out_dir, a.results_dir)


if __name__ == "__main__":
    main()
