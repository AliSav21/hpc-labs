"""Самоперевірка ЛР5: коректність структури, замків і формул стенду.

Кожна перевірка має очікуваний і фактичний результат; підсумок пишеться в
``results/lab5/checks.md`` і ``results/lab5/checks.json``. Код виходу 0 — усі
перевірки, що мають вердикт, пройдені.

Запуск: python -m scripts.lab5_selfcheck
"""

import argparse
import glob
import json
import math
import os
import random
import statistics
import sys
import threading
import time

from lab5 import bench
from lab5.rwlock import GlobalLock, RWLock
from lab5.structure import BoundedDeque, InvariantError, Node

RESULTS = []


def check(name, expected, actual, ok):
    """ok=None — інформаційний пункт без вердикту."""
    RESULTS.append({"name": name, "expected": expected, "actual": actual, "ok": ok})
    mark = "ІНФО" if ok is None else ("OK" if ok else "ПОМИЛКА")
    print(f"[{mark}] {name}: {actual}", flush=True)


# ---------------------------------------------------------------- структура


def check_differential(ops=200_000, capacity=1024, seed=19):
    """Дек проти еталонної моделі на списку Python: кожне повернене значення і розмір."""
    rnd = random.Random(seed)
    dq = BoundedDeque(capacity)
    model = []
    mismatches = 0
    value = 0
    for _ in range(ops):
        op = rnd.randrange(7)
        if op == 0:
            value += 1
            exp = len(model) < capacity
            if exp:
                model.insert(0, value)
            got = dq.push_front(value)
        elif op == 1:
            value += 1
            exp = len(model) < capacity
            if exp:
                model.append(value)
            got = dq.push_back(value)
        elif op == 2:
            exp = model.pop(0) if model else None
            got = dq.pop_front()
        elif op == 3:
            exp = model.pop() if model else None
            got = dq.pop_back()
        elif op == 4:
            exp = model[0] if model else None
            got = dq.peek_front()
        elif op == 5:
            exp = model[-1] if model else None
            got = dq.peek_back()
        else:
            exp = len(model)
            got = dq.size()
        if exp != got or dq.size() != len(model):
            mismatches += 1
    inv = dq.check_invariants()
    order_ok = [n for n in _walk(dq)] == model
    check("Дек збігається з еталонною моделлю на 200 000 випадкових операцій",
          "0 розбіжностей, порядок вузлів = модель, інваріанти виконані",
          f"{mismatches} розбіжностей, порядок {'збігається' if order_ok else 'НЕ збігається'}, "
          f"розмір {inv['size']}, найбільший {inv['max_size']}",
          mismatches == 0 and order_ok and inv["ok"])


def _walk(dq):
    node = dq._head
    while node is not None:
        yield node.value
        node = node.next


def check_capacity():
    dq = BoundedDeque(1024)
    accepted = sum(dq.push_back(i) for i in range(1024))
    over_back = dq.push_back(-1)
    over_front = dq.push_front(-2)
    inv = dq.check_invariants()
    check("Межа місткості 1024",
          "1024 push прийнято, 1025-й з обох кінців відхилено, розмір 1024",
          f"прийнято {accepted}, 1025-й: back={over_back}, front={over_front}, "
          f"розмір {inv['size']}, відмов {inv['rejected_full']}",
          accepted == 1024 and not over_back and not over_front and inv["size"] == 1024)


def check_empty():
    dq = BoundedDeque(1024)
    res = (dq.pop_front(), dq.pop_back(), dq.peek_front(), dq.peek_back(), dq.size())
    inv = dq.check_invariants()
    check("Операції на порожньому деку",
          "pop і peek повертають None, size 0, інваріанти виконані",
          f"{res}, pop на порожньому {inv['pops_on_empty']}",
          res == (None, None, None, None, 0) and inv["ok"])


def check_invariant_detector():
    """Перевірка інваріантів має ловити кожне пошкодження списку."""
    def fresh():
        dq = BoundedDeque(16)
        for i in range(5):
            dq.push_back(i)
        return dq

    cases = {}
    dq = fresh()
    dq._head.next.prev = None
    cases["розірване prev"] = dq
    dq = fresh()
    dq._head.next.next = dq._head.next.next.next  # вузол випав із ланцюга вперед
    cases["пропущений вузол"] = dq
    dq = fresh()
    dq._size = 4
    cases["хибний розмір"] = dq
    dq = fresh()
    dq._head.prev = Node(99)
    cases["prev у голови"] = dq
    dq = fresh()
    dq._tail.next = dq._head
    cases["цикл через хвіст"] = dq
    dq = fresh()
    dq._size = 17
    cases["розмір понад місткість"] = dq

    caught = []
    for name, d in cases.items():
        try:
            d.check_invariants()
        except InvariantError:
            caught.append(name)
    check("Перевірка інваріантів ловить пошкодження",
          f"усі {len(cases)} пошкоджень дають InvariantError",
          f"спіймано {len(caught)} з {len(cases)}: {', '.join(caught)}",
          len(caught) == len(cases))


