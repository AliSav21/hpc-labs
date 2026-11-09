"""Таблиці звіту ЛР4: кроки 2-10 з сирих даних results/lab4/.

    python -m scripts.lab4_tables
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252

import io
import json
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent
if str(sys_path) not in _sys.path:
    _sys.path.insert(0, str(sys_path))

from loadgen.usl import fit, model  # noqa: E402

ROOT = sys_path
LAB4 = ROOT / "results" / "lab4"
WLIST = (1, 2, 4)
T_POLL_S = 0.050
LEASE_S = 5


def load(name):
    p = LAB4 / f"{name}.json"
    return json.load(io.open(p, encoding="utf-8")) if p.exists() else None


def fmt(v, d=2, dash="—"):
    if v is None:
        return dash
    if isinstance(v, str):
        return v
    return f"{v:.{d}f}".replace(".", ",")


def ms(q, d=1):
    """Квантиль за гістограмою в мілісекундах, з позначкою виходу за останню межу."""
    if not q or "value_s" not in q:
        return "—"
    v = q["value_s"] * 1000
    return (f"> {v:.0f}" if q.get("beyond_last") else fmt(v, d))


def table(rows, head):
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------- кроки

GEN_NAMES = {
    "step2": "крок 2", "step3_poll50": "крок 3, T_poll 50 мс",
    "step3_poll1": "крок 3, T_poll 1 мс",
    "step4_skip_locked": "крок 4, SKIP LOCKED", "step4_for_update": "крок 4, FOR UPDATE",
    "step5": "крок 5", "step6_W1": "крок 6, W = 1, 400 задач",
    "step6_W2": "крок 6, W = 2, 400 задач", "step6_W4": "крок 6, W = 4, 400 задач",
    "step6_long_W1": "крок 6, W = 1, 1500 задач",
    "step6_long_W2": "крок 6, W = 2, 1500 задач",
    "step6_long_W4": "крок 6, W = 4, 1500 задач",
    "step7_low": "крок 7, 0,7·X(R)", "step7_high": "крок 7, 1,3·X(R)",
    "step8": "крок 8", "step9_k8s": "крок 9, за умовою",
    "step9_clean": "крок 9, без Ф3", "step9_poison": "крок 9, з Ф3",
    "step9_clean_lo": "крок 9, λ = 5, без Ф3", "step9_poison_lo": "крок 9, λ = 5, з Ф3",
    "step10_poll100": "крок 10, опитування", "step10_listen": "крок 10, LISTEN",
}
GEN_ORDER = list(GEN_NAMES)


def t_generator():
    """Достовірність генератора задач по всіх прогонах."""
    import glob
    import os
    import statistics as st
    rows = []
    for name in GEN_ORDER + sorted(
            os.path.basename(f)[:-9] for f in glob.glob(str(LAB4 / "*_gen.json"))):
        if any(r[0] == GEN_NAMES.get(name, name) for r in rows):
            continue
        d = load(f"{name}_gen")
        if not d:
            continue
        # остання секунда прогону генератора неповна — у ній лишок задач, тому
        # до середнього вона не входить
        body = d["rows"][:-1] if len(d["rows"]) > 1 else d["rows"]
        sent = [r["sent"] for r in body]
        if not sent:
            continue
        tgt = d["rate_target"]
        m = st.fmean(sent)
        created = d["created"] + d.get("poisoned", 0)
        attempted = created + d["non2xx"]
        # Прогони кроку 6 на 400 задачах тривають 2 секунди: повних секунд для
        # середнього немає, а сама інтенсивність там не має значення — задачі
        # створюються при нулі воркерів, аби наповнити чергу.
        short = len(body) < 2
        rows.append([
            GEN_NAMES.get(name, name), fmt(tgt),
            "—" if short else fmt(m),
            "—" if short else fmt(100 * (m - tgt) / tgt, 1),
            "—" if short else fmt(100 * (min(sent) - tgt) / tgt, 1),
            created, d["non2xx"],
            fmt(100 * d["non2xx"] / attempted, 2) if attempted else "—",
            d.get("stale_conn_retries", "—"),
        ])
    return chr(10).join([
        "### Достовірність генератора задач" + chr(10),
        table(rows, ["Прогін", "Задана, задач/с", "Надіслано, середнє",
                     "Відхилення середнього, %", "Відхилення найгіршої секунди, %",
                     "Створено задач", "Невдалих POST", "Частка невдалих, %",
                     "Повторів на свіжому зʼєднанні"]),
        "",
        "Середнє і відхилення беруться за повними секундами прогону: остання секунда "
        "неповна, у ній лишок задач. У прогонах кроку 6 задачі створюються при нулі "
        "воркерів лише щоб наповнити чергу, тому інтенсивність створення там на "
        "результат не впливає.",
        "",
        "Стовпчик «Повторів на свіжому зʼєднанні» зʼявився після виправлення генератора "
        "(повтор запиту, коли сервер закрив простоююче keep-alive зʼєднання); у прогонах, "
        "знятих до виправлення, його немає.",
    ])


def t_step2():
    d = load("step2")
    if not d:
        return "Крок 2: даних немає."
    io_cpu = d["io_cpu"]
    rows = []
    for name, label in (("t_io", "t_io (крок 1 + крок 3 задачі)"),
                        ("t_cpu", "t_cpu (обробка зрізу)")):
        s = io_cpu.get(name)
        if not s:
            continue
        rows.append([label, fmt(s["mean_s"] * 1000), fmt(s["p50_s"] * 1000),
                     fmt(s["p99_s"] * 1000), fmt(s["min_s"] * 1000),
                     fmt(s["max_s"] * 1000)])
    total = (io_cpu["t_io"]["mean_s"] + io_cpu["t_cpu"]["mean_s"])
    txt = ["### Крок 2. t_io і t_cpu на 20 задачах, один воркер\n",
           table(rows, ["Величина", "Середнє, мс", "p50, мс", "p99, мс",
                        "Мін., мс", "Макс., мс"]),
           "",
           f"Задач у вимірюванні: **{io_cpu['n']}**. "
           f"t_io + t_cpu = **{fmt(total * 1000)} мс**, "
           f"t_io + t_cpu + T_poll / 2 = **{fmt((total + T_POLL_S / 2) * 1000)} мс**, "
           f"тобто оцінка 1 / (t_io + t_cpu + T_poll / 2) = "
           f"**{fmt(1 / (total + T_POLL_S / 2))} задач/с**."]
    return "\n".join(txt)


def t_step3():
    d = load("step3")
    if not d:
        return "Крок 3: даних немає."
    rows = []
    for key, run in d.items():
        poll = key.replace("poll_", "").replace("ms", "")
        t = run["tasks"]
        rows.append([poll, run["workers"], t["tasks_total"], t["results_rows"],
                     t["tasks_with_many_results"],
                     run["log_duplicates"]["tasks_done_more_than_once"],
                     run["log_duplicates"]["of_them_by_different_workers"],
                     fmt(run["D_by_results"], 4), fmt(run["D_by_logs"], 4),
                     fmt(run["duration_s"], 1)])
    return "\n".join([
        "### Крок 3. Наївне захоплення: частка дублів D\n",
        table(rows, ["T_poll, мс", "W", "Задач", "Рядків у task_results",
                     "Задач з > 1 результатом", "Задач «виконано» > 1 разу",
                     "З них різними воркерами", "D за результатами", "D за журналами",
                     "Прогін, с"]),
    ])


NL = chr(10)


def step4_runs():
    """Усі прогони кроку 4, згруповані за режимом захоплення."""
    import glob
    import os
    out = {"skip_locked": [], "for_update": [],
           "skip_locked_nonotify": [], "for_update_nonotify": []}
    for f in sorted(glob.glob(str(LAB4 / "step4_*.json"))):
        n = os.path.basename(f)[:-5]
        if n.endswith("_gen") or n == "step4":
            continue
        d = load(n)
        if not d or "tasks" not in d:
            continue
        mode = d.get("mode") or ("skip_locked" if "skip_locked" in n else "for_update")
        # прогони з вимкненим тригером NOTIFY — окремий дослід, не повтор
        key = mode + ("_nonotify" if n.endswith("_nonotify") else "")
        out.setdefault(key, []).append((n, d))
    return out


def t_step4():
    import statistics as st
    runs = step4_runs()
    if not any(runs.values()):
        return "Крок 4: даних немає."
    names = {"skip_locked": "FOR UPDATE SKIP LOCKED",
             "for_update": "FOR UPDATE без SKIP LOCKED",
             "skip_locked_nonotify": "SKIP LOCKED, тригер NOTIFY вимкнено",
             "for_update_nonotify": "без SKIP LOCKED, тригер NOTIFY вимкнено"}
    rows = []
    for mode in ("skip_locked", "for_update", "skip_locked_nonotify",
                 "for_update_nonotify"):
        for n, d in runs.get(mode, []):
            L = d.get("locks") or {}
            rows.append([names[mode], n.replace("step4_", ""),
                         fmt(d["D_by_results"], 4), ms(d["claim_p99"]),
                         f"{d['claims']['empty']:.0f}", f"{d['claims']['task']:.0f}",
                         L.get("lock_waits_max", "—"), L.get("locks_ungranted_max", "—"),
                         L.get("samples", "—"), fmt(L.get("interval_s"), 2),
                         fmt(d["duration_s"], 1)])
    out = ["### Крок 4. Захоплення однією командою, T_poll = 1 мс, W = 4" + NL,
           table(rows, ["Режим", "Прогін", "D", "p99 захоплення, мс", "Порожніх захоплень",
                        "Захоплень із задачею", "Макс. рядків `Lock`",
                        "Макс. `pg_locks` not granted", "Знімків", "Інтервал знімків, с",
                        "Прогін, с"]), ""]

    # зведення: середнє і розмах по повторах кожного режиму
    agg = []
    for mode in ("skip_locked", "for_update"):
        rs = runs.get(mode, [])
        if not rs:
            continue
        def col(fn):
            v = [fn(d) for _, d in rs if fn(d) is not None]
            return v
        empty = col(lambda d: d["claims"]["empty"])
        p99 = col(lambda d: (d["claim_p99"] or {}).get("value_s"))
        dur = col(lambda d: d["duration_s"])
        lw = col(lambda d: (d.get("locks") or {}).get("lock_waits_max"))
        agg.append([names[mode], len(rs),
                    f"{st.fmean(empty):.0f}" if empty else "—",
                    f"{min(empty):.0f}–{max(empty):.0f}" if empty else "—",
                    fmt(st.fmean(p99) * 1000, 1) if p99 else "—",
                    f"{min(p99)*1000:.1f}–{max(p99)*1000:.1f}" if p99 else "—",
                    fmt(st.fmean(dur), 1) if dur else "—",
                    f"{min(dur):.1f}–{max(dur):.1f}" if dur else "—",
                    f"{min(lw)}–{max(lw)}" if lw else "—"])
    out += ["Зведення по повторах" + NL,
            table(agg, ["Режим", "Прогонів", "Порожніх захоплень, середнє", "розмах",
                        "p99 захоплення, мс", "розмах", "Прогін, с", "розмах",
                        "Макс. рядків `Lock`, розмах"]), ""]

    waits = []
    for mode, rs in runs.items():
        for n, d in rs:
            for w in ((d.get("locks") or {}).get("waiting_on") or []):
                if w not in waits:
                    waits.append(w)
    if waits:
        out += ["На чому саме зафіксовано очікування блокування:" + NL, "```",
                *waits[:8], "```", ""]
    return NL.join(out)


def t_step5():
    d = load("step5")
    if not d:
        return "Крок 5: даних немає."
    held = (d["held_at_stop"] or [{}])[0]
    after = (d["after_lease_expiry"] or [{}])[0]
    final = (d["final"] or [{}])[0]
    zero = d["zero_rows_from_stopped_pod"]
    rows = [
        ["Ідентифікатор задачі в обробці на момент зупинки", held.get("id", "—")],
        ["Її статус і воркер до зупинки",
         f"{held.get('status')}, {held.get('worker_id')}"],
        ["attempts до зупинки", held.get("attempts", "—")],
        ["token до зупинки", held.get("token", "—")],
        ["attempts після спливання lease", after.get("attempts", "—")],
        ["token після спливання lease", after.get("token", "—")],
        ["Статус після спливання lease", after.get("status", "—")],
        ["Воркер, який завершив задачу", after.get("worker_id", "—")],
        ["Рядків у task_results для цієї задачі", after.get("results", "—")],
        ["Воркер, що записав результат", after.get("result_worker", "—")],
        ["Статус у кінці прогону", final.get("status", "—")],
        ["Рядків у task_results у кінці прогону", final.get("results", "—")],
        ["Рядків журналу зупиненого воркера про нуль оновлених рядків", len(zero)],
    ]
    out = ["### Крок 5. Lease, heartbeat і fencing token\n",
           table(rows, ["Що записано", "Значення"]), ""]
    if zero:
        out += ["Рядок журналу зупиненого воркера:", "",
                "```json", json.dumps(zero[0], ensure_ascii=False), "```", ""]
    t = d["tasks"]
    out.append(f"Усього задач у прогоні {t['tasks_total']}, за статусами "
               f"{t['by_status']}, задач з більше ніж одним результатом "
               f"**{t['tasks_with_many_results']}**, задач з attempts > 1 "
               f"{t['attempts_gt1']}.")
    out.append(f"Команда з умови `kubectl exec ... -- kill -STOP 1` "
               f"{'діє' if d.get('documented_command_works') else '**не діє**'}; "
               f"сигнал надіслано "
               f"{'нею' if d.get('documented_command_works') else 'з простору імен вузла'}, "
               f"стан процесу після зупинки `{d.get('proc_state_after_stop')}`.")
    return "\n".join(out)


def step6_runs(min_tasks=1000, min_window=30):
    """Усі прогони кроку 6, придатні для підбору закону: ≥ min_tasks задач і повне вікно."""
    import glob
    import os
    import statistics as st
    runs, seen = [], set()
    for d in (LAB4, LAB4 / "v1"):
        for f in sorted(glob.glob(str(d / "step6*_W*.json"))):
            n = os.path.basename(f)[:-5]
            if n.endswith("_gen"):
                continue
            r = json.load(io.open(f, encoding="utf-8"))
            X = r.get("X") or {}
            if not X or not r.get("pre_queued"):
                continue
            key = (r["workers"], r["pre_queued"], round(X["X_rps"], 4))
            if key in seen:
                continue      # копія того самого прогону в архіві серії 1
            seen.add(key)
            runs.append({"name": n, "series": "серія 1" if d.name == "v1" else n,
                         "W": r["workers"], "X": X["X_rps"], "sd": X["sd"],
                         "window": X["window_s"], "tasks": r["pre_queued"],
                         "claim_p99": r.get("claim_p99"), "db": r.get("db") or {},
                         "usable": r["pre_queued"] >= min_tasks
                                   and X["window_s"] >= min_window})
    agg = {}
    for w in (1, 2, 4):
        v = [r["X"] for r in runs if r["W"] == w and r["usable"]]
        if v:
            agg[w] = {"n": len(v), "mean": st.fmean(v),
                      "sd": st.stdev(v) if len(v) > 1 else 0.0,
                      "min": min(v), "max": max(v)}
    return runs, agg


def t_step6():
    runs, agg = step6_runs()
    if not agg:
        return "Крок 6: даних немає."
    rows = []
    for r in sorted(runs, key=lambda r: (r["W"], r["name"])):
        rows.append([r["W"], r["tasks"], fmt(r["X"]), fmt(r["sd"]), r["window"],
                     ms(r["claim_p99"]), fmt(r["db"].get("db_tps"), 1),
                     fmt(r["db"].get("db_cpu_cores"), 3),
                     "так" if r["usable"] else "ні"])
    out = ["### Крок 6. Пропускна здатність пулу X(W)" + NL,
           table(rows, ["W", "Задач у черзі", "X(W), задач/с",
                        "Розкид усередині прогону", "Вікно, с", "p99 захоплення, мс",
                        "Транзакцій бази за с", "CPU пода бази, ядер",
                        "У підбір закону"]), ""]
    rows2 = [[w, a["n"], fmt(a["mean"]), fmt(a["sd"]),
              fmt(a["min"]) + "–" + fmt(a["max"]),
              fmt(100 * a["sd"] / a["mean"], 1),
              fmt(100 * (a["max"] - a["min"]) / a["min"], 0)]
             for w, a in sorted(agg.items())]
    out += ["Зведення по прогонах із повним вікном 30 с і чергою ≥ 1000 задач" + NL,
            table(rows2, ["W", "Прогонів", "X(W), середнє", "σ між прогонами",
                          "Розмах", "Коеф. варіації, %", "Розмах, %"]), ""]

    pts = {w: a["mean"] for w, a in agg.items()}
    if 1 in pts and len(pts) >= 2:
        f = fit(pts)
        tp = f.get("two_param") or {}
        rows3 = []
        for w in sorted(pts):
            c = pts[w] / pts[1]
            y = (w / c - 1) if w > 1 else None
            rows3.append([w, fmt(pts[w]), fmt(c, 4),
                          fmt(y, 4) if y is not None else "—",
                          w - 1 if w > 1 else "—", w * (w - 1) if w > 1 else "—",
                          fmt(model(w, f["lambda"], f["sigma"], f["kappa"]))])
        sm = f["sums"]
        out += ["Підбір закону за середніми" + NL,
                table(rows3, ["W", "X(W), задач/с", "C(W)", "y = W/C(W) − 1", "u", "v",
                              "Закон, задач/с"]), "",
                f"Σu² = {fmt(sm['uu'], 0)}, Σv² = {fmt(sm['vv'], 0)}, "
                f"Σuv = {fmt(sm['uv'], 0)}, Σuy = {fmt(sm['uy'], 4)}, "
                f"Σvy = {fmt(sm['vy'], 4)}, Δ = {fmt(f['det'], 0)}.", "",
                f"Двопараметрична оцінка: σ = {fmt(tp.get('sigma'), 4)}, "
                f"κ = {fmt(tp.get('kappa'), 4)}. Ужито: **σ = {fmt(f['sigma'], 4)}**, "
                f"**κ = {fmt(f['kappa'], 4)}**, λ = X(1) = {fmt(f['lambda'])} задач/с, "
                + (f"**W\* = {fmt(f['N_star'])}**, X(W\*) = "
                   f"{fmt(f.get('X_at_N_star'))} задач/с."
                   if f.get("N_star") else
                   f"максимуму немає, межа λ/σ = {fmt(f.get('X_limit'))} задач/с."), ""]
        if f.get("note"):
            out += [f"Зауваження підбору: {f['note']}.", ""]

        # чутливість до нормувальної точки
        rows4 = []
        for x1 in sorted({agg[1]["min"], 10.0, 10.5, agg[1]["mean"], 11.5,
                          agg[1]["max"]}):
            g = fit({1: x1, **{w: pts[w] for w in pts if w != 1}})
            t = g.get("two_param") or {}
            rows4.append([fmt(x1), fmt(pts[4] / x1, 3),
                          fmt(t.get("sigma"), 4), fmt(t.get("kappa"), 4),
                          fmt(g["sigma"], 4), fmt(g["kappa"], 4),
                          fmt(g["N_star"]) if g.get("N_star") else "немає"])
        out += ["Чутливість параметрів до нормувальної точки X(1) у межах її "
                "виміряного розмаху" + NL,
                table(rows4, ["X(1), задач/с", "C(4)", "σ двопарам.", "κ двопарам.",
                              "σ ужито", "κ ужито", "W*"]), ""]
    return NL.join(out)


def t_step7():
    d = load("step7")
    if not d:
        return "Крок 7: даних немає."
    rows = []
    for tag, r in d.items():
        wq = r["W_q"]
        rows.append([r["label"], fmt(r["rate_target"]), fmt(r["duration_s"], 0),
                     fmt(r["lambda_measured"]), fmt(r["L_q_mean"]),
                     fmt(wq.get("mean_s"), 4), fmt(r["little_lambda_Wq"]),
                     fmt(r.get("discrepancy_pct"), 1), r["queue_at_end"]])
    out = ["### Крок 7. Закон Літтла для черги\n",
           table(rows, ["Прогін", "λ задана", "Тривалість, с", "λ виміряна",
                        "L_q виміряне", "W_q, с", "λ · W_q", "Розбіжність, %",
                        "Черга на кінець"]), ""]
    high = d.get("high")
    if high:
        rows2 = []
        for key, label in (("W_q_first100", "перші 100 задач"),
                           ("W_q_last100", "останні 100 задач")):
            s = high.get(key) or {}
            rows2.append([label, s.get("n", 0), fmt(s.get("mean_s"), 3),
                          fmt(s.get("p50_s"), 3), fmt(s.get("p99_s"), 3),
                          fmt(s.get("max_s"), 3)])
        out += ["Прогін 1,3 · X(R): W_q за першими і останніми 100 задачами\n",
                table(rows2, ["Вибірка", "Задач", "Середнє, с", "p50, с", "p99, с",
                              "Макс., с"]), ""]
        out.append(f"Довжина черги на кінець прогону 1,3 · X(R): "
                   f"**{high['queue_at_end']}** у статусі queued і "
                   f"{high['running_at_end']} у running; задач, які так і не були "
                   f"захоплені, {high.get('never_started', high['queue_at_end'])}. "
                   f"«Останні 100 задач» у таблиці — останні сто з тих, що встигли "
                   f"бути захопленими: для хвоста черги W_q не визначений.")
    return "\n".join(out)


def t_step8():
    d = load("step8")
    if not d:
        return "Крок 8: даних немає."
    t = d["tasks"]
    exits = d["exit_codes_old_pods"]
    rows = [
        ["Воркерів до перезапуску", len(d["pods_before"])],
        ["Коди завершення контейнерів старих подів",
         ", ".join(f"{e['pod'].rsplit('-', 1)[-1]}: {e['exit_code']} ({e['reason']})"
                   for e in exits) or "—"],
        ["Рядків журналу про SIGTERM", len(d["sigterm_lines"])],
        ["Рядків журналу «зупинка завершена» з graceful = true",
         len([g for g in d["graceful_exit_lines"] if g.get("graceful")])],
        ["Задач, повернутих у чергу воркерами під час зупинки", d["requeued_by_workers"]],
        ["Задач у статусі failed", t["by_status"].get("failed", 0)],
        ["Задач з більше ніж одним результатом", t["tasks_with_many_results"]],
        ["Задач, у яких attempts більше 1", t["attempts_gt1"]],
        ["Усього задач у прогоні", t["tasks_total"]],
        ["Задач у статусі done", t["by_status"].get("done", 0)],
    ]
    return "\n".join(["### Крок 8. Коректна зупинка під прогоном\n",
                      table(rows, ["Що записано", "Значення"])])


NAMES9 = {
    "k8s": "за умовою: λ = 0,7·X(R), перезапуск контейнера kubelet",
    "poison": "λ = 0,7·X(R), наглядач процесу + Ф3",
    "clean": "λ = 0,7·X(R), наглядач процесу, без Ф3",
    "poison_lo": "λ = 5 задач/с, наглядач процесу + Ф3",
    "clean_lo": "λ = 5 задач/с, наглядач процесу, без Ф3",
}
ORDER9 = ["k8s", "clean", "poison", "clean_lo", "poison_lo"]


def step9_runs():
    """Прогони кроку 9 з окремих файлів, а не зі зведеного step9.json.

    Зведений файл пишеться в кінці серії: якщо серія обірвалась на півдорозі (а так і
    сталось, коли TRUNCATE не зміг узяти блокування), у ньому лишаються числа
    попередніх прогонів, і таблиця мовчки показує застарілі дані.
    """
    out = {}
    for tag in ORDER9:
        d = load(f"step9_{tag}")
        if d and "tasks" in d:
            out[tag] = d
    return out


def t_step9():
    d = step9_runs()
    if not d:
        return "Крок 9: даних немає."
    f = 0.15
    keys = [k for k in ORDER9 if k in d] + [k for k in d if k not in ORDER9]
    rows = []
    for tag in keys:
        r = d[tag]
        t = r["tasks"]
        n = t["tasks_total"]
        never = int(t["by_attempts"].get("0", 0))
        attempted = n - never          # задачі, яких воркер торкнувся хоч раз
        gt1 = t["attempts_gt1"]
        failed = t["by_status"].get("failed", 0)
        done = t["by_status"].get("done", 0)
        x = done / max(r["duration_s"], 1)
        rows.append([
            NAMES9.get(tag, tag), n, attempted,
            r["restarts_delta"], r["supervisor_restarts"] or r["builtin_exits"],
            done, failed, gt1,
            fmt(100 * gt1 / attempted, 1) if attempted else "—",
            fmt(100 * failed / attempted, 2) if attempted else "—",
            t["tasks_with_many_results"], fmt(x, 2),
        ])
    out = ["### Крок 9. FAIL_RATE = 0,15 і сценарій відмови Ф3\n",
           table(rows, ["Прогін", "Створено", "Захоплено хоч раз",
                        "Перезапусків контейнерів", "Падінь воркера", "done", "failed",
                        "attempts > 1", "Частка attempts > 1, %", "Частка failed, %",
                        "Задач з > 1 результатом", "X, задач/с"]), "",
           f"Очікувано: частка задач з attempts > 1 ≈ f = {fmt(f, 2)} "
           f"({fmt(100 * f, 0)} %), частка failed ≈ f³ = {fmt(f ** 3, 5)} "
           f"({fmt(100 * f ** 3, 3)} %), середня кількість захоплень однієї задачі "
           f"1 / (1 − f) = {fmt(1 / (1 - f), 3)}. Частки рахуються від задач, яких воркер "
           f"захопив хоч раз: у прогонах з λ = 0,7·X(R) черга росте, і хвіст задач за час "
           f"прогону не доходить до жодного воркера.", ""]

    rows2 = []
    for tag in keys:
        r = d[tag]
        t = r["tasks"]
        never = int(t["by_attempts"].get("0", 0))
        attempted = t["tasks_total"] - never
        for k, v in sorted(t["by_attempts"].items(), key=lambda kv: int(kv[0])):
            if k == "0":
                continue
            rows2.append([NAMES9.get(tag, tag), k, v,
                          fmt(100 * v / attempted, 2) if attempted else "—"])
    out += ["Розподіл захоплених задач за attempts\n",
            table(rows2, ["Прогін", "attempts", "Задач", "Частка захоплених, %"]), ""]

    rows3 = []
    for tag in keys:
        for r in d[tag].get("poison_tasks") or []:
            rows3.append([NAMES9.get(tag, tag), r["id"], r["status"], r["attempts"],
                          r["token"],
                          fmt(float(r["to_failed_s"]) if r.get("to_failed_s") else None, 2)])
    if rows3:
        out += ["Отруєні задачі (seed = 0) після прогону\n",
                table(rows3, ["Прогін", "id", "Статус", "attempts", "token",
                              "Від створення до failed, с"]), ""]

    rows4 = []
    for tag in keys:
        w = d[tag].get("wait_without_poison") or d[tag]["wait"]
        rows4.append([NAMES9.get(tag, tag), w.get("n"), fmt(w.get("mean_s"), 3),
                      fmt(w.get("p50_s"), 3), fmt(w.get("p99_s"), 3)])
    out += ["Час очікування решти задач (seed ≠ 0)\n",
            table(rows4, ["Прогін", "Задач", "Середнє W_q, с", "p50, с", "p99, с"]), ""]
    return "\n".join(out)


def t_step10():
    d = load("step10")
    if not d:
        return "Крок 10: даних немає."
    rows = []
    for tag, r in d.items():
        w = r["wait"]
        idle = r["idle"]
        rows.append([
            "опитування 100 мс" if r["wait_mode"] == "poll" else "LISTEN/NOTIFY",
            fmt(w.get("p50_s") * 1000 if w.get("p50_s") is not None else None),
            fmt(w.get("p99_s") * 1000 if w.get("p99_s") is not None else None),
            fmt(w.get("mean_s") * 1000 if w.get("mean_s") is not None else None),
            w.get("n"),
            fmt(idle["db_queries_per_s"], 1),
            fmt(idle["empty_claims_per_s"], 1),
            fmt(idle["window_s"], 0),
        ])
    return "\n".join([
        "### Крок 10 (вимога Д5). LISTEN/NOTIFY замість опитування\n",
        table(rows, ["Режим", "p50 (started_at − created_at), мс", "p99, мс",
                     "Середнє, мс", "Задач", "Запитів до бази за с (порожня черга)",
                     "Порожніх захоплень за с", "Вікно простою, с"]),
    ])


SECTIONS = [t_generator, t_step2, t_step3, t_step4, t_step5, t_step6, t_step7, t_step8, t_step9,
            t_step10]


if __name__ == "__main__":
    parts = ["# Таблиці лабораторної роботи 4 (варіант 19)", "",
             "Складено з сирих даних `results/lab4/` скриптом "
             "[scripts/lab4_tables.py](../../scripts/lab4_tables.py).", ""]
    for fn in SECTIONS:
        parts.append(fn())
        parts.append("")
    text = "\n".join(parts)
    out = LAB4 / "tables.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n-> {out.relative_to(ROOT)}")
