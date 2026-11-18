"""Стенд ЛР5: n потоків над деком С9 під глобальним замком або під RWLock.

Потоки з непарними номерами виконують лише запис, з парними — лише читання. Операції
йдуть одна за одною без пауз, аргументи генеруються в потоці до виклику операції.
Для кожної операції записуються тривалість tau = t_ret - t_call і час очікування замка
t_wait = t_acq - t_call; t_call береться до захоплення замка, t_ret — після звільнення.

Режими запуску:
  threads   n потоків (крок 2 і крок 3)
  single    X_1: один потік по черзі виконує записи і читання в пропорції n_w : n_r
  cs        t_cs,w і t_cs,r: час утримання замка без конкуренції
"""

import argparse
import json
import math
import os
import platform
import random
import sys
import threading
import time
from array import array

from lab5.rwlock import make_lock
from lab5.structure import CAPACITY, BoundedDeque

WARMUP = 0
MEASURE = 1
STOP = 2

PERCENTILES = (0.5, 0.99)


# ---------------------------------------------------------------- гарячі цикли


def writer_loop(dq, lock, state, rnd, result, mix="random"):
    """Операція запису С9: push або pop до випадково вибраного кінця.

    ``mix="random"`` — вид операції вибирається випадково, порівну між push і pop;
    розмір деку тоді робить випадкове блукання в межах 0..capacity.
    ``mix="pair"`` — строге чергування push, pop, як у С1 і С4 («за яким той самий
    потік виконує take»); розмір деку тоді не відходить від нуля.
    """
    tau = array("d")
    wait = array("d")
    tau_add = tau.append
    wait_add = wait.append
    pc = time.perf_counter
    acquire = lock.acquire_write
    release = lock.release_write
    pushes = (dq.push_back, dq.push_front)
    pops = (dq.pop_back, dq.pop_front)
    getrandbits = rnd.getrandbits
    ops = 0
    value = 0
    turn = 0
    random_mix = mix == "random"
    while True:
        st = state[0]
        if st == STOP:
            break
        # --- аргументи операції готуються тут, до виклику ---
        bits = getrandbits(2)
        end = bits & 1
        if (bits >> 1) if random_mix else turn:
            fn = pops[end]
            args = ()
            turn = 0
        else:
            value += 1
            fn = pushes[end]
            args = (value,)
            turn = 1
        # --- сама операція ---
        t_call = pc()
        acquire()
        t_acq = pc()
        fn(*args)
        release()
        t_ret = pc()
        if st == MEASURE:
            tau_add(t_ret - t_call)
            wait_add(t_acq - t_call)
            ops += 1
    result["ops"] = ops
    result["tau"] = tau
    result["wait"] = wait


def reader_loop(dq, lock, state, rnd, result):
    """Операція читання С9: peek_front(), peek_back(), size() порівну."""
    tau = array("d")
    wait = array("d")
    tau_add = tau.append
    wait_add = wait.append
    pc = time.perf_counter
    acquire = lock.acquire_read
    release = lock.release_read
    reads = (dq.peek_front, dq.peek_back, dq.size)
    ops = 0
    turn = 0
    while True:
        st = state[0]
        if st == STOP:
            break
        fn = reads[turn]
        turn = 0 if turn == 2 else turn + 1
        t_call = pc()
        acquire()
        t_acq = pc()
        fn()
        release()
        t_ret = pc()
        if st == MEASURE:
            tau_add(t_ret - t_call)
            wait_add(t_acq - t_call)
            ops += 1
    result["ops"] = ops
    result["tau"] = tau
    result["wait"] = wait


def single_loop(dq, lock, state, rnd, n_w, n_r, result, mix="random"):
    """X_1: один потік по черзі виконує n_w записів і n_r читань під тим самим замком."""
    tau_w = array("d")
    wait_w = array("d")
    tau_r = array("d")
    wait_r = array("d")
    pc = time.perf_counter
    acq_w, rel_w = lock.acquire_write, lock.release_write
    acq_r, rel_r = lock.acquire_read, lock.release_read
    pushes = (dq.push_back, dq.push_front)
    pops = (dq.pop_back, dq.pop_front)
    reads = (dq.peek_front, dq.peek_back, dq.size)
    getrandbits = rnd.getrandbits
    ops_w = ops_r = 0
    value = 0
    turn = 0
    rturn = 0
    random_mix = mix == "random"
    while True:
        st = state[0]
        if st == STOP:
            break
        for _ in range(n_w):
            bits = getrandbits(2)
            end = bits & 1
            if (bits >> 1) if random_mix else turn:
                fn, args = pops[end], ()
                turn = 0
            else:
                value += 1
                fn, args = pushes[end], (value,)
                turn = 1
            t_call = pc()
            acq_w()
            t_acq = pc()
            fn(*args)
            rel_w()
            t_ret = pc()
            if st == MEASURE:
                tau_w.append(t_ret - t_call)
                wait_w.append(t_acq - t_call)
                ops_w += 1
        for _ in range(n_r):
            fn = reads[rturn]
            rturn = 0 if rturn == 2 else rturn + 1
            t_call = pc()
            acq_r()
            t_acq = pc()
            fn()
            rel_r()
            t_ret = pc()
            if st == MEASURE:
                tau_r.append(t_ret - t_call)
                wait_r.append(t_acq - t_call)
                ops_r += 1
    result["writer"] = {"ops": ops_w, "tau": tau_w, "wait": wait_w}
    result["reader"] = {"ops": ops_r, "tau": tau_r, "wait": wait_r}


