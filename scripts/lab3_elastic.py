"""Крок 12 ЛР3: метрики еластичності двох прогонів за профілем і часові показники HPA.

Рахує за методикою SPEC Research Group:
    d(t) = min{N ≤ R : X(N) ≥ z(t)},  s(t) = кількість готових подів,
    θ_U, θ_O — частки часу нестачі і надлишку реплік,
    a_U, a_O — середня нестача і середній надлишок,
    W = Σ s(t) — витрачені репліко-секунди,
а також кількість секунд порушення порога (посекундний p99 > 2·p99₀) і не-2xx.

    python scripts\\lab3_elastic.py
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import csv
import io
import json
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent
if str(sys_path) not in _sys.path:
    _sys.path.insert(0, str(sys_path))

ROOT = sys_path
K8S = ROOT / "results" / "lab3"
R = 4
REQUESTS_CPU = 0.5


def load_run(name):
    run = json.load(io.open(K8S / f"{name}.json", encoding="utf-8"))
    rows = []
    with (K8S / f"{name}.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: (float(v) if v not in ("", None) else None)
                         for k, v in r.items()})
    series = json.load(io.open(K8S / f"{name}_prom.json", encoding="utf-8"))
    return run, rows, series


def series_at(series, name, start_wall, total_s, how="sum"):
    """Ряд Prometheus у вигляді списку значень по секундах прогону (0..total_s)."""
    items = series["series"].get(name, [])
    out = [None] * int(total_s)
    for item in items:
        for t, v in item["values"]:
            sec = int(round(t - start_wall))
            if 0 <= sec < len(out):
                if out[sec] is None:
                    out[sec] = v
                elif how == "sum":
                    out[sec] += v
                elif how == "max":
                    out[sec] = max(out[sec], v)
    # між точками збору значення вважається сталим
    last = None
    for i, v in enumerate(out):
        if v is None:
            out[i] = last
        else:
            last = v
    return out


def demand(z, x_by_n):
    """d(t): найменша кількість реплік, чия пропускна здатність покриває z."""
    for n in range(1, R + 1):
        if x_by_n[n] >= z:
            return n
    return R


def elasticity(rows, s, x_by_n, p99_threshold_ms):
    T = len(rows)
    theta_u = theta_o = a_u = a_o = 0
    w = 0.0
    viol = 0
    non2xx = 0
    for i, r in enumerate(rows):
        d = demand(r["target_rps"], x_by_n)
        si = s[i] if i < len(s) and s[i] is not None else 0
        if si < d:
            theta_u += 1
            a_u += d - si
        elif si > d:
            theta_o += 1
            a_o += si - d
        w += si
        if r["p99_ms"] is not None and r["p99_ms"] > p99_threshold_ms:
            viol += 1
        non2xx += int(r["non2xx"] or 0)
    return {"T": T, "theta_U": theta_u / T, "theta_O": theta_o / T,
            "a_U": a_u / T, "a_O": a_o / T, "W": w,
            "threshold_violation_s": viol, "non2xx": non2xx}


def hpa_timings(run, rows, series, spike_begin, spike_end, p99_spike_fixed_ms):
    """Чотири часові показники кроку 11."""
    start = run["start_wall"]
    total = int(sum(s["duration_s"] for s in run["segments"]))
    desired = series_at(series, "hpa_desired", start, total, "max")
    ready = series_at(series, "replicas_ready", start, total, "max")
    out = {}

    base = next((v for v in desired[:int(spike_begin) + 1] if v is not None), None)
    out["t_first_desired_change"] = next(
        (t - spike_begin for t in range(int(spike_begin), total)
         if desired[t] is not None and base is not None and desired[t] != base), None)

    peak = max((v for v in ready if v is not None), default=None)
    out["peak_ready"] = peak
    out["t_peak_ready"] = next(
        (t - spike_begin for t in range(int(spike_begin), total)
         if ready[t] == peak), None) if peak else None

    limit = 1.2 * p99_spike_fixed_ms
    run_len = 0
    out["t_p99_recovered"] = None
    for t in range(int(spike_begin), total):
        p = rows[t]["p99_ms"]
        if p is not None and p < limit:
            run_len += 1
            if run_len >= 10:
                out["t_p99_recovered"] = (t - 9) - spike_begin
                break
        else:
            run_len = 0

    after = next((v for v in ready[int(spike_end):] if v is not None), None)
    out["t_first_scale_down"] = next(
        (t - spike_end for t in range(int(spike_end), total)
         if ready[t] is not None and after is not None and ready[t] < after), None)
    out["p99_limit_ms"] = limit
    return out


def fmt(v, digits=2, dash="не досягнуто"):
    return dash if v is None else f"{v:.{digits}f}"


def main():
    usl = json.load(io.open(K8S / "usl.json", encoding="utf-8"))
    # X(N) за визначенням (основне) і за власним правилом (окремо), див. lab3_tables.py
    x_by_n = {int(k): v for k, v in usl["k8s"]["X"].items()}
    x_held = {int(k): v for k, v in usl.get("k8s_held", usl["k8s"])["X"].items()}

    fixed, f_rows, f_series = load_run("profile_fixed")
    hpa, h_rows, h_series = load_run("profile_hpa")
    total = int(sum(s["duration_s"] for s in fixed["segments"]))
    spike_begin = fixed["segments"][0]["duration_s"]
    spike_end = spike_begin + fixed["segments"][1]["duration_s"]

    # p99₀ — p99 клієнта на першому відрізку прогону без автомасштабування
    p99_0 = fixed["segment_stats"][0]["p99_ms_mean"]
    p99_spike_fixed = fixed["segment_stats"][1]["p99_ms_mean"]
    threshold = 2 * p99_0

    f_s = series_at(f_series, "replicas_ready", fixed["start_wall"], total, "max")
    h_s = series_at(h_series, "replicas_ready", hpa["start_wall"], total, "max")
    f_m = elasticity(f_rows[:total], f_s, x_by_n, threshold)
    h_m = elasticity(h_rows[:total], h_s, x_by_n, threshold)
    f_mh = elasticity(f_rows[:total], f_s, x_held, threshold)
    h_mh = elasticity(h_rows[:total], h_s, x_held, threshold)
    base_z = fixed["segments"][0]["target_rps"]
    spike_z = fixed["segments"][1]["target_rps"]
    level_def = min(1.5 * x_by_n[1], 0.9 * x_by_n[R])
    level_held = min(1.5 * x_held[1], 0.9 * x_held[R])
    timings =hpa_timings(hpa, h_rows, h_series, spike_begin, spike_end, p99_spike_fixed)

    out = []
    w = out.append
    w("# Крок 12. Метрики еластичності (варіант 19)\n")
    w(f"Профіль: {int(fixed['segments'][0]['duration_s'])} с на "
      f"{fixed['segments'][0]['target_rps']:.0f} rps (0,3·X(1)), "
      f"{int(fixed['segments'][1]['duration_s'])} с на "
      f"{fixed['segments'][1]['target_rps']:.0f} rps "
      f"(стрибок = min(1,5·X(1), 0,9·X(4)) за власним правилом X(N), чинним на момент "
      f"прогонів: {level_held:.0f} rps), "
      f"{int(fixed['segments'][2]['duration_s'])} с на "
      f"{fixed['segments'][2]['target_rps']:.0f} rps. T = {total} с.\n")
    w(f"За X(N) за визначенням рівні профілю були б: база 0,3·X(1) = "
      f"{0.3 * x_by_n[1]:.0f} rps, стрибок min(1,5·X(1), 0,9·X(4)) = {level_def:.0f} rps. "
      f"Прогонів із цими рівнями немає.\n")
    w(f"Потрібна кількість реплік d(t) за X(N) серії кроку 7 за визначенням: "
      + ", ".join(f"X({n}) = {x_by_n[n]:.0f}" for n in sorted(x_by_n))
      + f". На базовому рівні d = {demand(base_z, x_by_n)}, "
      f"на стрибку d = {demand(spike_z, x_by_n)}. За власним правилом: "
      + ", ".join(f"X({n}) = {x_held[n]:.0f}" for n in sorted(x_held))
      + f"; d = {demand(base_z, x_held)} і {demand(spike_z, x_held)}.\n")
    w(f"Поріг порушення: 2·p99₀ = {threshold:.0f} мс, де p99₀ = {p99_0:.0f} мс — p99 клієнта")
    w("на першому відрізку прогону без автомасштабування.\n")

    w("## Досягнута інтенсивність і затримка за відрізками\n")
    w("| Відрізок | Задано, rps | Фіксовані репліки: досягнуто / p99, мс / не-2xx "
      "| HPA: досягнуто / p99, мс / не-2xx |")
    w("|---|---|---|---|")
    for i, (fs, hs) in enumerate(zip(fixed["segment_stats"], hpa["segment_stats"])):
        w(f"| {i} | {fs['target_rps']:.0f} "
          f"| {fs['achieved_rps']:.1f} / {fmt(fs['p99_ms_mean'], 0, '—')} / {fs['non2xx']} "
          f"| {hs['achieved_rps']:.1f} / {fmt(hs['p99_ms_mean'], 0, '—')} / {hs['non2xx']} |")
    w("")

    w("## Метрики еластичності\n")
    w("| Метрика | Фіксовані R = 4 | HPA |")
    w("|---|---|---|")
    w(f"| θ_U, частка часу нестачі реплік | {f_m['theta_U']:.3f} | {h_m['theta_U']:.3f} |")
    w(f"| θ_O, частка часу надлишку реплік | {f_m['theta_O']:.3f} | {h_m['theta_O']:.3f} |")
    w(f"| a_U, середня нестача реплік | {f_m['a_U']:.3f} | {h_m['a_U']:.3f} |")
    w(f"| a_O, середній надлишок реплік | {f_m['a_O']:.3f} | {h_m['a_O']:.3f} |")
    w(f"| W, репліко-секунди | {f_m['W']:.0f} | {h_m['W']:.0f} |")
    w(f"| Секунд порушення порога ({threshold:.0f} мс) "
      f"| {f_m['threshold_violation_s']} | {h_m['threshold_violation_s']} |")
    w(f"| Відповідей не 2xx | {f_m['non2xx']} | {h_m['non2xx']} |")
    w("")
    w("Ті самі метрики, якщо d(t) рахувати за власним правилом X(N) "
      "(W, порушення порога і не-2xx від d(t) не залежать):\n")
    w("| Метрика | Фіксовані R = 4 | HPA |")
    w("|---|---|---|")
    w(f"| θ_U | {f_mh['theta_U']:.3f} | {h_mh['theta_U']:.3f} |")
    w(f"| θ_O | {f_mh['theta_O']:.3f} | {h_mh['theta_O']:.3f} |")
    w(f"| a_U | {f_mh['a_U']:.3f} | {h_mh['a_U']:.3f} |")
    w(f"| a_O | {f_mh['a_O']:.3f} | {h_mh['a_O']:.3f} |")
    w("")

    w("## Крок 11. Часові показники HPA\n")
    w("| Показник | Значення, с |")
    w("|---|---|")
    w(f"| Від початку стрибка до першої зміни бажаної кількості реплік "
      f"| {fmt(timings['t_first_desired_change'], 0)} |")
    w(f"| Від початку стрибка до готовності найбільшої кількості реплік "
      f"({timings['peak_ready']:.0f}) | {fmt(timings['t_peak_ready'], 0)} |")
    w(f"| Від початку стрибка до стабільного p99 нижче "
      f"{timings['p99_limit_ms']:.0f} мс протягом 10 с "
      f"| {fmt(timings['t_p99_recovered'], 0)} |")
    w(f"| Від кінця стрибка до першого зменшення кількості готових реплік "
      f"| {fmt(timings['t_first_scale_down'], 0)} |")
    w("")

    path = K8S / "elasticity.md"
    path.write_text("\n".join(out), encoding="utf-8")
    (K8S / "elasticity.json").write_text(json.dumps(
        {"threshold_ms": threshold, "p99_0_ms": p99_0,
         "p99_spike_fixed_ms": p99_spike_fixed,
         "fixed": f_m, "hpa": h_m, "fixed_held": f_mh, "hpa_held": h_mh,
         "level_def_rps": level_def, "level_held_rps": level_held, "timings": timings,
         "s_fixed": f_s, "s_hpa": h_s}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(path)
    print(K8S / "elasticity.json")
    print("\n".join(out[out.index("## Метрики еластичності\n") if
                        "## Метрики еластичності\n" in out else 0:]))


if __name__ == "__main__":
    main()
