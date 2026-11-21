"""Таблиці 1-6 ЛР6 з файлів прогонів у results/lab6.

Кожна виміряна величина — медіана цієї величини за прогонами конфігурації (прогонів
задає `--repeats` оркестратора, типово 3). Похідні величини — t_comm,exp, c, S, E, e,
S_w, E_w, t_lost — пораховані за формулами умови з цих медіан.

    python -m scripts.lab6_tables --out-dir results/lab6
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import statistics

from lab6.host import utf8_console
from lab6.train import ETA0, N_EPOCHS, VARIANT

P_MAX = VARIANT["p_max"]
B = VARIANT["batch"]
B_PROC = B // P_MAX
K_C = VARIANT["ckpt_interval"]
S_F = VARIANT["fail_step"]
DASH = "—"


def num(v, digits=2):
    """Число в українському записі: кома як десяткова, вузький пробіл на розряди."""
    if v is None:
        return DASH
    if isinstance(v, int) or (isinstance(v, float) and digits == 0):
        return f"{round(v):,}".replace(",", " ")
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def os_name(host):
    """«Windows 11 Home (10.0.26300)» з рядка platform.platform()."""
    m = re.search(r"(\d+\.\d+\.\d+)", host.get("platform", ""))
    build = f" ({m.group(1)})" if m else ""
    return f"Windows 11 Home{build}"


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def runs(out_dir, stem):
    """Прогони конфігурації за номером кола: {i: дані файлу stem_r{i}.json}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(out_dir, f"{stem}_r*.json"))):
        m = re.search(r"_r(\d+)\.json$", path)
        if m:
            out[int(m.group(1))] = load(path)
    return out


def med(rs, key):
    vals = [r[key] for r in rs if r and key in r and r[key] is not None]
    return statistics.median(vals) if vals else None


class Speed:
    """Медіани величин за прогонами однієї конфігурації вимірювання швидкості."""

    def __init__(self, by_round, label):
        self.label = label
        self.by_round = {i: r for i, r in (by_round or {}).items() if r}
        rs = [self.by_round[i] for i in sorted(self.by_round)]
        self.n = len(rs)
        self.t_step = med(rs, "t_step_ms")
        self.t_sync = med(rs, "t_sync_ms")
        self.x = med(rs, "throughput")
        self.b = rs[0]["b"] if self.n else None
        self.B = rs[0]["B"] if self.n else None
        self.P = rs[0]["P"] if self.n else None
        self.raw = rs
        self.t_steps = [r["t_step_ms"] for r in rs]
        self.xs = [r["throughput"] for r in rs]

    def spread(self, vals=None):
        """Розкид між прогонами у відсотках медіани."""
        vals = vals if vals is not None else self.t_steps
        if len(vals) < 2:
            return None
        m = statistics.median(vals)
        return (max(vals) - min(vals)) / m * 100 if m else None

    def iqr_spread(self, vals=None):
        """Розкид між квартилями у відсотках медіани: без поодиноких викидів."""
        vals = vals if vals is not None else self.t_steps
        if len(vals) < 4:
            return None
        q1, _, q3 = statistics.quantiles(vals, n=4)
        m = statistics.median(vals)
        return (q3 - q1) / m * 100 if m else None

    def __bool__(self):
        return self.n > 0


def paired_diff(a, b):
    """Медіана різниці t_step тих самих кіл двох конфігурацій (парне порівняння).

    Прогони знімаються по колу — коло i кожної конфігурації одне за одним, — тому різниця
    t_step кола i майже не містить дрейфу машини, який у різниці медіан лишається.
    """
    common = sorted(set(a.by_round) & set(b.by_round))
    diffs = [a.by_round[i]["t_step_ms"] - b.by_round[i]["t_step_ms"] for i in common]
    if not diffs:
        return None, 0, []
    return statistics.median(diffs), len(diffs), diffs