# ---------------------------------------------------------------- замки


def _exclusion_probe(lock, n_w, n_r, seconds, hold=0.0003):
    """Потоки тримають замок із time.sleep усередині (sleep відпускає GIL, тож
    перекриття, якщо замок його допускає, справді станеться) і рахують, хто всередині."""
    state = {"r": 0, "w": 0, "max_r": 0, "max_w": 0, "bad": 0, "ops_r": 0, "ops_w": 0}
    guard = threading.Lock()
    stop = [False]

    def writer():
        while not stop[0]:
            lock.acquire_write()
            with guard:
                state["w"] += 1
                if state["w"] > 1 or state["r"] > 0:
                    state["bad"] += 1
                state["max_w"] = max(state["max_w"], state["w"])
            time.sleep(hold)
            with guard:
                state["w"] -= 1
                state["ops_w"] += 1
            lock.release_write()

    def reader():
        while not stop[0]:
            lock.acquire_read()
            with guard:
                state["r"] += 1
                if state["w"] > 0:
                    state["bad"] += 1
                state["max_r"] = max(state["max_r"], state["r"])
            time.sleep(hold)
            with guard:
                state["r"] -= 1
                state["ops_r"] += 1
            lock.release_read()

    ths = ([threading.Thread(target=writer, daemon=True) for _ in range(n_w)]
           + [threading.Thread(target=reader, daemon=True) for _ in range(n_r)])
    for t in ths:
        t.start()
    time.sleep(seconds)
    stop[0] = True
    for t in ths:
        t.join(10)
    state["alive"] = sum(t.is_alive() for t in ths)
    return state


def check_global_exclusion():
    s = _exclusion_probe(GlobalLock(), 3, 3, 1.5)
    inside = max(s["max_r"], s["max_w"])
    check("Глобальний замок: взаємне виключення",
          "усередині не більше одного потоку будь-якої ролі",
          f"найбільше одночасно: читачів {s['max_r']}, писців {s['max_w']}; порушень "
          f"{s['bad']}; операцій {s['ops_w']} + {s['ops_r']}",
          s["bad"] == 0 and inside == 1 and s["alive"] == 0)


def check_rw_exclusion(policy):
    s = _exclusion_probe(RWLock(policy), 3, 3, 1.5)
    name = "перевага читача" if policy == "reader" else "перевага писця"
    check(f"RWLock ({name}): писець виключний, читачі разом",
          "порушень 0; писців одночасно 1; читачів одночасно > 1",
          f"найбільше одночасно: читачів {s['max_r']}, писців {s['max_w']}; порушень "
          f"{s['bad']}; операцій запису {s['ops_w']}, читання {s['ops_r']}",
          s["bad"] == 0 and s["max_w"] == 1 and s["max_r"] > 1 and s["alive"] == 0)
    return s


def check_policy_order(policy):
    """Читач R1 тримає замок; писець W стає в чергу; приходить читач R2.
    Перевага читача: R2 заходить раніше за W. Перевага писця: W раніше за R2."""
    lock = RWLock(policy)
    log = []
    lk = threading.Lock()

    def note(x):
        with lk:
            log.append(x)

    lock.acquire_read()  # R1

    def w():
        lock.acquire_write()
        note("W")
        time.sleep(0.02)
        lock.release_write()

    def r2():
        lock.acquire_read()
        note("R2")
        lock.release_read()

    tw = threading.Thread(target=w, daemon=True)
    tw.start()
    time.sleep(0.05)  # писець уже чекає
    tr = threading.Thread(target=r2, daemon=True)
    tr.start()
    time.sleep(0.05)
    entered_while_r1 = list(log)
    lock.release_read()  # R1 виходить
    tw.join(5)
    tr.join(5)
    if policy == "reader":
        exp = ["R2", "W"]
        name = "перевага читача"
    else:
        exp = ["W", "R2"]
        name = "перевага писця"
    check(f"Політика RWLock ({name}): порядок входу",
          f"читач R1 тримає, писець W чекає, приходить читач R2 → {' → '.join(exp)}",
          f"поки тримав R1, увійшли: {entered_while_r1 or 'ніхто'}; порядок {' → '.join(log)}",
          log == exp)


