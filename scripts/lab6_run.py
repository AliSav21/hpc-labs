"""Уся серія прогонів ЛР6 за один раз: кроки 1-5 умови і таблиці 1-6.

Кожен прогін — окремий процес (`python -m lab6.train`) або група процесів під обгорткою
torchrun ([scripts/lab6_torchrun.py](lab6_torchrun.py)). Перед кожним прогоном
оркестратор перевіряє умови на машині (живлення, економія заряду, фонове завантаження
процесора) і записує їх у файл прогону, як у ЛР5: час кроку тут — одиниці мілісекунд, і
без умов два прогони в різному живленні за самими файлами не відрізнити.

Запуск:

    python -m scripts.lab6_run --out-dir results/lab6
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from lab5.bench import run_conditions
from lab6.host import physical_cores, utf8_console
from lab6.train import ETA0, N_EPOCHS, VARIANT

PY = sys.executable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = {"require_ac": True, "max_bg": 10.0, "wait_s": 60.0}

P_MAX = VARIANT["p_max"]
B = VARIANT["batch"]
B_PROC = B // P_MAX          # b для weak scaling
K_C = VARIANT["ckpt_interval"]
S_F = VARIANT["fail_step"]


def n_th():
    phys = physical_cores()
    return max(1, phys // P_MAX) if phys else 1


def env(threads):
    e = dict(os.environ)
    e["OMP_NUM_THREADS"] = str(threads)
    e["USE_LIBUV"] = "0"          # збірка PyTorch для Windows без libuv
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
    return e


def wait_for_quiet():
    """Чекає, доки фонове завантаження процесора впаде до порога; зупиняє серію на батареї."""
    deadline = time.time() + GATE["wait_s"]
    last = None
    while True:
        c = run_conditions()
        if GATE["require_ac"] and (c.get("ac_power") is False or c.get("battery_saver")):
            raise SystemExit(
                "прогін не починається: ноутбук не від мережі або ввімкнена економія заряду; "
                "--allow-battery дозволяє серію на батареї (тоді числа з основною серією "
                "не порівнювати)")
        last = c
        bg = c.get("background_cpu_percent")
        if bg is None or bg <= GATE["max_bg"] or time.time() > deadline:
            return last
        print(f"  фон {bg} % > {GATE['max_bg']} %, чекаю…", flush=True)


def run(cmd, out_path, procs=1, log_path=None, timeout=1800):
    """Прогін окремим процесом; повертає прочитаний json результату."""
    threads = n_th()
    cond = wait_for_quiet()
    name = os.path.basename(out_path)
    print(f"[{time.strftime('%H:%M:%S')}] {name} (P = {procs}, n_th = {threads}, "
          f"фон {cond.get('background_cpu_percent')} %)", flush=True)
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=ROOT, env=env(threads), capture_output=True, timeout=timeout)
    dt = time.perf_counter() - t0
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    if log_path:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"$ {' '.join(cmd)}\n\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
    if r.returncode != 0:
        tail = "\n".join((err or out).splitlines()[-25:])
        raise SystemExit(f"прогін {name} завершився з кодом {r.returncode}:\n{tail}")
    with open(out_path, encoding="utf-8") as f:
        res = json.load(f)
    res.setdefault("host", {})["conditions_before"] = cond
    res["wall_s"] = dt
    res["cmd"] = " ".join(cmd)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    return res


def single(out_path, args, log_path=None):
    return run([PY, "-m", "lab6.train", *args, "--out", out_path], out_path,
               procs=1, log_path=log_path)


def torchrun(out_path, procs, args, extra=(), log_path=None, timeout=1800):
    cmd = [PY, "-m", "scripts.lab6_torchrun", "--standalone",
           f"--nproc_per_node={procs}", *extra, "-m", "lab6.train", *args,
           "--out", out_path]
    return run(cmd, out_path, procs=procs, log_path=log_path, timeout=timeout)


def speed_configs():
    """Усі конфігурації вимірювання швидкості: (файл, процесів, ключі lab6.train).

    Рядки таблиць 2, 3 і 4 беруться звідси; рядок P = P_max таблиць 3 і 4 — це та сама
    конфігурація `s2_speed_ddp`, тому вона знімається один раз.
    """
    cfgs = [
        ("s1_single_speed", 1,
         ["--sync", "none", "--global-batch", str(B), "--tag", "1 процес, батч B"]),
        ("s2_speed_manual", P_MAX,
         ["--sync", "manual", "--global-batch", str(B),
          "--tag", "P_max процесів, ручна синхронізація"]),
        ("s2_speed_ddp", P_MAX,
         ["--sync", "ddp", "--global-batch", str(B), "--tag", "P_max процесів, DDP"]),
        ("s2_speed_ddp_nosync", P_MAX,
         ["--sync", "ddp-nosync", "--global-batch", str(B),
          "--tag", "P_max процесів, DDP, no_sync"]),
    ]
    for p in range(1, P_MAX):  # рядки P < P_max таблиць 3 і 4
        cfgs += [
            (f"s3_speed_ddp_p{p}", p,
             ["--sync", "ddp", "--global-batch", str(B), "--tag", f"strong, P = {p}"]),
            (f"s3_speed_ddp_nosync_p{p}", p,
             ["--sync", "ddp-nosync", "--global-batch", str(B),
              "--tag", f"strong, no_sync, P = {p}"]),
            (f"s4_speed_weak_p{p}", p,
             ["--sync", "ddp", "--per-proc-batch", str(B_PROC), "--tag", f"weak, P = {p}"]),
        ]
    return cfgs


def speed_all(out_dir, repeats):
    """Прогони швидкості всіх конфігурацій по колу: прогін 1 кожної, потім прогін 2…

    Порядок по колу, а не серіями по конфігурації: на ноутбуці розкид часу кроку між
    прогонами доходить до 40 %, і за серіями поспіль повільний проміжок машини діставався
    цілій конфігурації, через що різниця t_step − t_step,ns (обмін DDP) виходила навіть
    відʼємною. По колу такий проміжок однаково зачіпає всі режими.
    """
    cfgs = speed_configs()
    print(f"\n== Вимірювання швидкості: {len(cfgs)} конфігурацій по {repeats} прогонів "
          f"по колу ==", flush=True)
    for i in range(1, repeats + 1):
        print(f"-- коло {i} з {repeats}", flush=True)
        for stem, procs, args in cfgs:
            path = os.path.join(out_dir, f"{stem}_r{i}.json")
            full = ["--task", "speed", *args]
            if procs == 1 and args[args.index("--sync") + 1] == "none":
                single(path, full)
            else:
                torchrun(path, procs, full)


# ---------------------------------------------------------------- кроки


def step1(out_dir):
    print("\n== Крок 1: стенд і навчання в одному процесі ==", flush=True)
    single(os.path.join(out_dir, "stand.json"), ["--task", "stand"])
    single(os.path.join(out_dir, "s1_single_train.json"),
           ["--task", "train", "--sync", "none", "--global-batch", str(B),
            "--lr", str(ETA0), "--epochs", str(N_EPOCHS), "--tag", "1 процес, батч B"])


def step2(out_dir):
    print("\n== Крок 2: ручна синхронізація і DDP ==", flush=True)
    grad_file = os.path.join(out_dir, "grad_single.pt")
    single(os.path.join(out_dir, "s2_grad_single.json"),
           ["--task", "grad-single", "--sync", "none", "--global-batch", str(B),
            "--grad-file", grad_file])
    torchrun(os.path.join(out_dir, "s2_equiv.json"), P_MAX,
             ["--task", "equiv", "--sync", "manual", "--global-batch", str(B),
              "--grad-file", grad_file])
    torchrun(os.path.join(out_dir, "s2_train_ddp.json"), P_MAX,
             ["--task", "train", "--sync", "ddp", "--global-batch", str(B),
              "--lr", str(ETA0), "--epochs", str(N_EPOCHS),
              "--tag", "P_max процесів, DDP, η₀"])


def step4(out_dir):
    print("\n== Крок 4: правило масштабування швидкості навчання ==", flush=True)
    torchrun(os.path.join(out_dir, "s4_train_p1_b.json"), 1,
             ["--task", "train", "--sync", "ddp", "--per-proc-batch", str(B_PROC),
              "--lr", str(ETA0), "--epochs", str(N_EPOCHS), "--tag", "P = 1, батч b, η₀"])
    torchrun(os.path.join(out_dir, "s4_train_lin.json"), P_MAX,
             ["--task", "train", "--sync", "ddp", "--global-batch", str(B),
              "--lr", str(ETA0 * P_MAX), "--epochs", str(N_EPOCHS),
              "--tag", "P_max процесів, η_P за лінійним правилом"])


def step5(out_dir):
    print("\n== Крок 5: контрольні точки, відмова і відновлення ==", flush=True)
    fail_dir = os.path.join(out_dir, "s5_failure")
    if os.path.isdir(fail_dir):
        shutil.rmtree(fail_dir)
    os.makedirs(fail_dir, exist_ok=True)
    out_path = os.path.join(fail_dir, "result.json")
    # статична адреса групи: перезапущені процеси знаходять один одного за 127.0.0.1:29500
    cmd = [PY, "-m", "scripts.lab6_torchrun", "--nnodes=1", f"--nproc_per_node={P_MAX}",
           "--max-restarts=1", "--rdzv-backend=c10d", "--rdzv-endpoint=127.0.0.1:29500",
           "--rdzv-id=lab6", "-m", "lab6.train",
           "--task", "train", "--sync", "ddp", "--global-batch", str(B),
           "--lr", str(ETA0), "--epochs", str(N_EPOCHS),
           "--ckpt-dir", fail_dir, "--ckpt-interval", str(K_C),
           "--fail-step", str(S_F), "--tag", "DDP з відмовою на кроці s_f",
           "--out", out_path]
    run(cmd, out_path, procs=P_MAX,
        log_path=os.path.join(fail_dir, "torchrun.log"), timeout=1800)

    # контрольний прогін без відмови з тими самими контрольними точками
    ok_dir = os.path.join(out_dir, "s5_nofail")
    if os.path.isdir(ok_dir):
        shutil.rmtree(ok_dir)
    os.makedirs(ok_dir, exist_ok=True)
    ok_path = os.path.join(ok_dir, "result.json")
    torchrun(ok_path, P_MAX,
             ["--task", "train", "--sync", "ddp", "--global-batch", str(B),
              "--lr", str(ETA0), "--epochs", str(N_EPOCHS),
              "--ckpt-dir", ok_dir, "--ckpt-interval", str(K_C),
              "--tag", "DDP без відмови, контрольні точки"],
             log_path=os.path.join(ok_dir, "torchrun.log"))


# ---------------------------------------------------------------- точка входу


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="results/lab6")
    ap.add_argument("--repeats", type=int, default=9,
                    help="прогонів на кожну конфігурацію вимірювання швидкості")
    ap.add_argument("--steps", default="1,2,speed,4,5",
                    help="що виконувати: 1 — стенд і навчання в одному процесі, "
                         "2 — еквівалентність градієнта і навчання з DDP, "
                         "speed — вимірювання швидкості всіх конфігурацій (таблиці 2-4), "
                         "4 — прогони правила масштабування η, 5 — відмова і відновлення")
    ap.add_argument("--allow-battery", action="store_true")
    ap.add_argument("--max-bg", type=float, default=10.0)
    ap.add_argument("--tables-only", action="store_true",
                    help="перебудувати таблиці з уже знятих файлів")
    a = ap.parse_args(argv)
    utf8_console()

    GATE["require_ac"] = not a.allow_battery
    GATE["max_bg"] = a.max_bg
    out_dir = a.out_dir
    os.makedirs(out_dir, exist_ok=True)
    want = {s.strip() for s in a.steps.split(",") if s.strip()}

    if not a.tables_only:
        print(f"ЛР6, варіант {VARIANT['number']}: датасет {VARIANT['dataset']}, ширина "
              f"{VARIANT['width']}, P_max = {P_MAX}, B = {B}, K_c = {K_C}, s_f = {S_F}, "
              f"n_th = {n_th()}, прогонів швидкості на конфігурацію {a.repeats}", flush=True)
        t0 = time.perf_counter()
        if "1" in want:
            step1(out_dir)
        if "2" in want:
            step2(out_dir)
        if "speed" in want or "3" in want:
            speed_all(out_dir, a.repeats)
        if "4" in want:
            step4(out_dir)
        if "5" in want:
            step5(out_dir)
        print(f"\nсерія зайняла {time.perf_counter() - t0:.0f} с", flush=True)

    from scripts.lab6_tables import build
    build(out_dir)


if __name__ == "__main__":
    main()
