"""Дослід: чи в обміні причина того, що P_max процесів не прискорюють навчання.

Таблиці 3 і 4 дають S(2) < 1 і S_w(2) < 1 при частці обміну c ≈ 0,01. Якщо обмін тут не
винен, то два процеси сповільнює сама машина: процесор ноутбука з 4 фізичними ядрами при
4 активних потоках тримає нижчу частоту, ніж при 2.

Дослід це перевіряє без torch.distributed взагалі: ті самі 10 кроків прогріву і 50
вимірюваних кроків виконуються спершу в одному процесі, а потім у двох процесах, що
стартують одночасно і нічого один одному не передають. Якщо час кроку в парі зростає
так само, як у DDP на двох процесах, причина не в обміні.

    python -m scripts.lab6_contention --out results/lab6/contention.json
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

from lab5.bench import run_conditions
from lab6.host import utf8_console
from lab6.train import VARIANT

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
B_PROC = VARIANT["batch"] // VARIANT["p_max"]


def env(threads):
    e = dict(os.environ)
    e["OMP_NUM_THREADS"] = str(threads)
    e["USE_LIBUV"] = "0"
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
    return e


def launch(tmp_dir, tag, batch, threads):
    out = os.path.join(tmp_dir, f"{tag}.json")
    cmd = [PY, "-m", "lab6.train", "--task", "speed", "--sync", "none",
           "--global-batch", str(batch), "--threads", str(threads),
           "--tag", tag, "--out", out]
    return subprocess.Popen(cmd, cwd=ROOT, env=env(threads),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE), out


def group(tmp_dir, n, batch, threads, rnd):
    """n незалежних процесів одночасно; повертає t_step кожного."""
    procs = [launch(tmp_dir, f"n{n}_r{rnd}_p{i}", batch, threads) for i in range(n)]
    res = []
    for p, out in procs:
        _, err = p.communicate(timeout=900)
        if p.returncode != 0:
            raise SystemExit(f"прогін впав: {err.decode('utf-8', 'replace')[-500:]}")
        with open(out, encoding="utf-8") as f:
            res.append(json.load(f))
    return res


def thread_sweep(tmp_dir, batch, rounds, threads_list):
    """Один процес на n_th = 1, 2, 4: скільки обчислень машина віддає від потоків."""
    out = {}
    for rnd in range(1, rounds + 1):
        for n_th in threads_list:
            p, path = launch(tmp_dir, f"th{n_th}_r{rnd}", batch, n_th)
            _, err = p.communicate(timeout=900)
            if p.returncode != 0:
                raise SystemExit(f"прогін впав: {err.decode('utf-8', 'replace')[-500:]}")
            with open(path, encoding="utf-8") as f:
                t = json.load(f)["t_step_ms"]
            out.setdefault(str(n_th), []).append(t)
            print(f"коло {rnd}: один процес, батч {batch}, n_th = {n_th} — "
                  f"t_step {t:.2f} мс", flush=True)
    return {k: {"t_step_ms": v, "t_step_median_ms": statistics.median(v)}
            for k, v in out.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="results/lab6/contention.json")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--threads", type=int, default=2, help="n_th кожного процесу")
    ap.add_argument("--no-thread-sweep", action="store_true")
    a = ap.parse_args(argv)
    utf8_console()

    tmp_dir = os.path.join(os.path.dirname(a.out) or ".", "contention_runs")
    os.makedirs(tmp_dir, exist_ok=True)
    out = {"batch_per_process": B_PROC, "threads_per_process": a.threads,
           "rounds": a.rounds, "conditions": run_conditions(), "groups": {}}

    for rnd in range(1, a.rounds + 1):
        for n in (1, 2):
            t0 = time.perf_counter()
            res = group(tmp_dir, n, B_PROC, a.threads, rnd)
            ts = [r["t_step_ms"] for r in res]
            out["groups"].setdefault(str(n), []).append(
                {"round": rnd, "t_step_ms": ts, "wall_s": time.perf_counter() - t0})
            print(f"коло {rnd}: {n} процес(и) по батчу {B_PROC} і {a.threads} потоках — "
                  f"t_step {', '.join(f'{t:.2f}' for t in ts)} мс", flush=True)

    for n, rows in out["groups"].items():
        vals = [t for row in rows for t in row["t_step_ms"]]
        out["groups"][n] = {"runs": rows, "t_step_median_ms": statistics.median(vals),
                            "t_step_all_ms": vals}
    one = out["groups"]["1"]["t_step_median_ms"]
    two = out["groups"]["2"]["t_step_median_ms"]
    out["slowdown_two_vs_one"] = two / one
    print(f"\nмедіана t_step: один процес {one:.2f} мс, кожен із двох {two:.2f} мс, "
          f"сповільнення {two / one:.2f} раза")

    if not a.no_thread_sweep:
        print("\nрозгортка за потоками (один процес):", flush=True)
        out["thread_sweep"] = thread_sweep(tmp_dir, B_PROC, a.rounds, [1, 2, 4])
        s = out["thread_sweep"]
        if "1" in s and "4" in s:
            print(f"\nмедіана t_step одного процесу: n_th = 1 — "
                  f"{s['1']['t_step_median_ms']:.2f} мс, n_th = 2 — "
                  f"{s['2']['t_step_median_ms']:.2f} мс, n_th = 4 — "
                  f"{s['4']['t_step_median_ms']:.2f} мс")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    # у підсумку є всі t_step кожного прогону, тож проміжні файли не потрібні
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"записано {a.out}")


if __name__ == "__main__":
    main()
