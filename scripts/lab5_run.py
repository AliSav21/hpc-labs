"""Оркестратор ЛР5: прогони кроків 1-5 за варіантом 19 і таблиці 1-6.

Кожна конфігурація знімається тричі по D = 10 с після прогріву 1 с; у таблиці йде
медіана трьох прогонів: кожна виміряна величина — медіана цієї величини за прогонами,
похідні величини — за формулами умови з цих медіан (``median_view``).

Запуск:  python -m scripts.lab5_run --out-dir results/lab5
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time

from lab5 import bench

VARIANT = {"structure": "С9", "n": 6, "parameter": 1024}


# ---------------------------------------------------------------- форматування


def num(v, digits=2):
    """Число з комою як десятковим розділювачем і пробілом між тисячами."""
    if v is None:
        return "—"
    if isinstance(v, int) or (isinstance(v, float) and digits == 0):
        s = f"{round(v):,}".replace(",", " ")
        return s
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s


def ratio(rw, g, digits=3):
    """Відношення RW / G; якщо значення під глобальним замком нульове — різниця RW − G."""
    if g in (None, 0) or rw is None:
        if rw is None or g is None:
            return "—"
        return f"RW − G = {num(rw - g, digits)}"
    return num(rw / g, digits)


# ---------------------------------------------------------------- прогони


GATE = {"max_bg": 10.0, "max_wait_s": 60.0, "require_ac": True}


def wait_quiet(label):
    """Не починати прогін, доки машина не в керованому стані.

    На батареї або в режимі економії заряду серія зупиняється зовсім: абсолютні X
    на ноутбуці в таких станах відрізняються в рази. Фонове завантаження процесора
    іншими програмами чекаємо до порогу ``max_bg`` %; якщо за ``max_wait_s`` нижче
    не опустилось, прогін іде, а завантаження записується в його умови.
    """
    if sys.platform != "win32":
        return
    t0 = time.time()
    while True:
        c = bench.run_conditions()
        if GATE["require_ac"] and (c.get("ac_power") is False or c.get("battery_saver")):
            raise SystemExit(f"{label}: ноутбук не від мережі або ввімкнено економію заряду "
                             f"({c}); серію зупинено")
        bg = c.get("background_cpu_percent")
        if bg is None or bg <= GATE["max_bg"]:
            return
        if time.time() - t0 > GATE["max_wait_s"]:
            print(f"  {label}: фон {bg} % не опустився до {GATE['max_bg']} % за "
                  f"{GATE['max_wait_s']:.0f} с, прогін іде з записом умов", flush=True)
            return
        print(f"  {label}: фонове завантаження {bg} %, чекаю", flush=True)


def repeat(fn, times, label):
    runs = []
    for i in range(1, times + 1):
        wait_quiet(f"{label}, прогін {i}")
        print(f"  {label}: прогін {i} з {times}", flush=True)
        runs.append(fn())
        time.sleep(0.5)
    return runs


ROLE_KEYS = ("ops", "X", "x_per_thread", "p50_us", "p99_us", "wait_max_ms", "w", "cv",
             "wait_sum_s", "mu", "sigma", "threads")
TOTAL_KEYS = ("p50_us", "p99_us", "wait_max_ms", "w", "cv")


def _med(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def median_view(runs, kind):
    """Медіана трьох прогонів за умовою: кожна величина таблиці — медіана цієї самої
    величини за прогонами. Похідні величини рахуються з медіан за формулами умови:
    X = X_w + X_r, φ = X_роль / X, h = p99 / p50, x = X / n_ролі, t̄_cs і 1 / t̄_cs."""
    if kind == "cs":
        t_w = _med(r["t_cs_w_us"] for r in runs)
        t_r = _med(r["t_cs_r_us"] for r in runs)
        r0 = runs[0]
        t_bar = (r0["n_w"] * t_w + r0["n_r"] * t_r) / r0["n"]
        return {"mode": "cs", "lock": r0["lock"], "n": r0["n"], "n_w": r0["n_w"],
                "n_r": r0["n_r"], "t_cs_w_us": t_w, "t_cs_r_us": t_r, "t_cs_bar_us": t_bar,
                "x_limit": 1e6 / t_bar, "samples_w": _med(r["samples_w"] for r in runs),
                "samples_r": _med(r["samples_r"] for r in runs), "host": r0["host"]}
    if kind == "single":
        return {"mode": "single", "lock": runs[0]["lock"], "host": runs[0]["host"],
                "total": {"X1": _med(r["total"]["X1"] for r in runs)}}
    r0 = runs[0]
    out = {k: r0[k] for k in ("mode", "lock", "n", "n_w", "n_r", "capacity", "write_mix",
                              "host") if k in r0}
    out["duration_s"] = _med(r["duration_s"] for r in runs)
    for role in ("write", "read"):
        st = {k: _med(r[role][k] for r in runs) for k in ROLE_KEYS if k in r0[role]}
        st["h"] = st["p99_us"] / st["p50_us"] if st.get("p50_us") else None
        st["x_per_thread"] = st["X"] / out["n_w" if role == "write" else "n_r"]
        out[role] = st
    x = out["write"]["X"] + out["read"]["X"]
    tot = {k: _med(r["total"][k] for r in runs) for k in TOTAL_KEYS}
    tot.update({"ops": out["write"]["ops"] + out["read"]["ops"], "X": x,
                "phi_w": out["write"]["X"] / x, "phi_r": out["read"]["X"] / x})
    out["total"] = tot
    out["per_thread"] = [
        {"thread": t["thread"], "role": t["role"],
         "ops": _med(next(p["ops"] for p in r["per_thread"] if p["thread"] == t["thread"])
                     for r in runs)}
        for t in r0["per_thread"]]
    out["runs"] = len(runs)
    return out


def run_all(out_dir, repeats, duration, warmup, n, capacity, seed, with_control):
    os.makedirs(out_dir, exist_ok=True)
    series = {}

    def save(name, obj):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    print("Крок 2: t_cs,w і t_cs,r без конкуренції (глобальний замок)")
    cs_runs = repeat(
        lambda: bench.run_cs("global", n, capacity, warmup, duration),
        repeats, "t_cs")
    save("cs_runs.json", cs_runs)
    series["cs"] = {"runs": cs_runs, "median": median_view(cs_runs, "cs")}

    for lock in ("global", "rw"):
        print(f"Крок {2 if lock == 'global' else 3}: X_1, один потік, замок {lock}")
        runs = repeat(
            lambda lk=lock: bench.run_single(lk, n, capacity, warmup, duration, seed),
            repeats, f"X_1 {lock}")
        save(f"single_{lock}.json", runs)
        series[f"single_{lock}"] = {
            "runs": runs, "median": median_view(runs, "single")}

    configs = [("global", "random"), ("rw", "random"), ("rw-writer", "random")]
    if with_control:
        configs += [("global", "pair"), ("rw", "pair")]
    for lock, mix in configs:
        print(f"Прогони на {n} потоках: замок {lock}, суміш запису {mix}")
        runs = repeat(
            lambda lk=lock, mx=mix: bench.run_threads(
                lk, n, capacity, warmup, duration, seed, mx),
            repeats, f"{lock}/{mix}")
        save(f"threads_{lock}_{mix}.json", runs)
        series[f"threads_{lock}_{mix}"] = {
            "runs": runs, "median": median_view(runs, "threads")}

    return series


# ---------------------------------------------------------------- таблиці


def role_row(name, st, t_cs=None):
    cells = [
        name,
        num(st["ops"], 0),
        num(st["X"], 0),
        num(st["p50_us"], 1),
        num(st["p99_us"], 1),
        num(st["wait_max_ms"], 3),
        num(st["w"], 4),
        num(st["cv"], 4),
    ]
    if t_cs is not None:
        cells.append(num(t_cs, 3))
    return "| " + " | ".join(cells) + " |"


def lock_table(run, cs=None):
    head = ["Роль", "Операцій за D", "X, оп/с", "p50, мкс", "p99, мкс",
            "Найбільший t_wait, мс", "w", "cv"]
    if cs:
        head.append("t_cs, мкс")
    lines = ["| " + " | ".join(head) + " |",
             "|" + "---|" * len(head)]
    w, r, t = run["write"], run["read"], run["total"]
    lines.append(role_row("Запис", w, cs["t_cs_w_us"] if cs else None))
    lines.append(role_row("Читання", r, cs["t_cs_r_us"] if cs else None))
    total = {
        "ops": t["ops"], "X": t["X"], "p50_us": t["p50_us"], "p99_us": t["p99_us"],
        "wait_max_ms": t["wait_max_ms"], "w": t["w"], "cv": t["cv"],
    }
    lines.append(role_row("Разом", total, cs["t_cs_bar_us"] if cs else None))
    return "\n".join(lines)


def build_tables(series, n, capacity):
    g = series["threads_global_random"]["median"]
    rw = series["threads_rw_random"]["median"]
    rp = series["threads_rw-writer_random"]["median"]
    cs = series["cs"]["median"]
    x1_g = series["single_global"]["median"]["total"]["X1"]
    x1_rw = series["single_rw"]["median"]["total"]["X1"]
    n_w, n_r = g["n_w"], g["n_r"]
    host = g["host"]

    out = []
    a = out.append

    a("### Таблиця 1. Стенд\n")
    a("| n | n_w | n_r | Мова і версія | Логічних ядер | X_1, оп/с | "
      "Інваріанти після прогонів кроків 2 і 3 |")
    a("|---|---|---|---|---|---|---|")
    step23 = [r for k in ("threads_global_random", "threads_rw_random")
              for r in series[k]["runs"]]
    inv_ok = all(r["invariants"]["ok"] for r in step23)
    inv_txt = (f"виконані в усіх {len(step23)} прогонах" if inv_ok else "ПОРУШЕНІ")
    a(f"| {n} | {n_w} | {n_r} | Python {host['python']} "
      f"({host['implementation']}) | {host['logical_cores']} | {num(x1_g, 0)} | "
      f"{inv_txt} |")
    a(f"\nX_1 у таблиці — під глобальним замком; під RWLock той самий однопотоковий "
      f"прогін дає {num(x1_rw, 0)} оп/с (у кроці 4 кожне S(n) рахується від X_1 свого "
      f"способу синхронізації).\n")

    a("### Таблиця 2. Глобальний замок\n")
    a(lock_table(g, cs))
    a(f"\nt_cs середній = {num(cs['t_cs_bar_us'], 3)} мкс, межа пропускної здатності "
      f"1 / t_cs = {num(cs['x_limit'], 0)} оп/с.\n")

    a("### Таблиця 3. RWLock\n")
    a(lock_table(rw))
    a("")

    a("### Таблиця 4. Операції потоків за D секунд\n")
    a("| Потік | Роль | Глобальний замок | RWLock |")
    a("|---|---|---|---|")
    gp = {t["thread"]: t["ops"] for t in g["per_thread"]}
    rp_ = {t["thread"]: t["ops"] for t in rw["per_thread"]}
    for tid in range(1, n + 1):
        role = "запис" if tid % 2 else "читання"
        a(f"| {tid} | {role} | {num(gp[tid], 0)} | {num(rp_[tid], 0)} |")
    a("")

    a("### Таблиця 5. Метрики прискорення\n")
    a("| Метрика | Формула | Значення |")
    a("|---|---|---|")
    s_g = g["total"]["X"] / x1_g
    s_rw = rw["total"]["X"] / x1_rw
    rho = g["total"]["X"] * cs["t_cs_bar_us"] / 1e6
    a(f"| S(n), глобальний замок | X(G) / X_1 | {num(s_g, 3)} |")
    a(f"| E(n), глобальний замок | S(n) / n | {num(s_g / n, 3)} |")
    a(f"| S(n), RWLock | X(RW) / X_1 | {num(s_rw, 3)} |")
    a(f"| E(n), RWLock | S(n) / n | {num(s_rw / n, 3)} |")
    a(f"| S_w | X_w(RW) / X_w(G) | {num(rw['write']['X'] / g['write']['X'], 3)} |")
    a(f"| S_r | X_r(RW) / X_r(G) | {num(rw['read']['X'] / g['read']['X'], 3)} |")
    a(f"| S_Σ | X(RW) / X(G) | {num(rw['total']['X'] / g['total']['X'], 3)} |")
    a(f"| ρ, завантаженість глобального замка | X(G) · t̄_cs | {num(rho, 3)} |")
    a("")

    a("### Таблиця 6. Порівняння доступу до даних\n")
    head = ["Показник", "Глобальний замок: запис", "Глобальний замок: читання",
            "RWLock: запис", "RWLock: читання", "RW / G: запис", "RW / G: читання"]
    a("| " + " | ".join(head) + " |")
    a("|" + "---|" * len(head))
    rows = [
        ("X, оп/с", "X", 0),
        ("x, на один потік ролі, оп/с", "x_per_thread", 0),
        ("p50, мкс", "p50_us", 1),
        ("p99, мкс", "p99_us", 1),
        ("h = p99 / p50", "h", 2),
        ("Найбільший t_wait, мс", "wait_max_ms", 3),
        ("w", "w", 4),
        ("cv", "cv", 4),
    ]
    for label, key, digits in rows:
        gw, gr = g["write"][key], g["read"][key]
        rww, rwr = rw["write"][key], rw["read"][key]
        a(f"| {label} | {num(gw, digits)} | {num(gr, digits)} | {num(rww, digits)} | "
          f"{num(rwr, digits)} | {ratio(rww, gw)} | {ratio(rwr, gr)} |")
    a(f"| φ, частка ролі в усіх операціях | {num(g['total']['phi_w'], 4)} | "
      f"{num(g['total']['phi_r'], 4)} | {num(rw['total']['phi_w'], 4)} | "
      f"{num(rw['total']['phi_r'], 4)} | "
      f"{ratio(rw['total']['phi_w'], g['total']['phi_w'])} | "
      f"{ratio(rw['total']['phi_r'], g['total']['phi_r'])} |")
    a("")

    a("### Розкид між трьома прогонами кожної конфігурації\n")
    a("| Конфігурація | X прогонів, оп/с | Медіана | Розмах, % |")
    a("|---|---|---|---|")
    for key, label in (
        ("threads_global_random", "глобальний замок"),
        ("threads_rw_random", "RWLock, перевага читача"),
        ("threads_rw-writer_random", "RWLock, перевага писця"),
        ("threads_global_pair", "глобальний замок, контроль push+pop"),
        ("threads_rw_pair", "RWLock, контроль push+pop"),
        ("single_global", "X_1, глобальний замок"),
        ("single_rw", "X_1, RWLock"),
    ):
        if key not in series:
            continue
        runs = series[key]["runs"]
        path = "total.X1" if key.startswith("single") else "total.X"
        vals = []
        for r in runs:
            cur = r
            for part in path.split("."):
                cur = cur[part]
            vals.append(cur)
        med = statistics.median(vals)
        spread = (max(vals) - min(vals)) / med * 100 if med else 0.0
        a(f"| {label} | {', '.join(num(v, 0) for v in vals)} | {num(med, 0)} | "
          f"{num(spread, 1)} |")
    a("")

    a("### Умови прогонів\n")
    a("| Конфігурація | Від мережі | Економія заряду | Таймер Windows, мс | "
      "Фонове завантаження CPU перед прогоном, % |")
    a("|---|---|---|---|---|")
    for key in SERIES_FILES:
        if key not in series:
            continue
        conds = [r.get("host", {}).get("conditions") for r in series[key]["runs"]]
        if not all(conds):
            a(f"| {key} | не записано | не записано | не записано | не записано |")
            continue
        ac = "так" if all(c.get("ac_power") for c in conds) else "НІ"
        saver = "ні" if not any(c.get("battery_saver") for c in conds) else "ТАК"
        tr = sorted({c.get("timer_resolution_ms") for c in conds})
        bgs = [c.get("background_cpu_percent") for c in conds]
        a(f"| {key} | {ac} | {saver} | {', '.join(num(t, 1) for t in tr)} | "
          f"{', '.join(num(b, 1) for b in bgs)} |")
    a("")

    a("### RWLock з перевагою писця (додатковий прогін)\n")
    a(lock_table(rp))
    a("")

    if "threads_global_pair" in series:
        a("### Контрольна суміш запису: строге чергування push і pop\n")
        a("| Замок | X_w, оп/с | X_r, оп/с | X, оп/с | Найбільший розмір деку | "
          "w запису | w читання |")
        a("|---|---|---|---|---|---|---|")
        for key, label in (("threads_global_pair", "глобальний"),
                           ("threads_rw_pair", "RWLock")):
            m = series[key]["median"]
            mx = max(r["invariants"]["max_size"] for r in series[key]["runs"])
            a(f"| {label} | {num(m['write']['X'], 0)} | {num(m['read']['X'], 0)} | "
              f"{num(m['total']['X'], 0)} | {mx} | "
              f"{num(m['write']['w'], 4)} | {num(m['read']['w'], 4)} |")
        a("")

    a("### Інваріанти структури після прогонів\n")
    a("Стан структури — окремо по кожному з трьох прогонів конфігурації, через кому. "
      "Лічильники охоплюють і прогрів.\n")
    a("| Конфігурація | Розмір на кінець | Найбільший за прогін | Покладено | Вийнято | "
      "Відмов push через повноту | pop на порожньому | Інваріанти |")
    a("|---|---|---|---|---|---|---|---|")
    for key, label in (
        ("threads_global_random", "крок 2, глобальний замок"),
        ("threads_rw_random", "крок 3, RWLock, перевага читача"),
        ("threads_rw-writer_random", "RWLock, перевага писця"),
        ("threads_global_pair", "контроль, глобальний замок"),
        ("threads_rw_pair", "контроль, RWLock"),
    ):
        if key not in series:
            continue
        invs = [r["invariants"] for r in series[key]["runs"]]

        def col(k):
            return ", ".join(str(i[k]) if k in ("size", "max_size") else num(i[k], 0)
                             for i in invs)
        ok = all(i["ok"] for i in invs)
        a(f"| {label} | {col('size')} | {col('max_size')} з {invs[0]['capacity']} | "
          f"{col('pushed')} | {col('popped')} | {col('rejected_full')} | "
          f"{col('pops_on_empty')} | {'виконані в усіх трьох' if ok else 'ПОРУШЕНІ'} |")
    a("")

    return "\n".join(out)


def summary_numbers(series, n):
    g = series["threads_global_random"]["median"]
    rw = series["threads_rw_random"]["median"]
    rp = series["threads_rw-writer_random"]["median"]
    cs = series["cs"]["median"]
    x1_g = series["single_global"]["median"]["total"]["X1"]
    x1_rw = series["single_rw"]["median"]["total"]["X1"]
    return {
        "variant": VARIANT,
        "host": g["host"],
        "x1_global": x1_g,
        "x1_rw": x1_rw,
        "t_cs_w_us": cs["t_cs_w_us"],
        "t_cs_r_us": cs["t_cs_r_us"],
        "t_cs_bar_us": cs["t_cs_bar_us"],
        "x_limit": cs["x_limit"],
        "global": g,
        "rw": rw,
        "rw_writer": rp,
        "S_n_global": g["total"]["X"] / x1_g,
        "E_n_global": g["total"]["X"] / x1_g / n,
        "S_n_rw": rw["total"]["X"] / x1_rw,
        "E_n_rw": rw["total"]["X"] / x1_rw / n,
        "S_w": rw["write"]["X"] / g["write"]["X"],
        "S_r": rw["read"]["X"] / g["read"]["X"],
        "S_total": rw["total"]["X"] / g["total"]["X"],
        "rho": g["total"]["X"] * cs["t_cs_bar_us"] / 1e6,
    }


SERIES_FILES = {
    "cs": ("cs_runs.json", "cs"),
    "single_global": ("single_global.json", "single"),
    "single_rw": ("single_rw.json", "single"),
    "threads_global_random": ("threads_global_random.json", "threads"),
    "threads_rw_random": ("threads_rw_random.json", "threads"),
    "threads_rw-writer_random": ("threads_rw-writer_random.json", "threads"),
    "threads_global_pair": ("threads_global_pair.json", "threads"),
    "threads_rw_pair": ("threads_rw_pair.json", "threads"),
}


def load_series(out_dir):
    """Зібрати серії з уже знятих файлів, щоб перебудувати таблиці без прогонів."""
    series = {}
    for key, (name, kind) in SERIES_FILES.items():
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            runs = json.load(f)
        if not isinstance(runs, list):
            runs = [runs]
        series[key] = {"runs": runs, "median": median_view(runs, kind)}
    return series


def main(argv=None):
    ap = argparse.ArgumentParser(description="Прогони і таблиці ЛР5 (варіант 19)")
    ap.add_argument("--out-dir", default="results/lab5")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--warmup", type=float, default=1.0)
    ap.add_argument("--n", type=int, default=VARIANT["n"])
    ap.add_argument("--capacity", type=int, default=VARIANT["parameter"])
    ap.add_argument("--seed", type=int, default=190519)
    ap.add_argument("--no-control", action="store_true",
                    help="не знімати контрольну суміш запису push+pop")
    ap.add_argument("--tables-only", action="store_true",
                    help="перебудувати таблиці з уже знятих файлів, без прогонів")
    ap.add_argument("--max-bg", type=float, default=10.0,
                    help="поріг фонового завантаження CPU перед прогоном, %%")
    ap.add_argument("--allow-battery", action="store_true",
                    help="не зупиняти серію, якщо ноутбук не від мережі")
    a = ap.parse_args(argv)
    GATE["max_bg"] = a.max_bg
    GATE["require_ac"] = not a.allow_battery

    t0 = time.time()
    if a.tables_only:
        print(f"перебудова таблиць з {a.out_dir} без прогонів")
        series = load_series(a.out_dir)
        missing = [k for k in ("cs", "single_global", "single_rw",
                               "threads_global_random", "threads_rw_random",
                               "threads_rw-writer_random") if k not in series]
        if missing:
            print(f"не вистачає файлів для: {', '.join(missing)}")
            return 1
    else:
        print(f"Варіант 19: структура {VARIANT['structure']}, n = {a.n}, "
              f"місткість {a.capacity}; {platform.platform()}")
        series = run_all(a.out_dir, a.repeats, a.duration, a.warmup, a.n, a.capacity,
                         a.seed, not a.no_control)

    tables = build_tables(series, a.n, a.capacity)
    tables_path = os.path.join(a.out_dir, "tables.md")
    with open(tables_path, "w", encoding="utf-8") as f:
        f.write("# Таблиці ЛР5 (варіант 19: С9, n = 6, місткість 1024)\n\n")
        f.write("Кожна виміряна величина таблиць — медіана цієї величини за трьома "
                "прогонами конфігурації, як вимагає умова. Похідні величини (X = X_w + X_r, "
                "φ, x, h, t̄_cs, межа 1 / t̄_cs, метрики кроку 4 і відношення RW / G) "
                "пораховані за формулами умови з цих медіан. Усі три прогони — у розділі "
                "про розкид і у файлах прогонів.\n\n")
        f.write(tables)
    summary = summary_numbers(series, a.n)
    with open(os.path.join(a.out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nтаблиці: {tables_path}")
    print(f"підсумок: {os.path.join(a.out_dir, 'summary.json')}")
    print(f"усе разом за {time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
