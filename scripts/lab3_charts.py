"""Діаграми ЛР3: кроки 8 і 9 (запити за секунду по подах і C(N) двох стендів).

Потрібен matplotlib: pip install -r requirements-dev.txt

    python scripts\\lab3_charts.py
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import csv
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
from scripts.lab3_tables import x_definition  # noqa: E402

ROOT = sys_path
K8S = ROOT / "results" / "lab3"
COMPOSE = ROOT / "results" / "lab2"
OUT = K8S / "charts"
REPLICAS = (1, 2, 3, 4)


def load(d, n):
    return json.load(io.open(d / f"N{n}.json", encoding="utf-8"))


def step_at_x(run):
    return next((p for p in run["prom"] if p.get("step") == run["X_step"]), {})


def chart_pods(runs):
    """Крок 8: кількість запитів за секунду для кожного пода за R реплік."""
    p = step_at_x(runs[4])
    pods = sorted(p["rps_per_pod"].items(), key=lambda kv: -kv[1])
    names = [k.rsplit("-", 1)[-1] for k, _ in pods]
    vals = [v for _, v in pods]
    avg = sum(vals) / len(vals)

    fig, ax = plt.subplots(figsize=(7, 4.2))
    bars = ax.bar(names, vals, color="#4c72b0")
    ax.axhline(avg, color="#c44e52", linestyle="--",
               label=f"середнє {avg:.0f} rps")
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 3, f"{v:.0f}",
                ha="center", fontsize=9)
    ax.set_title(f"Запити за секунду по подах, R = 4 реплік\n"
                 f"задана інтенсивність {p['target_rps']:.0f} rps, "
                 f"I = {p['imbalance_I']:.2f}")
    ax.set_xlabel("под web")
    ax.set_ylabel("запитів за секунду")
    ax.set_ylim(0, max(vals) * 1.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "rps_per_pod_N4.png", dpi=140)
    plt.close(fig)


def chart_cn(runs, compose):
    """Крок 9: C(N) з розкидом для обох стендів і криві закону."""
    x_k = {n: runs[n]["X"] for n in REPLICAS}
    sd_k = {n: runs[n]["X_sd"] for n in REPLICAS}
    f_k = fit(x_k)

    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    ns = [n for n in REPLICAS]
    xs = [i / 10 for i in range(10, 61)]

    c_k = [x_k[n] / x_k[1] for n in ns]
    e_k = [sd_k[n] / x_k[1] for n in ns]
    ax.errorbar(ns, c_k, yerr=e_k, fmt="o", color="#4c72b0", capsize=4,
                label="Kubernetes, вимірювання")
    ax.plot(xs, [model(x, f_k["lambda"], f_k["sigma"], f_k["kappa"]) / x_k[1] for x in xs],
            color="#4c72b0",
            label=f"Kubernetes: σ = {f_k['sigma']:.3f}, κ = {f_k['kappa']:.4f}")

    if compose:
        x_c = {n: compose[n]["X"] for n in REPLICAS}
        sd_c = {n: compose[n]["X_sd"] for n in REPLICAS}
        f_c = fit(x_c)
        ax.errorbar(ns, [x_c[n] / x_c[1] for n in ns],
                    yerr=[sd_c[n] / x_c[1] for n in ns], fmt="s", color="#dd8452",
                    capsize=4, label="Compose (ЛР2), вимірювання")
        ax.plot(xs, [model(x, f_c["lambda"], f_c["sigma"], f_c["kappa"]) / x_c[1] for x in xs],
                color="#dd8452",
                label=f"Compose: σ = {f_c['sigma']:.3f}, κ = {f_c['kappa']:.4f}")

    ax.plot(xs, xs, color="#999999", linestyle=":", label="ідеальне масштабування")
    if f_k.get("N_star") and f_k["N_star"] <= 6.0:
        ax.axvline(f_k["N_star"], color="#4c72b0", linestyle="-.", alpha=0.6)
        ax.text(f_k["N_star"] + 0.05, 0.4, f"N* = {f_k['N_star']:.2f}",
                color="#4c72b0", fontsize=9)
    elif f_k.get("N_star"):
        ax.text(4.3, 0.4, f"Kubernetes: N* = {f_k['N_star']:.1f}, поза віссю",
                color="#4c72b0", fontsize=9)
    ax.set_title("Відносна пропускна здатність C(N) = X(N) / X(1)")
    ax.set_xlabel("кількість реплік N")
    ax.set_ylabel("C(N)")
    ax.set_xlim(0.8, 6.2)
    ax.set_ylim(0, 4.2)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8.5, loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "cn_compose_vs_k8s.png", dpi=140)
    plt.close(fig)


def chart_x_abs(runs, compose):
    """Додатково: абсолютні X(N) двох стендів — видно різницю шляху доставки."""
    fig, ax = plt.subplots(figsize=(7, 4.2))
    ns = list(REPLICAS)
    w = 0.38
    k = [runs[n]["X"] for n in ns]
    ax.bar([n - w / 2 for n in ns], k, width=w, color="#4c72b0", label="Kubernetes (kind)")
    if compose:
        c = [compose[n]["X"] for n in ns]
        ax.bar([n + w / 2 for n in ns], c, width=w, color="#dd8452", label="Compose (ЛР2)")
        for n, v in zip(ns, c):
            ax.text(n + w / 2, v + 10, f"{v:.0f}", ha="center", fontsize=8.5)
    for n, v in zip(ns, k):
        ax.text(n - w / 2, v + 10, f"{v:.0f}", ha="center", fontsize=8.5)
    ax.set_title("Пропускна здатність X(N) на двох стендах")
    ax.set_xlabel("кількість реплік N")
    ax.set_ylabel("rps")
    ax.set_xticks(ns)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "x_compose_vs_k8s.png", dpi=140)
    plt.close(fig)


def load_profile(name):
    run = json.load(io.open(K8S / f"{name}.json", encoding="utf-8"))
    with (K8S / f"{name}.csv").open(encoding="utf-8") as f:
        rows = [{k: (float(v) if v not in ("", None) else None) for k, v in r.items()}
                for r in csv.DictReader(f)]
    series = json.load(io.open(K8S / f"{name}_prom.json", encoding="utf-8"))
    return run, rows, series


def chart_elasticity():
    """Крок 12: по чотири діаграми на кожен прогін, однакові шкали осей."""
    el = json.load(io.open(K8S / "elasticity.json", encoding="utf-8"))
    runs = {"fixed": load_profile("profile_fixed"), "hpa": load_profile("profile_hpa")}
    titles = {"fixed": "Фіксовані R = 4 репліки (крок 10)",
              "hpa": "HorizontalPodAutoscaler (крок 11)"}
    x_by_n = {int(k): v for k, v in
              json.load(io.open(K8S / "usl.json", encoding="utf-8"))["k8s"]["X"].items()}

    total = int(sum(x["duration_s"] for x in runs["fixed"][0]["segments"]))
    s_by = {"fixed": el["s_fixed"], "hpa": el["s_hpa"]}

    def demand(z):
        for n in range(1, 5):
            if x_by_n[n] >= z:
                return n
        return 4

    # межі шкал спільні для обох прогонів
    p99_max = max(r["p99_ms"] or 0 for _, rows, _ in runs.values() for r in rows[:total])
    rps_max = max(max(max(r["target_rps"], r["achieved_rps"]) for r in rows[:total])
                  for _, rows, _ in runs.values())

    fig, axes = plt.subplots(4, 2, figsize=(12.5, 11), sharex=True)
    for col, key in enumerate(("fixed", "hpa")):
        run, rows, series = runs[key]
        rows = rows[:total]
        t = [r["t_s"] for r in rows]
        start = run["start_wall"]

        ax = axes[0][col]
        ax.plot(t, [r["target_rps"] for r in rows], color="#999999", linestyle="--",
                label="задана")
        ax.plot(t, [r["achieved_rps"] for r in rows], color="#4c72b0", label="досягнута")
        ax.set_title(titles[key])
        ax.set_ylabel("rps")
        ax.set_ylim(0, rps_max * 1.15)
        ax.legend(fontsize=8)

        ax = axes[1][col]
        ax.plot(t, [r["p99_ms"] for r in rows], color="#c44e52")
        ax.axhline(el["threshold_ms"], color="#999999", linestyle=":",
                   label=f"поріг 2·p99₀ = {el['threshold_ms']:.0f} мс")
        ax.set_ylabel("p99 клієнта, мс")
        ax.set_yscale("log")
        ax.set_ylim(5, max(p99_max * 1.5, 100))
        ax.legend(fontsize=8)

        ax = axes[2][col]
        ax.step(t, [demand(r["target_rps"]) for r in rows], where="post",
                color="#dd8452", label="d(t), потрібно")
        ax.step(t, [s_by[key][i] if i < len(s_by[key]) else None for i in range(len(t))],
                where="post", color="#4c72b0", label="s(t), готово")
        ax.set_ylabel("реплік")
        ax.set_ylim(0, 5)
        ax.legend(fontsize=8)

        ax = axes[3][col]
        util = {}
        for item in series["series"].get("cpu_utilization", []):
            for ts, v in item["values"]:
                sec = int(round(ts - start))
                if 0 <= sec < total:
                    util[sec] = v * 100
        ax.plot(sorted(util), [util[k] for k in sorted(util)], color="#55a868")
        ax.axhline(50, color="#999999", linestyle=":", label="ціль HPA 50 %")
        ax.set_ylabel("CPU до requests, %")
        ax.set_ylim(0, 140)
        ax.set_xlabel("секунда прогону")
        ax.legend(fontsize=8)

        for ax in axes[:, col]:
            ax.axvspan(run["segments"][0]["duration_s"],
                       run["segments"][0]["duration_s"] + run["segments"][1]["duration_s"],
                       color="#f0f0f0", zorder=0)
            ax.grid(alpha=0.3)

    fig.suptitle("Крок 12: прогони за профілем навантаження, однакові шкали осей")
    fig.tight_layout()
    fig.savefig(OUT / "elasticity.png", dpi=140)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    runs = {n: load(K8S, n) for n in REPLICAS}
    # X(N) за визначенням ЛР2 (найбільша серед сходинок середня досягнута), як у
    # таблицях кроків 7-9; у Compose (ЛР2) X уже за цим визначенням
    for n in REPLICAS:
        d = x_definition(runs[n])
        runs[n]["X"], runs[n]["X_sd"], runs[n]["X_step"] = d["X"], d["sd"], d["step"]
    compose = ({n: load(COMPOSE, n) for n in REPLICAS}
               if (COMPOSE / "N1.json").exists() else None)
    chart_pods(runs)
    chart_cn(runs, compose)
    chart_x_abs(runs, compose)
    if (K8S / "elasticity.json").exists():
        chart_elasticity()
    for f in sorted(OUT.glob("*.png")):
        print(f)


if __name__ == "__main__":
    main()