def rows_md(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------- таблиці


def table1(stand):
    h = stand["host"]
    c = h.get("conditions", {})
    t = ["### Таблиця 1. Стенд", ""]
    t.append(rows_md(
        ["Процесор", "Фізичних ядер C_phys", "Логічних ядер", "Памʼять, ГБ", "ОС",
         "Python", "PyTorch", "n_th"],
        [[str(h.get("cpu") or DASH), num(h.get("physical_cores"), 0),
          num(h.get("logical_cores"), 0), num(h.get("memory_gb"), 1),
          os_name(h), f"{h['python']} ({h['implementation']})",
          h["torch"], num(stand["n_th"], 0)]]))
    t.append("")
    t.append(rows_md(
        ["Датасет", "K", "C", "c1 / c2 / h", "n_params", "M, МіБ", "N", "B", "P_max"],
        [[f"{stand['dataset']}.npz ({VARIANT['dataset']}, DermaMNIST)",
          num(stand["K"], 0), num(stand["C"], 0),
          f"{stand['c1']} / {stand['c2']} / {stand['h']}",
          num(stand["n_params"], 0), num(stand["M_mib"], 3),
          num(stand["N_train"], 0), num(stand["B"], 0), num(P_MAX, 0)]]))
    t.append("")
    t.append(f"n_params за формулою умови — {num(stand['n_params_formula'], 0)}, збігається з "
             f"кількістю параметрів моделі; M = 4 · n_params = "
             f"{num(stand['M_bytes'], 0)} Б = {num(stand['M_mib'], 3)} МіБ. Тестова частина — "
             f"{num(stand['N_test'], 0)} прикладів. Параметри навчання: SGD з моментом "
             f"{num(stand['momentum'], 1)}, η₀ = {num(stand['eta0'], 2)}, n_ep = "
             f"{num(stand['n_epochs'], 0)}, 8 тензорів параметрів. "
             f"n_th = ⌊C_phys / P_max⌋ = ⌊{h.get('physical_cores')} / {P_MAX}⌋ = "
             f"{stand['n_th']}.")
    if c:
        t.append("")
        t.append(f"Умови під час опису стенду: від мережі — "
                 f"{'так' if c.get('ac_power') else 'НІ'}, економія заряду — "
                 f"{'ТАК' if c.get('battery_saver') else 'ні'}, таймер Windows "
                 f"{num(c.get('timer_resolution_ms'), 1)} мс.")
    return "\n".join(t)


def table2(single, manual, ddp, nosync, equiv, train_single, train_ddp):
    t_comm, n_pairs, diffs = paired_diff(ddp, nosync)
    t_comm_meds = ddp.t_step - nosync.t_step if (ddp and nosync) else None
    rows = [
        ["1 процес, батч B", num(single.t_step, 2), "0", "0", num(single.x, 0)],
        ["P_max процесів, ручна синхронізація", num(manual.t_step, 2),
         num(manual.t_sync, 2),
         num(manual.t_sync / manual.t_step, 3) if manual else DASH, num(manual.x, 0)],
        ["P_max процесів, DDP", num(ddp.t_step, 2), num(t_comm, 2),
         num(t_comm / ddp.t_step, 3) if t_comm is not None else DASH, num(ddp.x, 0)],
        ["P_max процесів, DDP, no_sync", num(nosync.t_step, 2), "0", "0",
         num(nosync.x, 0)],
    ]
    t = ["### Таблиця 2. Синхронізація градієнтів", "",
         rows_md(["Режим", "t_step, мс", "Час обміну на крок, мс",
                  "Частка кроку в обміні", "X, прикл./с"], rows), "",
         "Час обміну на крок для ручної синхронізації дорівнює t_sync (від першого виклику "
         "all-reduce до повернення з останнього), для DDP дорівнює t_comm,exp = t_step − "
         "t_step,ns.", "",
         f"t_comm,exp для DDP — медіана різниці t_step − t_step,ns за {n_pairs} колами "
         f"прогонів (у кожному колі обидва режими знімаються один за одним). Різниця "
         f"медіан двох конфігурацій дає {num(t_comm_meds, 2)} мс: вона містить дрейф "
         f"машини між прогонами, бо розкид t_step між прогонами "
         f"({num(ddp.spread(), 0)} % медіани для DDP, {num(nosync.spread(), 0)} % для "
         f"no_sync) того самого порядку, що й сам обмін. Різниці по колах — "
         + ", ".join(num(d, 2) for d in diffs) + " мс.", ""]
    t.append(rows_md(
        ["max \\|g − ḡ\\| / max \\|g\\|", "Різниця сум ваг між процесами після 50 кроків",
         "ACC, 1 процес", "ACC, DDP на P_max", "AUC, 1 процес", "AUC, DDP на P_max"],
        [[f"{equiv['max_rel_grad_diff']:.2e}".replace(".", ",") if equiv else DASH,
          num(equiv["weight_sum_spread"], 0) if equiv else DASH,
          num(train_single["acc"], 4), num(train_ddp["acc"], 4),
          num(train_single["auc"], 4), num(train_ddp["auc"], 4)]]))
    return "\n".join(t)


def karp_flatt(s, p):
    if s is None or p == 1:
        return None
    return (1 / s - 1 / p) / (1 - 1 / p)


def table3(strong):
    """strong: {P: (Speed ddp, Speed nosync)}."""
    x1 = strong[1][0].x if 1 in strong else None
    rows = []
    for p in range(1, 5):
        if p not in strong:
            rows.append([num(p, 0)] + [DASH] * 8)
            continue
        sp, ns = strong[p]
        t_comm = paired_diff(sp, ns)[0]
        c = t_comm / sp.t_step if t_comm is not None else None
        s = sp.x / x1 if x1 else None
        rows.append([num(p, 0), num(sp.b, 0), num(sp.t_step, 2), num(ns.t_step, 2),
                     num(sp.x, 0), num(s, 3), num(s / p if s else None, 3),
                     num(karp_flatt(s, p), 3), num(c, 3)])
    return "\n".join([
        "### Таблиця 3. Strong scaling, B = const", "",
        rows_md(["P", "b", "t_step, мс", "t_step,ns, мс", "X, прикл./с", "S", "E", "e", "c"],
                rows), "",
        f"Глобальний батч B = {B} у всіх рядках, прогін з P = 1 теж виконано через torchrun "
        f"з DDP. Рядки з P, більшим за P_max = {P_MAX}, не заповнюються. t_step і "
        f"t_step,ns — медіани за прогонами конфігурації, c порахована з медіани різниці по "
        f"колах (див. пояснення до таблиці 2).",
    ])


def table4(weak):
    x1 = weak[1].x if 1 in weak else None
    rows = []
    for p in range(1, 5):
        if p not in weak:
            rows.append([num(p, 0)] + [DASH] * 5)
            continue
        sp = weak[p]
        s = sp.x / x1 if x1 else None
        rows.append([num(p, 0), num(sp.B, 0), num(sp.t_step, 2), num(sp.x, 0),
                     num(s, 3), num(s / p if s else None, 3)])
    return "\n".join([
        "### Таблиця 4. Weak scaling, b = const", "",
        rows_md(["P", "B_P", "t_step, мс", "X, прикл./с", "S_w", "E_w"], rows), "",
        f"Батч процесу b = B / P_max = {B_PROC} у всіх рядках. На P = P_max = {P_MAX} "
        f"глобальний батч дорівнює B = {B}, тому рядок узятий з таблиці 3.",
    ])


def table5(t_p1_b, t_ddp_eta0, t_ddp_lin):
    def row(label, r, eta_label):
        return [label, num(r["B"], 0), eta_label, num(r["steps_total"], 0),
                num(r["acc"], 4), num(r["auc"], 4),
                num(r["loss_max_last_epoch"], 4)]

    rows = [
        row("P = 1", t_p1_b, f"η₀ = {num(ETA0, 2)}"),
        row("P = P_max, з кроку 2", t_ddp_eta0, f"η₀ = {num(ETA0, 2)}"),
        row("P = P_max", t_ddp_lin,
            f"η_P = η₀ · B / b = {num(t_ddp_lin['lr'], 2)}"),
    ]
    return "\n".join([
        "### Таблиця 5. Якість за різних глобальних батчів", "",
        rows_md(["Режим", "B_P", "η", f"Кроків оптимізатора за n_ep = {N_EPOCHS} епох",
                 "ACC", "AUC", "Найбільша втрата за останню епоху"], rows), "",
        f"Лінійне правило взято з B₀ = b = {B_PROC}: η_P = η₀ · B_P / B₀ = "
        f"{num(ETA0, 2)} · {B} / {B_PROC} = {num(ETA0 * P_MAX, 2)}. Втрата на кроці — "
        f"втрата на глобальному батчі (середнє локальних втрат процесів, частки однакового "
        f"розміру).",
    ])


def failure_events(fail_dir):
    ev = []
    for path in sorted(glob.glob(os.path.join(fail_dir, "events-rank*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    ev.append(json.loads(line))
    return ev


def agent_detect_time(log_path):
    """Момент, коли агент torchrun записав про відмову процесу (з його ж журналу)."""
    if not os.path.exists(log_path):
        return None
    pat = re.compile(r"^E(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d+).*failed \(exitcode")
    year = dt.datetime.now().year
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.match(line)
            if m:
                mon, day, hh, mm, ss, frac = m.groups()
                t = dt.datetime(year, int(mon), int(day), int(hh), int(mm), int(ss),
                                int(frac[:6].ljust(6, "0")))
                return t.timestamp()
    return None


def failure_summary(fail_dir):
    ev = failure_events(fail_dir)
    if not ev:
        return None
    fail = next((e for e in ev if e["kind"] == "fail"), None)
    if not fail:
        return None
    t_f = fail["t"]
    ckpts = [e for e in ev if e["kind"] == "ckpt" and e["t"] <= t_f]
    last = max(ckpts, key=lambda e: e["step"]) if ckpts else None
    steps0 = [e for e in ev if e["kind"] == "step" and e["attempt"] == 0]
    done_before = max((e["step"] for e in steps0), default=0)
    after = sorted((e for e in ev if e["kind"] == "step" and e["attempt"] > 0),
                   key=lambda e: e["t"])
    first_after = after[0] if after else None
    resume = next((e for e in ev if e["kind"] == "resume"), None)
    start1 = next((e for e in ev if e["kind"] == "start" and e["attempt"] > 0), None)
    t_r = first_after["t"] - t_f if first_after else None
    t_lost = (t_f - last["t"]) + t_r if (last and t_r is not None) else None
    detect = agent_detect_time(os.path.join(fail_dir, "torchrun.log"))
    return {
        "s_f": fail["step"],
        "t_fail": t_f,
        "ckpt_step": last["step"] if last else None,
        "t_ckpt": last["t"] if last else None,
        "steps_done_before_fail": done_before,
        "steps_redone": (done_before - last["step"]) if last else None,
        "t_f_minus_t_c": (t_f - last["t"]) if last else None,
        "t_r": t_r,
        "t_lost": t_lost,
        "t_detect_agent": detect,
        "detect_after_fail": (detect - t_f) if detect else None,
        "restart_boot": (start1["t"] - t_f) if start1 else None,
        "resume_to_first_step": (first_after["t"] - resume["t"])
        if (resume and first_after) else None,
        "first_step_after": first_after["step"] if first_after else None,
    }


def losses_by_step(result, attempt=None):
    out = {}
    for h in result.get("loss_history", []):
        if attempt is None or h["attempt"] == attempt:
            out[h["step"]] = h["loss"]
    return out


def table6(fs, fail_result, ok_result, ctrl_result=None):
    window = list(range(S_F - 2, S_F + 3))
    lf = losses_by_step(fail_result)
    lo = losses_by_step(ok_result)
    t = ["### Таблиця 6. Відмова і відновлення", "",
         rows_md(["K_c", "s_f", "Крок останньої контрольної точки",
                  "Кроків виконано повторно", "t_lost, с"],
                 [[num(K_C, 0), num(S_F, 0), num(fs["ckpt_step"], 0),
                   num(fs["steps_redone"], 0), num(fs["t_lost"], 2)]]), ""]
    header = ["Крок"] + [str(s) for s in window] + [f"ACC після n_ep = {N_EPOCHS} епох"]
    rows = [
        ["Втрата, прогін з відмовою"] + [num(lf.get(s), 4) for s in window]
        + [num(fail_result.get("acc"), 4)],
        ["Втрата, прогін без відмови"] + [num(lo.get(s), 4) for s in window]
        + [num(ok_result.get("acc"), 4)],
    ]
    t.append(rows_md(header, rows))
    t.append("")
    same = all(lf.get(s) == lo.get(s) for s in window if s in lo)
    t.append(
        f"Процес rank {P_MAX - 1} завершився викликом os._exit(1) на початку кроку {S_F}, "
        f"тому сам крок {S_F} до відмови не виконувався і в прогоні з відмовою виконаний "
        f"один раз — уже після відновлення. Двічі виконані кроки "
        f"{fs['ckpt_step'] + 1}-{fs['steps_done_before_fail']}: до відмови і повторно "
        f"після відновлення з контрольної точки. У рядку «з відмовою» стоять втрати "
        f"прогону після відновлення (у `result.json` лишається історія останньої спроби; "
        f"втрати обох спроб — у журналі подій). Втрати на кроках "
        f"{window[0]}-{window[-1]} у двох прогонах "
        + ("збігаються побітово" if same else "відрізняються") + ", ACC після "
        f"{N_EPOCHS} епох "
        + ("однаковий" if fail_result.get("acc") == ok_result.get("acc") else "відрізняється")
        + ".")
    t.append("")
    t.append(
        f"Складові t_lost: t_f − t_c = {num(fs['t_f_minus_t_c'], 2)} с (робота від "
        f"контрольної точки на кроці {fs['ckpt_step']} до відмови на кроці {S_F}), "
        f"t_r = {num(fs['t_r'], 2)} с (від відмови до завершення першого кроку після "
        f"відновлення). У t_r найбільша частина — зупинка групи агентом torchrun: "
        f"про відмову процесу агент записав через {num(fs['detect_after_fail'], 2)} с після "
        f"неї, перезапущені процеси почали роботу через {num(fs['restart_boot'], 2)} с, "
        f"а читання контрольної точки і перший крок після нього зайняли "
        f"{num(fs['resume_to_first_step'], 2)} с. Середня втрачена робота за умовою — "
        f"K_c / 2 = {num(K_C / 2, 1)} кроку.")
    if ctrl_result:
        lc = losses_by_step(ctrl_result)
        same_ctrl = all(lc.get(s) == lo.get(s) for s in window if s in lo)
        t.append("")
        t.append(
            "Рядок «без відмови» — прогін DDP кроку 2 (без контрольних точок). "
            "Контрольний прогін тієї самої конфігурації з контрольними точками кожні "
            f"K_c = {K_C} кроків дає на цих кроках "
            + ("ті самі втрати" if same_ctrl else "інші втрати")
            + f" і ACC {num(ctrl_result.get('acc'), 4)}: запис контрольних точок на "
              "навчання не впливає.")
    return "\n".join(t)


def spread_section(speeds):
    """Розкид між прогонами: що саме стоїть за медіанами таблиць 2-4."""
    rows = []
    for label, sp in speeds:
        if not sp:
            continue
        rows.append([
            label, num(sp.n, 0), num(sp.t_step, 2),
            f"{num(min(sp.t_steps), 2)}–{num(max(sp.t_steps), 2)}",
            num(sp.spread(), 0), num(sp.iqr_spread(), 0), num(sp.x, 0),
            f"{num(min(sp.xs), 0)}–{num(max(sp.xs), 0)}", num(sp.spread(sp.xs), 0),
            num(sp.B / sp.t_step * 1e3, 0)])
    return "\n".join([
        "### Розкид між прогонами", "",
        rows_md(["Конфігурація", "Прогонів", "t_step медіана, мс", "t_step від–до, мс",
                 "розкид t_step, % медіани", "розкид між квартилями, % медіани",
                 "X медіана, прикл./с", "X від–до", "розкид X, %",
                 "B / t_step, прикл./с"], rows), "",
        "Найбільші значення розкиду створюють поодинокі прогони, у яких машина була "
        "зайнята стороннім; розкид між квартилями показує, як розходяться прогони без цих "
        "викидів. Медіана, яка стоїть у таблицях, до одного такого прогону нечутлива, а "
        "середнє — ні, тому X за методикою умови (з тривалості 50 кроків) розходиться "
        "між прогонами сильніше за медіану t_step.",
        "",
        "X за методикою умови рахується з тривалості 50 кроків (t_block), тобто з "
        "середнього часу кроку, і тому чутливіша до поодиноких довгих кроків, ніж медіана "
        "t_step. Останній стовпець — B / t_step, оцінка тієї самої пропускної здатності з "
        "медіанного кроку; у таблицях 2-4 стоїть X за умовою, а ця оцінка наведена для "
        "порівняння стійкості.",
    ])


def conditions_section(items):
    rows = []
    for label, rs in items:
        conds = [r.get("host", {}).get("conditions_before", {}) for r in rs if r]
        if not conds:
            continue
        ac = "так" if all(c.get("ac_power") for c in conds) else "НІ"
        saver = "ні" if not any(c.get("battery_saver") for c in conds) else "ТАК"
        timer = num(conds[0].get("timer_resolution_ms"), 1)
        bg = ", ".join(num(c.get("background_cpu_percent"), 1) for c in conds)
        rows.append([label, ac, saver, timer, bg])
    return "\n".join(["### Умови прогонів", "",
                      rows_md(["Прогін", "Від мережі", "Економія заряду",
                               "Таймер Windows, мс",
                               "Фонове завантаження CPU перед прогоном, %"], rows)])


# ---------------------------------------------------------------- збірка


def build(out_dir):
    stand = load(os.path.join(out_dir, "stand.json"))
    single_speed = Speed(runs(out_dir, "s1_single_speed"), "1 процес, батч B")
    manual = Speed(runs(out_dir, "s2_speed_manual"), "ручна синхронізація")
    ddp = Speed(runs(out_dir, "s2_speed_ddp"), "DDP")
    nosync = Speed(runs(out_dir, "s2_speed_ddp_nosync"), "DDP, no_sync")
    equiv = load(os.path.join(out_dir, "s2_equiv.json"))
    train_single = load(os.path.join(out_dir, "s1_single_train.json"))
    train_ddp = load(os.path.join(out_dir, "s2_train_ddp.json"))

    strong = {}
    for p in range(1, P_MAX + 1):
        if p == P_MAX:
            sp, ns = ddp, nosync
        else:
            sp = Speed(runs(out_dir, f"s3_speed_ddp_p{p}"), f"strong P = {p}")
            ns = Speed(runs(out_dir, f"s3_speed_ddp_nosync_p{p}"), f"no_sync P = {p}")
        if sp and ns:
            strong[p] = (sp, ns)

    weak = {}
    for p in range(1, P_MAX + 1):
        sp = ddp if p == P_MAX else Speed(runs(out_dir, f"s4_speed_weak_p{p}"),
                                          f"weak P = {p}")
        if sp:
            weak[p] = sp

    t_p1_b = load(os.path.join(out_dir, "s4_train_p1_b.json"))
    t_lin = load(os.path.join(out_dir, "s4_train_lin.json"))
    fail_result = load(os.path.join(out_dir, "s5_failure", "result.json"))
    ok_result = load(os.path.join(out_dir, "s5_nofail", "result.json"))
    fs = failure_summary(os.path.join(out_dir, "s5_failure"))

    parts = [
        f"# Таблиці ЛР6 (варіант {VARIANT['number']}: {VARIANT['dataset']} DermaMNIST, "
        f"{VARIANT['width']}, P_max = {P_MAX}, B = {B}, K_c = {K_C}, s_f = {S_F})",
        "",
        "Кожна виміряна величина таблиць 2-4 — медіана цієї величини за прогонами "
        "конфігурації (t_step усередині прогону вже є медіаною 50 вимірюваних кроків за "
        "методикою умови). Похідні величини — t_comm,exp, c, S, E, e, S_w, E_w — "
        "пораховані за формулами умови з цих медіан. Прогони навчання детерміновані "
        "(torch.manual_seed(0), DistributedSampler із зерном 0), тож ACC, AUC і втрати "
        "знято по одному прогону на режим.",
        "",
    ]
    if stand:
        parts += [table1(stand), ""]
    if single_speed and manual and ddp and nosync and equiv and train_single and train_ddp:
        parts += [table2(single_speed, manual, ddp, nosync, equiv, train_single,
                         train_ddp), ""]
    if strong:
        parts += [table3(strong), ""]
    if weak:
        parts += [table4(weak), ""]
    if t_p1_b and train_ddp and t_lin:
        parts += [table5(t_p1_b, train_ddp, t_lin), ""]
    if fs and fail_result and train_ddp:
        parts += [table6(fs, fail_result, train_ddp, ok_result), ""]

    speeds = [("1 процес, батч B", single_speed),
              ("P_max, ручна синхронізація", manual),
              ("P_max, DDP", ddp),
              ("P_max, DDP, no_sync", nosync)]
    for p in range(1, P_MAX):
        speeds += [
            (f"strong, P = {p}", Speed(runs(out_dir, f"s3_speed_ddp_p{p}"), "")),
            (f"strong, no_sync, P = {p}",
             Speed(runs(out_dir, f"s3_speed_ddp_nosync_p{p}"), "")),
            (f"weak, P = {p}", Speed(runs(out_dir, f"s4_speed_weak_p{p}"), "")),
        ]
    parts += [spread_section(speeds), ""]

    cond_items = [("1 процес, батч B (швидкість)", single_speed.raw),
                  ("P_max, ручна синхронізація", manual.raw),
                  ("P_max, DDP", ddp.raw),
                  ("P_max, DDP, no_sync", nosync.raw)]
    for p in range(1, P_MAX):
        cond_items.append((f"strong, P = {p}",
                           Speed(runs(out_dir, f"s3_speed_ddp_p{p}"), "").raw))
        cond_items.append((f"weak, P = {p}",
                           Speed(runs(out_dir, f"s4_speed_weak_p{p}"), "").raw))
    cond_items.append(("навчання, 1 процес", [train_single] if train_single else []))
    cond_items.append(("навчання, DDP на P_max, η₀", [train_ddp] if train_ddp else []))
    cond_items.append(("навчання, P = 1, батч b", [t_p1_b] if t_p1_b else []))
    cond_items.append(("навчання, DDP на P_max, η_P", [t_lin] if t_lin else []))
    cond_items.append(("прогін з відмовою", [fail_result] if fail_result else []))
    parts += [conditions_section(cond_items), ""]

    path = os.path.join(out_dir, "tables.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    summary = {
        "variant": VARIANT,
        "n_th": stand["n_th"] if stand else None,
        "n_params": stand["n_params"] if stand else None,
        "M_mib": stand["M_mib"] if stand else None,
        "speed": {
            key: {"t_step_ms": sp.t_step, "t_sync_ms": sp.t_sync, "X": sp.x,
                  "X_from_median_step": sp.B / sp.t_step * 1e3, "runs": sp.n,
                  "t_steps_ms": sp.t_steps, "t_step_spread_percent": sp.spread(),
                  "X_spread_percent": sp.spread(sp.xs)}
            for key, sp in (("single_B", single_speed), ("manual", manual),
                            ("ddp", ddp), ("ddp_nosync", nosync))
        },
        "t_comm_exp_ms": paired_diff(ddp, nosync)[0],
        "t_comm_exp_ms_median_difference": (ddp.t_step - nosync.t_step)
        if (ddp and nosync) else None,
        "t_comm_exp_pairs_ms": paired_diff(ddp, nosync)[2],
        "c": (paired_diff(ddp, nosync)[0] / ddp.t_step) if ddp else None,
        "strong": {p: {"b": s.b, "t_step_ms": s.t_step, "t_step_ns_ms": n.t_step,
                       "X": s.x, "S": s.x / strong[1][0].x,
                       "E": s.x / strong[1][0].x / p,
                       "e": karp_flatt(s.x / strong[1][0].x, p),
                       "t_comm_exp_ms": paired_diff(s, n)[0],
                       "c": paired_diff(s, n)[0] / s.t_step}
                   for p, (s, n) in strong.items()},
        "weak": {p: {"B_P": s.B, "t_step_ms": s.t_step, "X": s.x,
                     "S_w": s.x / weak[1].x, "E_w": s.x / weak[1].x / p}
                 for p, s in weak.items()},
        "equiv": {k: equiv[k] for k in ("max_rel_grad_diff", "weight_sum_spread")
                  } if equiv else None,
        "quality": {
            "single_B": {"acc": train_single["acc"], "auc": train_single["auc"],
                         "steps": train_single["steps_total"]} if train_single else None,
            "ddp_B_eta0": {"acc": train_ddp["acc"], "auc": train_ddp["auc"],
                           "steps": train_ddp["steps_total"]} if train_ddp else None,
            "p1_b_eta0": {"acc": t_p1_b["acc"], "auc": t_p1_b["auc"],
                          "steps": t_p1_b["steps_total"]} if t_p1_b else None,
            "ddp_B_lin": {"acc": t_lin["acc"], "auc": t_lin["auc"], "lr": t_lin["lr"],
                          "steps": t_lin["steps_total"]} if t_lin else None,
        },
        "failure": fs,
    }
    spath = os.path.join(out_dir, "summary.json")
    with open(spath, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"записано {path} і {spath}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="results/lab6")
    a = ap.parse_args(argv)
    utf8_console()
    build(a.out_dir)


if __name__ == "__main__":
    main()
