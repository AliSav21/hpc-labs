"""Звірка кроку обробки зрізу з незалежною реалізацією (перевірка ЛР4).

Друга реалізація написана інакше: квантилі через сортування всіх S² значень замість
гістограми, перетворення пікселя з плаваючою комою замість цілочисельного округлення.
Збігатись мають p_lo, p_hi, кількість вилучених і кількість залишених пікселів.

Середнє і стандартне відхилення можуть розійтися на тисячні, і це не помилка: вбудована
round() у Python округлює рівно половину до парного, а формула (2a + b) // 2b у
worker/slice.py — вгору, як і вимагає ⌊ ⌉ з умови. Скрипт рахує, скільки пікселів лягло
рівно на половину, тож розбіжність одразу видно звідки.

    python -m scripts.xcheck_slice
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252

import math
import statistics
from pathlib import Path

sys_path = Path(__file__).resolve().parent.parent
if str(sys_path) not in _sys.path:
    _sys.path.insert(0, str(sys_path))

from worker.slice import (LEVELS, SLICE_SIZE, histogram, make_slice,  # noqa: E402
                          quantile_level, run)

SEEDS = (1, 7, 12345, 999983, 2 ** 31 - 1, 42424242)
Q_LO, Q_HI = 0.02, 0.98


def reference(seed, q_lo, q_hi, size=SLICE_SIZE):
    """Незалежна реалізація: сортування і плаваюча кома."""
    n = size * size
    x = seed
    px = []
    for _ in range(n):
        x = (1103515245 * x + 12345) % (2 ** 31)
        px.append(x >> 19)
    s = sorted(px)
    p_lo = s[max(math.ceil(q_lo * n) - 1, 0)]
    p_hi = s[max(math.ceil(q_hi * n) - 1, 0)]
    kept, dropped = [], 0
    for p in px:
        if p < p_lo or p > p_hi:
            dropped += 1
        else:
            kept.append(round(4095 * (p - p_lo) / (p_hi - p_lo)))
    return {"p_lo": p_lo, "p_hi": p_hi, "dropped": dropped, "kept": len(kept),
            "mean": statistics.fmean(kept), "sd": statistics.pstdev(kept)}


def exact_halves(seed, q_lo, q_hi, size=SLICE_SIZE):
    """Скільки пікселів дають рівно половину і тому залежать від правила округлення."""
    px = make_slice(seed, size)
    h = histogram(px)
    n = len(px)
    p_lo, p_hi = quantile_level(h, n, q_lo), quantile_level(h, n, q_hi)
    span = p_hi - p_lo
    return sum(h[p] for p in range(p_lo, p_hi + 1)
               if (2 * 4095 * (p - p_lo)) % (2 * span) == span)


def main():
    hard, soft = 0, 0
    print(f"зріз {SLICE_SIZE} × {SLICE_SIZE}, рівнів {LEVELS}, "
          f"квантилі {Q_LO} / {Q_HI}\n")
    for seed in SEEDS:
        a, b = run(seed, Q_LO, Q_HI), reference(seed, Q_LO, Q_HI)
        same_hard = all(a[k] == b[k] for k in ("p_lo", "p_hi", "dropped", "kept"))
        same_soft = (abs(a["mean"] - b["mean"]) < 1e-9
                     and abs(a["sd"] - b["sd"]) < 1e-6)
        halves = exact_halves(seed, Q_LO, Q_HI)
        mark = "OK" if same_hard and same_soft else (
            "лише округлення" if same_hard else "РОЗБІЖНІСТЬ")
        print(f"seed={seed:>12}  p_lo={a['p_lo']:4d} p_hi={a['p_hi']:4d} "
              f"вилучено={a['dropped']:5d}  Δmean={a['mean'] - b['mean']:+.6f}  "
              f"пікселів рівно на половині={halves:4d}  {mark}")
        hard += not same_hard
        soft += same_hard and not same_soft

    d1, d2 = run(777, Q_LO, Q_HI), run(777, Q_LO, Q_HI)
    print(f"\nдетермінованість (той самий seed двічі): "
          f"{'OK' if d1 == d2 else 'РОЗБІЖНІСТЬ'}")
    px = make_slice(12345)
    print(f"значення зрізу в межах [0, {LEVELS - 1}]: "
          f"{min(px) >= 0 and max(px) < LEVELS} (min {min(px)}, max {max(px)})")
    a = run(12345, Q_LO, Q_HI)
    print(f"частка вилучених {100 * a['dropped'] / a['pixels']:.2f} % "
          f"(очікувано ≈ {100 * (Q_LO + 1 - Q_HI):.0f} %)")

    print(f"\nрозбіжностей по p_lo / p_hi / вилучених / залишених: {hard}")
    print(f"розбіжностей лише в середньому і σ через правило округлення: {soft}")
    return 1 if hard else 0


if __name__ == "__main__":
    _sys.exit(main())