# ---------------------------------------------------------------- без замка


class NullLock:
    def acquire_read(self):
        pass

    def release_read(self):
        pass

    def acquire_write(self):
        pass

    def release_write(self):
        pass


def _switch_point():
    """Порожній виклик функції: місце, де CPython 3.10 може перемкнути потік."""
    return None


class YieldingDeque(BoundedDeque):
    """Той самий дек, але між читанням спільного стану і записом у нього стоїть один
    виклик функції. Логіка операцій та сама, змінено лише те, де можливе перемикання."""

    __slots__ = ()

    def push_back(self, value):
        if self._size >= self.capacity:
            self._rejected += 1
            return False
        node = Node(value)
        tail = self._tail
        _switch_point()
        if tail is None:
            self._head = node
        else:
            node.prev = tail
            tail.next = node
        self._tail = node
        self._size += 1
        self._pushed += 1
        return True

    def pop_front(self):
        node = self._head
        _switch_point()
        if node is None:
            self._empty_pops += 1
            return None
        nxt = node.next
        self._head = nxt
        if nxt is None:
            self._tail = None
        else:
            nxt.prev = None
        node.next = None
        self._size -= 1
        self._popped += 1
        return node.value


def _race_trial(dq_cls, lock, seconds):
    """6 писців без пауз: push_back або pop_front навпіл, потім перевірка інваріантів."""
    dq = dq_cls(1024)
    stop = [False]

    def worker(seed):
        rnd = random.Random(seed)
        acq, rel = lock.acquire_write, lock.release_write
        v = 0
        while not stop[0]:
            v += 1
            acq()
            if rnd.getrandbits(1):
                dq.push_back(v)
            else:
                dq.pop_front()
            rel()

    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(6)]
    for t in ths:
        t.start()
    time.sleep(seconds)
    stop[0] = True
    for t in ths:
        t.join(10)
    try:
        dq.check_invariants()
        return None
    except Exception as e:  # noqa: BLE001 — цикл у списку теж результат
        return f"{type(e).__name__}: {e}"


def check_races(seconds=2.0, tries=5):
    """Чи ловить стенд гонку і чи усувають її замки.

    Справжній код деку без замка на CPython 3.10 не ламається: між читанням і записом
    спільного стану в його операціях немає жодного виклику функції, а саме на викликах
    і зворотних переходах інтерпретатор перемикає потоки. Тому для доказу, що
    перевірка інваріантів бачить гонку, а замки її прибирають, у дек вставлено одну
    точку перемикання.
    """
    default = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    out = {}
    try:
        cases = [
            ("справжній дек, без замка", BoundedDeque, NullLock),
            ("дек із точкою перемикання, без замка", YieldingDeque, NullLock),
            ("дек із точкою перемикання, глобальний замок", YieldingDeque, GlobalLock),
            ("дек із точкою перемикання, RWLock читача", YieldingDeque,
             lambda: RWLock("reader")),
            ("дек із точкою перемикання, RWLock писця", YieldingDeque,
             lambda: RWLock("writer")),
        ]
        for name, cls, mk in cases:
            out[name] = [_race_trial(cls, mk(), seconds) for _ in range(tries)]
    finally:
        sys.setswitchinterval(default)

    def broken(name):
        return [o for o in out[name] if o]

    real = broken("справжній дек, без замка")
    check("Справжній дек без замка, інтервал перемикання 1 мкс",
          "інформаційно: чи відтворюється гонка на цьому інтерпретаторі",
          f"порушено в {len(real)} з {tries} прогонів по {seconds:.0f} с",
          None)
    inj = broken("дек із точкою перемикання, без замка")
    check("Перевірка інваріантів бачить справжню гонку",
          f"дек з однією точкою перемикання без замка ламається хоча б в одному з {tries}",
          f"порушено в {len(inj)} з {tries}" + (f"; приклад: {inj[0]}" if inj else ""),
          len(inj) > 0)
    locked = {k: len(broken(k)) for k in out if "замок" in k or "RWLock" in k}
    locked.pop("дек із точкою перемикання, без замка", None)
    check("Обидва замки усувають цю гонку",
          f"0 порушень з {tries} для кожного замка",
          "; ".join(f"{k.split(', ', 1)[1]}: {v} з {tries}" for k, v in locked.items()),
          all(v == 0 for v in locked.values()))
    return out