def cs_loop(dq, lock, state, n_w, n_r, result, mix="random"):
    """t_cs: час від отримання до звільнення замка, один потік, без конкуренції.

    t_cs = t_rel - t_acq, де t_acq узятий одразу після захоплення, а t_rel — останньою
    дією перед звільненням, тобто сам виклик release у час утримання не входить.
    """
    cs_w = array("d")
    cs_r = array("d")
    pc = time.perf_counter
    acq_w, rel_w = lock.acquire_write, lock.release_write
    acq_r, rel_r = lock.acquire_read, lock.release_read
    pushes = (dq.push_back, dq.push_front)
    pops = (dq.pop_back, dq.pop_front)
    reads = (dq.peek_front, dq.peek_back, dq.size)
    rnd = random.Random(20250519)
    getrandbits = rnd.getrandbits
    value = 0
    turn = 0
    rturn = 0
    random_mix = mix == "random"
    while True:
        st = state[0]
        if st == STOP:
            break
        for _ in range(n_w):
            bits = getrandbits(2)
            end = bits & 1
            if (bits >> 1) if random_mix else turn:
                fn, args = pops[end], ()
                turn = 0
            else:
                value += 1
                fn, args = pushes[end], (value,)
                turn = 1
            acq_w()
            t_acq = pc()
            fn(*args)
            t_rel = pc()
            rel_w()
            if st == MEASURE:
                cs_w.append(t_rel - t_acq)
        for _ in range(n_r):
            fn = reads[rturn]
            rturn = 0 if rturn == 2 else rturn + 1
            acq_r()
            t_acq = pc()
            fn()
            t_rel = pc()
            rel_r()
            if st == MEASURE:
                cs_r.append(t_rel - t_acq)
    result["cs_w"] = cs_w
    result["cs_r"] = cs_r


# ---------------------------------------------------------------- статистика


def _sorted_values(arrays):
    """Злити масиви тривалостей в один відсортований. numpy використовується лише тут,
    щоб не тримати мільйони об'єктів float у списку Python."""
    total = sum(len(a) for a in arrays)
    if total == 0:
        return None
    merged = array("d")
    for a in arrays:
        merged.extend(a)
    try:
        import numpy as np
    except ImportError:
        return sorted(merged)
    buf = np.frombuffer(merged, dtype=np.float64).copy()
    buf.sort()
    return buf


def percentiles(arrays):
    """p_q = tau_(ceil(q*N)) — порядкова статистика, нумерація з одиниці."""
    data = _sorted_values(arrays)
    if data is None:
        return {f"p{int(q * 100)}": None for q in PERCENTILES}
    n = len(data)
    out = {}
    for q in PERCENTILES:
        k = max(1, min(n, math.ceil(q * n)))
        out[f"p{int(q * 100)}"] = float(data[k - 1])
    return out


def role_stats(per_thread, n_role, duration):
    """Зведення по ролі: X, x, перцентилі, w, cv, найбільший t_wait."""
    counts = [t["ops"] for t in per_thread]
    taus = [t["tau"] for t in per_thread]
    waits = [t["wait"] for t in per_thread]
    n_ops = sum(counts)
    wait_sum = sum(float(sum(w)) for w in waits)
    wait_max = max((max(w) for w in waits if len(w)), default=0.0)
    pct = percentiles(taus)
    k = len(counts)
    mu = n_ops / k if k else 0.0
    sigma = math.sqrt(sum((c - mu) ** 2 for c in counts) / k) if k else 0.0
    x = n_ops / duration
    p50, p99 = pct["p50"], pct["p99"]
    return {
        "threads": n_role,
        "ops": n_ops,
        "ops_per_thread": counts,
        "X": x,
        "x_per_thread": x / n_role if n_role else 0.0,
        "p50_us": p50 * 1e6 if p50 is not None else None,
        "p99_us": p99 * 1e6 if p99 is not None else None,
        "h": (p99 / p50) if p50 else None,
        "wait_sum_s": wait_sum,
        "wait_max_ms": wait_max * 1e3,
        "w": wait_sum / (n_role * duration) if n_role else 0.0,
        "mu": mu,
        "sigma": sigma,
        "cv": sigma / mu if mu else 0.0,
    }


