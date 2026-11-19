"""Таблиці кроків 7-9 ЛР3: X(N), дисбаланс, параметри закону масштабованості
і порівняння стендів Compose (ЛР2) та Kubernetes (ЛР3).

X(N) береться за визначенням ЛР2, на яке спирається крок 7: найбільша серед сходинок
середня досягнута інтенсивність. Власне правило («найбільша витримана сходинка»:
досягнуто не менше 0,9 заданої і менше 1 % не-2xx) подано окремим стовпцем і
окремою оцінкою закону.

    python scripts\\lab3_tables.py
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")
import io
import json
import math
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent
if str(sys_path) not in _sys.path:
    _sys.path.insert(0, str(sys_path))

from loadgen import prom  # noqa: E402
from loadgen.usl import fit, model  # noqa: E402

ROOT = sys_path
K8S = ROOT / "results" / "lab3"
COMPOSE = ROOT / "results" / "lab2"
REPLICAS = (1, 2, 3, 4)


def load(d, n):
    return json.load(io.open(d / f"N{n}.json", encoding="utf-8"))


def load_series(d, n):
    p = d / f"N{n}_prom.json"
    return json.load(io.open(p, encoding="utf-8")) if p.exists() else None


def ms(q):
    """Значення квантиля з оцінки за гістограмою, мс."""
    if not q:
        return None
    return q["value_s"] * 1000


def fmt(v, digits=1, dash="—"):
    if v is None:
        return dash
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def x_definition(run):
    """X(N) за визначенням: найбільша серед сходинок середня досягнута інтенсивність."""
    best = max(run["steps"], key=lambda s: s["achieved_rps"])
    return {"X": best["achieved_rps"], "sd": best["achieved_sd"], "step": best["step"],
            "target": best["target_rps"], "p99_ms": best["p99_ms"],
            "non2xx_share": best["non2xx_share"]}


def x_held(run):
    """Власне правило: найбільша сходинка, яку стенд витримав."""
    return {"X": run["X"], "sd": run["X_sd"], "step": run["X_step"],
            "target": run["X_target_rps"], "p99_ms": run["X_p99_ms"],
            "non2xx_share": run["X_non2xx_share"]}


def prom_row(run, step):
    return next((p for p in run["prom"] if p.get("step") == step), {})


def throttle_1m(series, pod, window):
    """Допоміжна оцінка частки дроселювання пода з експорту ряду rate(...[1m]):
    середнє значень ряду всередині вікна сходинки. Вікно ряду ширше (1 хв), тому
    це не те саме, що increase за 30 с, і подається лише там, де основного значення немає."""
    if not series:
        return None
    w0, w1 = window
    for s in series["series"].get("throttle_per_pod", []):
        if s["labels"].get("pod") != pod:
            continue
        vals = [float(v) for t, v in s["values"] if w0 <= t <= w1]
        vals = [v for v in vals if not math.isnan(v)]
        return sum(vals) / len(vals) if vals else None
    return None


def throttle_avg_cell(p):
    """Середнє і максимум дроселювання з позначкою, якщо ряд є не для всіх подів."""
    pods = p.get("rps_per_pod", {})
    thr = p.get("throttle_per_pod", {})
    if not thr:
        return "—", "—"
    avg = fmt(p.get("throttle_avg"), 4)
    mx = fmt(p.get("throttle_max"), 4)
    if len(thr) < len(pods):
        note = f" (за {len(thr)} подами з {len(pods)})"
        return avg + note, mx + note
    return avg, mx


def pod_throttle_cell(p, pod, series):
    thr = p.get("throttle_per_pod", {})
    if pod in thr:
        return fmt(thr[pod], 4)
    est = throttle_1m(series, pod, p["window_unix"])
    return ("немає ряду за 30 с" +
            (f"; за rate 1 хв ≈ {fmt(est, 4)}" if est is not None else ""))


def pods_by_node(prom_row_):
    """Розподіл подів по вузлах на момент вимірювання, за kube_pod_info з Prometheus.

    Надійніше за знімок kubectl під час прогону: там ще видно поди, які завершуються
    після зменшення кількості реплік. Якщо Prometheus недоступний, повертає {}.
    """
    pods = set(prom_row_.get("rps_per_pod", {}))
    if not pods:
        return {}
    at = prom_row_["window_unix"][1]
    out = {}
    try:
        rows = prom.query(prom.k8s_url(), 'kube_pod_info{namespace="donors"}', at)
    except Exception:
        return {}
    for lbl, _ in rows:
        if lbl.get("pod") in pods:
            out.setdefault(lbl.get("node", "?"), []).append(lbl["pod"])
    return out


def imbalance_table(w, runs, series, which, title):
    w(title)
    w("| N | Сходинка (задана, rps) | I | Запити за секунду по подах "
      "| Найзавантаженіший: rps / p99, мс / дроселювання "
      "| Найменш завантажений: rps / p99, мс / дроселювання |")
    w("|---|---|---|---|---|---|")
    for n in REPLICAS:
        x = which[n]
        p = prom_row(runs[n], x["step"])
        pods = ", ".join(fmt(v, 0) for v in sorted(p.get("rps_per_pod", {}).values(),
                                                   reverse=True))
        if n == 1:
            w(f"| 1 | {fmt(x['target'], 0)} | 1,000 | {pods} | — | — |")
            continue
        w(f"| {n} | {fmt(x['target'], 0)} | {fmt(p.get('imbalance_I'), 3)} | {pods} "
          f"| {fmt(p.get('busiest_rps'), 0)} / {fmt(ms(p.get('busiest_p99')), 1)} "
          f"/ {pod_throttle_cell(p, p.get('busiest_pod'), series[n])} "
          f"| {fmt(p.get('idlest_rps'), 0)} / {fmt(ms(p.get('idlest_p99')), 1)} "
          f"/ {pod_throttle_cell(p, p.get('idlest_pod'), series[n])} |")
    w("")


def fit_section(w, xs, f, title):
    w(title)
    w("| N | X(N), rps | C(N) | y = N/C(N) − 1 | u = N − 1 | v = N(N − 1) |")
    w("|---|---|---|---|---|---|")
    w(f"| 1 | {fmt(xs[1], 1)} | 1,000 | — | — | — |")
    for r in f["rows"]:
        w(f"| {r['N']} | {fmt(xs[r['N']], 1)} | {fmt(r['C'], 3)} | {fmt(r['y'], 4)} "
          f"| {r['u']} | {r['v']} |")
    w("")
    tp = f.get("two_param") or {}
    if tp:
        w(f"Двопараметрична оцінка: σ = {fmt(tp['sigma'], 4)}, κ = {fmt(tp['kappa'], 4)}.  ")
    w(f"λ = X(1) = {fmt(f['lambda'], 1)} rps, σ = {fmt(f['sigma'], 4)}, "
      f"κ = {fmt(f['kappa'], 4)}  ")
    if f.get("N_star"):
        w(f"N* = √((1 − σ) / κ) = {fmt(f['N_star'], 2)}, "
          f"X(N*) ≈ {fmt(f['X_at_N_star'], 0)} rps")
    elif f.get("X_limit"):
        w(f"κ = 0, максимуму немає; межа λ / σ = {fmt(f['X_limit'], 0)} rps")
    if f.get("note"):
        w(f"\nПримітка: {f['note']}")
    w("")
    w("| N | Виміряно, rps | Закон, rps | Різниця, % |")
    w("|---|---|---|---|")
    for n in REPLICAS:
        mv = model(n, f["lambda"], f["sigma"], f["kappa"])
        w(f"| {n} | {fmt(xs[n], 1)} | {fmt(mv, 1)} | "
          f"{'+' if mv >= xs[n] else ''}{fmt((mv - xs[n]) / xs[n] * 100, 1)} |")
    w("")


def main():
    runs = {n: load(K8S, n) for n in REPLICAS}
    series = {n: load_series(K8S, n) for n in REPLICAS}
    xd = {n: x_definition(runs[n]) for n in REPLICAS}
    xh = {n: x_held(runs[n]) for n in REPLICAS}
    x_def = {n: xd[n]["X"] for n in REPLICAS}
    x_hld = {n: xh[n]["X"] for n in REPLICAS}
    f_def = fit(x_def)
    f_hld = fit(x_hld)

    compose = {}
    f_comp = None
    if (COMPOSE / "N1.json").exists():
        compose = {n: load(COMPOSE, n)["X"] for n in REPLICAS}
        f_comp = fit(compose)

    out = []
    w = out.append

    w("# Таблиці кроків 7-9 (варіант 19, кластер kind)\n")
    w("Сходинка 30 с, середні за останні 20 с, 32 постійних зʼєднання з закриттям після")
    w("100 запитів. **X(N) за визначенням** — найбільша серед сходинок середня досягнута")
    w("інтенсивність (визначення ЛР2). **Власне правило** — найбільша сходинка, яку стенд")
    w("витримав: досягнуто не менше 0,9 заданої і менше 1 % відповідей не 2xx.\n")

    # ---------------------------------------------------------------- крок 7
    w("## Крок 7. Пропускна здатність X(N)\n")
    w("| N | X(N), rps | Розкид, rps | Сходинка (задана, rps) | p99 клієнта, мс "
      "| p99 сервера, мс | Не-2xx, % | Дроселювання сер. | Дроселювання макс. "
      "| Власне правило: X, rps (сходинка, не-2xx %) |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for n in REPLICAS:
        x, h = xd[n], xh[n]
        p = prom_row(runs[n], x["step"])
        avg, mx = throttle_avg_cell(p)
        w(f"| {n} | {fmt(x['X'], 1)} | {fmt(x['sd'], 1)} | {fmt(x['target'], 0)} "
          f"| {fmt(x['p99_ms'], 0)} | {fmt(ms(p.get('server_p99')), 1)} "
          f"| {fmt((x['non2xx_share'] or 0) * 100, 2)} | {avg} | {mx} "
          f"| {fmt(h['X'], 1)} ({fmt(h['target'], 0)}, "
          f"{fmt((h['non2xx_share'] or 0) * 100, 2)}) |")
    w("")
    w("Дроселювання на сходинках власного правила:\n")
    w("| N | Сходинка (задана, rps) | Дроселювання сер. | Дроселювання макс. |")
    w("|---|---|---|---|")
    for n in REPLICAS:
        p = prom_row(runs[n], xh[n]["step"])
        avg, mx = throttle_avg_cell(p)
        w(f"| {n} | {fmt(xh[n]['target'], 0)} | {avg} | {mx} |")
    w("")
    livez = K8S / "ladder_livez.json"
    g = json.load(io.open(livez, encoding="utf-8"))["X"] if livez.exists() else 700.0
    w(f"Межа стенду без бази (ступінчастий прогін на `/livez`): **{fmt(g, 0)} rps**; "
      f"найбільша X(N) за визначенням це {fmt(max(x_def.values()) / g * 100, 0)} % цієї межі.\n")

    # ---------------------------------------------------------------- крок 8
    imbalance_table(w, runs, series, xd,
                    "## Крок 8. Коефіцієнт дисбалансу навантаження\n\n"
                    "I = X_max / X_avg на сходинці, де досягнуто X(N) за визначенням.\n")
    imbalance_table(w, runs, series, xh,
                    "Те саме на сходинках власного правила:\n")

    # ---------------------------------------------------------------- Д1
    w("## Додаткова вимога Д1. Розподіл подів по робочих вузлах\n")
    w("На сходинці X(N) за визначенням.\n")
    w("| N | Поди по вузлах | Запити за секунду на вузол |")
    w("|---|---|---|")
    for n in REPLICAS:
        p = prom_row(runs[n], xd[n]["step"])
        by_node = pods_by_node(p) or runs[n].get("pods_by_node", {})
        pods_s = ", ".join(f"{k} = {len(v)}" for k, v in sorted(by_node.items()))
        rps_s = ", ".join(f"{k} = {fmt(v, 0)}" for k, v in
                          sorted(p.get("rps_per_node", {}).items()))
        w(f"| {n} | {pods_s} | {rps_s} |")
    w("")

    # ---------------------------------------------------------------- крок 9
    w("## Крок 9. Закон масштабованості\n")
    w("Лінеаризована форма N / C(N) − 1 = σ(N − 1) + κN(N − 1), метод найменших квадратів")
    w("без вільного члена.\n")
    fit_section(w, x_def, f_def, "### За X(N) за визначенням\n")
    fit_section(w, x_hld, f_hld, "### За власним правилом\n")

    # ---------------------------------------------------------------- порівняння стендів
    if f_comp:
        w("## Порівняння стендів: Compose (ЛР2) і Kubernetes (ЛР3)\n")
        w("В обох стовпцях X(N) за визначенням ЛР2.\n")
        w("| N | X(N) Compose, rps | C(N) Compose | X(N) Kubernetes, rps | C(N) Kubernetes |")
        w("|---|---|---|---|---|")
        for n in REPLICAS:
            w(f"| {n} | {fmt(compose[n], 1)} | {fmt(compose[n] / compose[1], 3)} "
              f"| {fmt(x_def[n], 1)} | {fmt(x_def[n] / x_def[1], 3)} |")
        w("")
        w("| Параметр | Compose | Kubernetes |")
        w("|---|---|---|")
        w(f"| λ = X(1), rps | {fmt(f_comp['lambda'], 1)} | {fmt(f_def['lambda'], 1)} |")
        w(f"| σ | {fmt(f_comp['sigma'], 4)} | {fmt(f_def['sigma'], 4)} |")
        w(f"| κ | {fmt(f_comp['kappa'], 4)} | {fmt(f_def['kappa'], 4)} |")
        w(f"| N* | {fmt(f_comp.get('N_star'), 2)} | {fmt(f_def.get('N_star'), 2)} |")
        w(f"| X(N*), rps | {fmt(f_comp.get('X_at_N_star'), 0)} "
          f"| {fmt(f_def.get('X_at_N_star'), 0)} |")
        w("")

    path = K8S / "tables.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"{path}")
    (K8S / "usl.json").write_text(
        json.dumps({"k8s": {"X": x_def, "fit": f_def, "rule": "definition",
                            "steps": {n: xd[n]["step"] for n in REPLICAS}},
                    "k8s_held": {"X": x_hld, "fit": f_hld, "rule": "held",
                                 "steps": {n: xh[n]["step"] for n in REPLICAS}},
                    "compose": {"X": compose, "fit": f_comp}},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"{K8S / 'usl.json'}")


if __name__ == "__main__":
    main()