# ---------------------------------------------------------------- суміш операцій


class CountingDeque(BoundedDeque):
    __slots__ = ("calls",)

    def __init__(self, capacity):
        super().__init__(capacity)
        self.calls = {}

    def _c(self, k):
        self.calls[k] = self.calls.get(k, 0) + 1

    def push_front(self, v):
        self._c("push_front")
        return super().push_front(v)

    def push_back(self, v):
        self._c("push_back")
        return super().push_back(v)

    def pop_front(self):
        self._c("pop_front")
        return super().pop_front()

    def pop_back(self):
        self._c("pop_back")
        return super().pop_back()

    def peek_front(self):
        self._c("peek_front")
        return super().peek_front()

    def peek_back(self):
        self._c("peek_back")
        return super().peek_back()

    def size(self):
        self._c("size")
        return super().size()


def check_op_mix(seconds=2.0):
    """Частки операцій запису і читання — саме ті, що в умові варіанта.
    Лічильник один на структуру і без замка, тож прогін іде під глобальним замком."""
    dq = CountingDeque(1024)
    lock = GlobalLock()
    state = [bench.MEASURE]
    res = [{} for _ in range(6)]
    ths = []
    for i in range(6):
        rnd = random.Random(500 + i)
        if i % 2 == 0:
            ths.append(threading.Thread(target=bench.writer_loop,
                                        args=(dq, lock, state, rnd, res[i], "random")))
        else:
            ths.append(threading.Thread(target=bench.reader_loop,
                                        args=(dq, lock, state, rnd, res[i])))
    for th in ths:
        th.start()
    time.sleep(seconds)
    state[0] = bench.STOP
    for th in ths:
        th.join(10)
    c = dq.calls
    w_total = c.get("push_front", 0) + c.get("push_back", 0) + c.get("pop_front", 0) + c.get("pop_back", 0)
    r_total = c.get("peek_front", 0) + c.get("peek_back", 0) + c.get("size", 0)
    push = (c.get("push_front", 0) + c.get("push_back", 0)) / w_total
    front = (c.get("push_front", 0) + c.get("pop_front", 0)) / w_total
    reads = [c.get(k, 0) / r_total for k in ("peek_front", "peek_back", "size")]
    counted_w = sum(res[i]["ops"] for i in range(0, 6, 2))
    counted_r = sum(res[i]["ops"] for i in range(1, 6, 2))
    # біноміальна похибка частки: 4 сигми
    tol = 4 * math.sqrt(0.25 / w_total)
    ok = (abs(push - 0.5) < tol and abs(front - 0.5) < tol
          and max(reads) - min(reads) <= 3 / r_total + 1e-12
          and counted_w == w_total and counted_r == r_total)
    check("Суміш операцій за умовою варіанта",
          "push : pop = 1 : 1, початок : хвіст = 1 : 1 (у межах 4σ); peek_front, peek_back, "
          "size рівно по третині; лічильник потоків = кількість викликів структури",
          f"push {push:.4f}, початок {front:.4f} (допуск ±{tol:.4f}); читання "
          f"{reads[0]:.5f} / {reads[1]:.5f} / {reads[2]:.5f}; викликів запису {w_total}, "
          f"пораховано потоками {counted_w}; читання {r_total} / {counted_r}",
          ok)


# ---------------------------------------------------------------- формули


def check_percentiles():
    data = [list(range(1, 101)), list(range(1, 201)), [5.0], list(range(1, 11))]
    got = []
    for d in data:
        arr = bench.array("d", d)
        p = bench.percentiles([arr])
        got.append((p["p50"], p["p99"]))
    exp = [(50.0, 99.0), (100.0, 198.0), (5.0, 5.0), (5.0, 10.0)]
    # той самий ряд, розбитий на кілька потоків і перемішаний
    rnd = random.Random(3)
    vals = list(range(1, 1001))
    rnd.shuffle(vals)
    parts = [bench.array("d", vals[i::3]) for i in range(3)]
    p = bench.percentiles(parts)
    split_ok = (p["p50"], p["p99"]) == (500.0, 990.0)
    check("Перцентиль p_q = τ_(⌈q·N⌉)",
          f"{exp} і (500, 990) на 1..1000, розбитому між трьома потоками",
          f"{got}; розбитий ряд: ({p['p50']:.0f}, {p['p99']:.0f})",
          got == exp and split_ok)