def mean(a):
    return float(sum(a)) / len(a) if len(a) else 0.0


# ---------------------------------------------------------------- прогони


def host_info():
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "logical_cores": os.cpu_count(),
    }


def run_threads(lock_kind, n, capacity, warmup, duration, seed, mix="random"):
    n_w = (n + 1) // 2
    n_r = n // 2
    dq = BoundedDeque(capacity)
    lock = make_lock(lock_kind)
    state = [WARMUP]
    results = []
    threads = []
    for tid in range(1, n + 1):
        res = {"thread": tid, "role": "write" if tid % 2 else "read"}
        results.append(res)
        rnd = random.Random(seed + tid)
        if tid % 2:
            target, args = writer_loop, (dq, lock, state, rnd, res, mix)
        else:
            target, args = reader_loop, (dq, lock, state, rnd, res)
        th = threading.Thread(target=target, args=args, name=f"t{tid}", daemon=True)
        threads.append(th)
    for th in threads:
        th.start()
    time.sleep(warmup)
    state[0] = MEASURE
    t0 = time.perf_counter()
    time.sleep(duration)
    t1 = time.perf_counter()
    state[0] = STOP
    for th in threads:
        th.join(60)
    d = t1 - t0

    writers = [r for r in results if r["role"] == "write"]
    readers = [r for r in results if r["role"] == "read"]
    w = role_stats(writers, n_w, d)
    r = role_stats(readers, n_r, d)
    total_ops = w["ops"] + r["ops"]
    x_total = total_ops / d
    both = percentiles([x["tau"] for x in results])
    counts = [x["ops"] for x in results]
    mu = sum(counts) / len(counts)
    sg = math.sqrt(sum((c - mu) ** 2 for c in counts) / len(counts))
    wait_sum = w["wait_sum_s"] + r["wait_sum_s"]
    return {
        "mode": "threads",
        "lock": lock_kind,
        "n": n,
        "n_w": n_w,
        "n_r": n_r,
        "capacity": capacity,
        "write_mix": mix,
        "warmup_s": warmup,
        "duration_s": d,
        "seed": seed,
        "write": w,
        "read": r,
        "total": {
            "ops": total_ops,
            "X": x_total,
            "phi_w": w["X"] / x_total if x_total else 0.0,
            "phi_r": r["X"] / x_total if x_total else 0.0,
            "p50_us": both["p50"] * 1e6 if both["p50"] is not None else None,
            "p99_us": both["p99"] * 1e6 if both["p99"] is not None else None,
            "wait_max_ms": max(w["wait_max_ms"], r["wait_max_ms"]),
            "w": wait_sum / (n * d),
            "cv": sg / mu if mu else 0.0,
        },
        "invariants": dq.check_invariants(),
        "per_thread": [
            {"thread": x["thread"], "role": x["role"], "ops": x["ops"]} for x in results
        ],
        "host": host_info(),
    }


def run_single(lock_kind, n, capacity, warmup, duration, seed, mix="random"):
    n_w = (n + 1) // 2
    n_r = n // 2
    dq = BoundedDeque(capacity)
    lock = make_lock(lock_kind)
    state = [WARMUP]
    res = {}
    rnd = random.Random(seed)
    th = threading.Thread(
        target=single_loop, args=(dq, lock, state, rnd, n_w, n_r, res, mix),
        daemon=True)
    th.start()
    time.sleep(warmup)
    state[0] = MEASURE
    t0 = time.perf_counter()
    time.sleep(duration)
    t1 = time.perf_counter()
    state[0] = STOP
    th.join(60)
    d = t1 - t0
    w = role_stats([res["writer"]], 1, d)
    r = role_stats([res["reader"]], 1, d)
    total = w["ops"] + r["ops"]
    return {
        "mode": "single",
        "lock": lock_kind,
        "n": n,
        "ratio": f"{n_w}:{n_r}",
        "capacity": capacity,
        "write_mix": mix,
        "duration_s": d,
        "write": w,
        "read": r,
        "total": {"ops": total, "X1": total / d},
        "invariants": dq.check_invariants(),
        "host": host_info(),
    }


