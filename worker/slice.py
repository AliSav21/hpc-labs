"""Синтетичний зріз і крок обробки (лабораторна робота 4, варіант 19).

Зріз S × S значень від 0 до 4095 генерується з seed лінійним конгруентним генератором,
тому однаковий seed дає однаковий зріз у будь-якій мові і результат задачі детермінований.

Крок обробки — перетворення першого порядку: кожен піксель результату залежить лише від
того самого пікселя входу і від межі вікна яскравості, обчисленої за гістограмою всього
зрізу. Лише цикли мови: без NumPy і Numba, як вимагає умова.
"""
from math import sqrt

SLICE_SIZE = 256          # S за умовою, однаковий для всіх варіантів
LEVELS = 4096             # 12 бітів
MASK31 = 0x7FFFFFFF       # mod 2^31
A, C = 1103515245, 12345
SEED_MAX = 1 << 31


class SliceError(ValueError):
    """Зріз або його гістограма не придатні для кроку обробки."""


def make_slice(seed, size=SLICE_SIZE):
    """x(0) = seed, x(i+1) = (A·x(i) + C) mod 2^31, p(i) = x(i+1) >> 19.

    Зсув на 19 лишає 12 старших бітів із 31, тобто значення 0..4095.

    Домен генератора — 1 ≤ seed < 2^31: саме такі значення видає сервіс web. seed = 0
    лежить поза доменом, і крок обробки на ньому викидає виняток (сценарій відмови Ф3).
    """
    if not isinstance(seed, int) or not 1 <= seed < SEED_MAX:
        raise SliceError(f"seed {seed} поза доменом генератора 1..2^31-1")
    n = size * size
    px = [0] * n
    x = seed
    for i in range(n):
        x = (A * x + C) & MASK31
        px[i] = x >> 19
    return px


def histogram(px):
    """Гістограма зрізу з 4096 рівнів."""
    hist = [0] * LEVELS
    for p in px:
        hist[p] += 1
    return hist


def quantile_level(hist, n, q):
    """Найменший рівень, на якому накопичена частка гістограми досягає q."""
    need = q * n
    acc = 0
    for v in range(LEVELS):
        acc += hist[v]
        if acc >= need:
            return v
    return LEVELS - 1


def process(px, q_lo, q_hi):
    """Вікно яскравості за квантилями і розтягування на весь діапазон.

        p' = 0,                                     якщо p < p_lo або p > p_hi
        p' = ⌊4095 · (p − p_lo) / (p_hi − p_lo)⌉,   якщо p_lo ≤ p ≤ p_hi

    Повертає p_lo, p_hi, кількість вилучених пікселів, середнє і стандартне відхилення
    значень p' за пікселями, що лишилися.
    """
    n = len(px)
    hist = histogram(px)
    p_lo = quantile_level(hist, n, q_lo)
    p_hi = quantile_level(hist, n, q_hi)
    if p_hi <= p_lo:
        raise SliceError(f"вироджене вікно яскравості: p_lo = {p_lo}, p_hi = {p_hi}")
    span = p_hi - p_lo
    two_span = 2 * span
    dropped = 0
    kept = 0
    total = 0
    total_sq = 0
    for p in px:
        if p < p_lo or p > p_hi:
            dropped += 1
            continue
        # округлення до найближчого цілого без плаваючої коми: ⌊a/b⌉ = (2a + b) // 2b
        v = (2 * 4095 * (p - p_lo) + span) // two_span
        kept += 1
        total += v
        total_sq += v * v
    mean = total / kept
    var = total_sq / kept - mean * mean
    return {
        "p_lo": p_lo,
        "p_hi": p_hi,
        "dropped": dropped,
        "kept": kept,
        "mean": mean,
        "sd": sqrt(var) if var > 0 else 0.0,
        "pixels": n,
    }


def run(seed, q_lo, q_hi, size=SLICE_SIZE):
    """Повний крок обробки задачі: зріз із seed і точкове перетворення за гістограмою."""
    return process(make_slice(seed, size), q_lo, q_hi)


if __name__ == "__main__":
    import json
    import sys
    import time

    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    q_lo = float(sys.argv[2]) if len(sys.argv) > 2 else 0.02
    q_hi = float(sys.argv[3]) if len(sys.argv) > 3 else 0.98
    t0 = time.perf_counter()
    out = run(seed, q_lo, q_hi)
    out["t_cpu_s"] = round(time.perf_counter() - t0, 6)
    print(json.dumps(out, ensure_ascii=False, indent=2))