def check_timer(calls=1_000_000):
    """Крок таймера і вартість одного виклику: обидва входять у виміряні t_cs і τ."""
    pc = time.perf_counter
    deltas = []
    for _ in range(200_000):
        a = pc()
        b = pc()
        deltas.append(b - a)
    steps = sorted(set(round(d * 1e9) for d in deltas if d > 0))[:3]
    costs = []
    for _ in range(5):
        t0 = pc()
        for _ in range(calls):
            pc()
        costs.append((pc() - t0) / calls * 1e9)
    cost = statistics.median(costs)
    info = time.get_clock_info("perf_counter")
    check("Таймер perf_counter",
          "інформаційно: крок таймера і скільки один його виклик додає до t_cs",
          f"роздільність за системою {info.resolution * 1e9:.0f} нс; найменші ненульові "
          f"різниці {steps} нс; один виклик ≈ {cost:.0f} нс (медіана 5 серій по "
          f"{calls} викликів)",
          None)
    return {"resolution_ns": info.resolution * 1e9, "steps_ns": steps, "call_ns": cost}


def check_recompute(out_dir):
    """Незалежний перерахунок метрик із сирих прогонів проти того, що записав стенд."""
    worst = 0.0
    n_checked = 0
    problems = []
    for path in sorted(glob.glob(os.path.join(out_dir, "threads_*.json"))):
        for run in json.load(open(path, encoding="utf-8")):
            d = run["duration_s"]
            for role, k in (("write", run["n_w"]), ("read", run["n_r"])):
                st = run[role]
                counts = [t["ops"] for t in run["per_thread"]
                          if t["role"] == role]
                n_ops = sum(counts)
                mu = n_ops / k
                cv = math.sqrt(sum((c - mu) ** 2 for c in counts) / k) / mu
                pairs = [
                    (st["ops"], n_ops),
                    (st["X"], n_ops / d),
                    (st["x_per_thread"], n_ops / d / k),
                    (st["w"], st["wait_sum_s"] / (k * d)),
                    (st["cv"], cv),
                    (st["h"], st["p99_us"] / st["p50_us"]),
                ]
                for a, b in pairs:
                    rel = abs(a - b) / abs(b) if b else abs(a - b)
                    worst = max(worst, rel)
                    n_checked += 1
                    if rel > 1e-9:
                        problems.append((os.path.basename(path), role, a, b))
                if not 0 <= st["w"] <= 1:
                    problems.append((os.path.basename(path), role, "w поза 0..1", st["w"]))
            tot = run["total"]
            phi = run["write"]["X"] / (run["write"]["X"] + run["read"]["X"])
            for a, b in ((tot["X"], run["write"]["X"] + run["read"]["X"]),
                         (tot["phi_w"], phi), (tot["phi_w"] + tot["phi_r"], 1.0)):
                rel = abs(a - b) / abs(b)
                worst = max(worst, rel)
                n_checked += 1
                if rel > 1e-9:
                    problems.append((os.path.basename(path), "total", a, b))

    s = json.load(open(os.path.join(out_dir, "summary.json"), encoding="utf-8"))
    g, rw = s["global"], s["rw"]
    derived = {
        "S_n_global": g["total"]["X"] / s["x1_global"],
        "E_n_global": g["total"]["X"] / s["x1_global"] / g["n"],
        "S_n_rw": rw["total"]["X"] / s["x1_rw"],
        "E_n_rw": rw["total"]["X"] / s["x1_rw"] / rw["n"],
        "S_w": rw["write"]["X"] / g["write"]["X"],
        "S_r": rw["read"]["X"] / g["read"]["X"],
        "S_total": rw["total"]["X"] / g["total"]["X"],
        "rho": g["total"]["X"] * s["t_cs_bar_us"] / 1e6,
        "t_cs_bar_us": (g["n_w"] * s["t_cs_w_us"] + g["n_r"] * s["t_cs_r_us"]) / g["n"],
        "x_limit": 1e6 / s["t_cs_bar_us"],
    }
    for k, v in derived.items():
        rel = abs(s[k] - v) / abs(v)
        worst = max(worst, rel)
        n_checked += 1
        if rel > 1e-9:
            problems.append(("summary.json", k, s[k], v))
    check("Незалежний перерахунок X, x, w, cv, h, φ, S, E, ρ, t̄_cs із сирих прогонів",
          "збіг із записаним стендом до 1e-9 відносної похибки, w у межах 0..1",
          f"перевірено {n_checked} величин у 15 прогонах і summary.json; найбільша "
          f"відносна розбіжність {worst:.1e}; проблем {len(problems)}",
          not problems)