def run_cs(lock_kind, n, capacity, warmup, duration, mix="random"):
    n_w = (n + 1) // 2
    n_r = n // 2
    dq = BoundedDeque(capacity)
    lock = make_lock(lock_kind)
    state = [WARMUP]
    res = {}
    th = threading.Thread(
        target=cs_loop, args=(dq, lock, state, n_w, n_r, res, mix), daemon=True)
    th.start()
    time.sleep(warmup)
    state[0] = MEASURE
    time.sleep(duration)
    state[0] = STOP
    th.join(60)
    t_cs_w = mean(res["cs_w"])
    t_cs_r = mean(res["cs_r"])
    t_bar = (n_w * t_cs_w + n_r * t_cs_r) / n
    return {
        "mode": "cs",
        "lock": lock_kind,
        "n": n,
        "n_w": n_w,
        "n_r": n_r,
        "capacity": capacity,
        "write_mix": mix,
        "samples_w": len(res["cs_w"]),
        "samples_r": len(res["cs_r"]),
        "t_cs_w_us": t_cs_w * 1e6,
        "t_cs_r_us": t_cs_r * 1e6,
        "t_cs_bar_us": t_bar * 1e6,
        "x_limit": 1.0 / t_bar if t_bar else None,
        "invariants": dq.check_invariants(),
        "host": host_info(),
    }


def print_summary(out):
    """Друк того, що вимагає крок 1: операції кожного потоку, X_w, X_r, p50 і p99 кожної
    ролі, найбільший t_wait запису, w_w і w_r, інваріанти."""
    m = out["mode"]
    print(f"режим {m}, замок {out['lock']}, суміш запису {out.get('write_mix')}, "
          f"D = {out.get('duration_s', 0):.3f} с")
    if m == "cs":
        print(f"  t_cs,w = {out['t_cs_w_us']:.3f} мкс ({out['samples_w']} операцій)")
        print(f"  t_cs,r = {out['t_cs_r_us']:.3f} мкс ({out['samples_r']} операцій)")
        print(f"  t_cs середній = {out['t_cs_bar_us']:.3f} мкс, межа 1/t_cs = "
              f"{out['x_limit']:.0f} оп/с")
    elif m == "single":
        print(f"  X_1 = {out['total']['X1']:.0f} оп/с у пропорції {out['ratio']} "
              f"({out['total']['ops']} операцій)")
    else:
        for t in out["per_thread"]:
            role = "запис" if t["role"] == "write" else "читання"
            print(f"  потік {t['thread']:>2} {role:<8} {t['ops']:>9} операцій")
        w, r, tot = out["write"], out["read"], out["total"]
        print(f"  X_w = {w['X']:.0f} оп/с, X_r = {r['X']:.0f} оп/с, X = {tot['X']:.0f} оп/с")
        print(f"  запис:   p50 {w['p50_us']:.2f} мкс, p99 {w['p99_us']:.2f} мкс, "
              f"w_w = {w['w']:.4f}, cv = {w['cv']:.4f}")
        print(f"  читання: p50 {r['p50_us']:.2f} мкс, p99 {r['p99_us']:.2f} мкс, "
              f"w_r = {r['w']:.4f}, cv = {r['cv']:.4f}")
        print(f"  найбільший t_wait запису = {w['wait_max_ms']:.3f} мс, "
              f"читання = {r['wait_max_ms']:.3f} мс")
        print(f"  phi_w = {tot['phi_w']:.4f}, phi_r = {tot['phi_r']:.4f}")
    inv = out["invariants"]
    print(f"  інваріанти: ok={inv['ok']}, розмір {inv['size']}, найбільший за прогін "
          f"{inv['max_size']} з {inv['capacity']}, покладено {inv['pushed']}, "
          f"вийнято {inv['popped']}, відмов через повноту {inv['rejected_full']}, "
          f"pop на порожньому {inv['pops_on_empty']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Стенд ЛР5: дек С9 під двома замками")
    ap.add_argument("mode", choices=("threads", "single", "cs"))
    ap.add_argument("--lock", default="global", choices=("global", "rw", "rw-writer"))
    ap.add_argument("--n", type=int, default=6, help="кількість потоків стенду")
    ap.add_argument("--capacity", type=int, default=CAPACITY)
    ap.add_argument("--warmup", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=190519)
    ap.add_argument("--write-mix", default="random", choices=("random", "pair"),
                    help="random: push або pop випадково порівну; pair: строге чергування")
    ap.add_argument("--out", help="файл JSON для підсумку")
    a = ap.parse_args(argv)

    if a.mode == "threads":
        out = run_threads(
            a.lock, a.n, a.capacity, a.warmup, a.duration, a.seed, a.write_mix)
    elif a.mode == "single":
        out = run_single(
            a.lock, a.n, a.capacity, a.warmup, a.duration, a.seed, a.write_mix)
    else:
        out = run_cs(a.lock, a.n, a.capacity, a.warmup, a.duration, a.write_mix)

    print_summary(out)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"підсумок: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