def check_counters_vs_ops(out_dir):
    """Лічильники структури охоплюють і прогрів; різниця з N_w — це операції прогріву."""
    rows = []
    for path in ("threads_global_random.json", "threads_rw_random.json",
                 "threads_rw-writer_random.json"):
        for run in json.load(open(os.path.join(out_dir, path), encoding="utf-8")):
            inv = run["invariants"]
            calls = inv["pushed"] + inv["rejected_full"] + inv["popped"] + inv["pops_on_empty"]
            rows.append((path, calls, run["write"]["ops"], calls - run["write"]["ops"]))
    ok = all(extra > 0 for *_, extra in rows)
    extras = [extra for *_, extra in rows]
    check("Лічильники структури проти N_w",
          "викликів запису в структурі більше за N_w рівно на операції прогріву (> 0)",
          f"надлишок {min(extras)}–{max(extras)} викликів на 9 прогонах",
          ok)
    return rows


# ---------------------------------------------------------------- звіт


def write_md(path, extra):
    lines = ["# Самоперевірка ЛР5 (варіант 19)", "",
             "Згенеровано `python -m scripts.lab5_selfcheck`. ІНФО — пункт без вердикту.", "",
             "| # | Перевірка | Очікувано | Фактично | Вердикт |", "|---|---|---|---|---|"]
    for i, r in enumerate(RESULTS, 1):
        v = "ІНФО" if r["ok"] is None else ("OK" if r["ok"] else "ПОМИЛКА")
        lines.append(f"| {i} | {r['name']} | {r['expected']} | {r['actual']} | {v} |")
    passed = sum(1 for r in RESULTS if r["ok"])
    total = sum(1 for r in RESULTS if r["ok"] is not None)
    lines += ["", f"**Пройдено {passed} з {total}**; інформаційних пунктів "
              f"{sum(1 for r in RESULTS if r['ok'] is None)}.", ""]
    lines += extra
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Самоперевірка ЛР5")
    ap.add_argument("--out-dir", default="results/lab5")
    a = ap.parse_args(argv)

    check_differential()
    check_capacity()
    check_empty()
    check_invariant_detector()
    check_global_exclusion()
    rw_r = check_rw_exclusion("reader")
    rw_w = check_rw_exclusion("writer")
    check_policy_order("reader")
    check_policy_order("writer")
    check_op_mix()
    check_percentiles()
    check_recompute(a.out_dir)
    rows = check_counters_vs_ops(a.out_dir)
    timer = check_timer()
    races = check_races()

    extra = ["## Лічильники структури і прогрів", "",
             "| Файл | Викликів запису в структурі | N_w за D | Різниця (прогрів) |",
             "|---|---|---|---|"]
    for path, calls, nw, ex in rows:
        extra.append(f"| {path} | {calls} | {nw} | {ex} |")
    extra += ["", "## Гонка без замка і з замками", "",
              "Інтервал перемикання потоків 1 мкс, 6 писців без пауз, 5 прогонів по 2 с.", "",
              "| Варіант | Прогонів з порушенням | Перше порушення |", "|---|---|---|"]
    for name, outs in races.items():
        bad = [o for o in outs if o]
        extra.append(f"| {name} | {len(bad)} з {len(outs)} | {bad[0] if bad else '—'} |")
    write_md(os.path.join(a.out_dir, "checks.md"), extra)
    with open(os.path.join(a.out_dir, "checks.json"), "w", encoding="utf-8") as f:
        json.dump({"checks": RESULTS, "timer": timer,
                   "rw_reader_probe": rw_r, "rw_writer_probe": rw_w,
                   "races": races,
                   "counters": [dict(zip(("file", "calls", "N_w", "extra"), r))
                                for r in rows]},
                  f, ensure_ascii=False, indent=2)
    failed = [r for r in RESULTS if r["ok"] is False]
    print(f"\nпройдено {sum(1 for r in RESULTS if r['ok'])} з "
          f"{sum(1 for r in RESULTS if r['ok'] is not None)}; "
          f"звіт: {os.path.join(a.out_dir, 'checks.md')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
