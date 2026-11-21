"""Самоперевірка ЛР6: незалежний перерахунок усього, що пішло в таблиці.

Перевіряє не код проти самого себе, а числа проти формул умови і проти незалежних
реалізацій: кількість параметрів, межі нормування і вміст датасету, кількість кроків за
епоху, розбиття індексів семплером, рівність ḡ = g у подвійній точності, ACC і AUC проти
власної реалізації, X проти 50 · B / t, метрики масштабування проти формул, збіг втрат
прогону з відмовою і без неї, узгодженість контрольної точки.

    python -m scripts.lab6_selfcheck --out-dir results/lab6

Код виходу 0 — усі перевірки з вердиктом пройдені.
"""

import argparse
import glob
import inspect
import json
import math
import os
import re
import statistics

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from lab6 import train as T
from lab6.data import DATASETS, WIDTHS, load_split
from lab6.host import utf8_console
from lab6.model import Net, n_params_formula

CHECKS = []


def check(name, ok, detail):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'ok' if ok else 'ПОМИЛКА'}] {name}: {detail}", flush=True)
    return ok


def note(name, detail):
    CHECKS.append({"name": name, "ok": None, "detail": detail})
    print(f"[  –  ] {name}: {detail}", flush=True)


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def runs(out_dir, stem):
    return [load(p) for p in sorted(glob.glob(os.path.join(out_dir, f"{stem}_r*.json")))]


def indexed(out_dir, stem):
    """Прогони конфігурації за номером кола."""
    out = {}
    for p in sorted(glob.glob(os.path.join(out_dir, f"{stem}_r*.json"))):
        m = re.search(r"_r(\d+)\.json$", p)
        if m:
            out[int(m.group(1))] = load(p)
    return out


# ---------------------------------------------------------------- перевірки


def check_model():
    name, K, C = DATASETS[T.VARIANT["dataset"]]
    c1, c2, h = WIDTHS[T.VARIANT["width"]]
    torch.manual_seed(0)
    model = Net(C, c1, c2, h, K)
    n = sum(p.numel() for p in model.parameters())
    formula = n_params_formula(C, c1, c2, h, K)
    hand = 9 * 3 * 16 + 16 + 9 * 16 * 32 + 32 + 49 * 32 * 128 + 128 + 128 * 7 + 7
    check("n_params моделі = формула умови", n == formula == hand,
          f"модель {n}, формула {formula}, розрахунок для Ш1 і Д1 {hand}")
    check("тензорів параметрів 8", len(list(model.parameters())) == 8,
          f"{len(list(model.parameters()))} тензорів: вага і зсув кожного з 4 шарів")
    check("M = 4 · n_params", 4 * n == 827292,
          f"M = {4 * n} Б = {4 * n / 1024 ** 2:.3f} МіБ")
    check("немає шарів, залежних від складу батча",
          not any(isinstance(m, (nn.BatchNorm1d, nn.BatchNorm2d, nn.Dropout))
                  for m in model.modules()),
          "у мережі немає BatchNorm і Dropout, тому ḡ = g виконується точно в дійсних числах")
    with torch.no_grad():
        shape = model.features(torch.zeros(1, C, 28, 28)).shape
    check("карта ознак після двох підвибірок 7 × 7", tuple(shape[2:]) == (7, 7),
          f"форма {tuple(shape)} — {shape[1]} каналів 7 × 7")
    return model, name, K, C


def check_data(name, K):
    path = os.path.join("data", f"{name}.npz")
    d = np.load(path)
    train = load_split(path, "train", n=T.N_TRAIN)
    test = load_split(path, "test")
    check("N = 7000 перших прикладів train", len(train) == T.N_TRAIN == 7000,
          f"train {len(train)} з {len(d['train_images'])} у файлі")
    check("тестова частина — усе розбиття test", len(test) == len(d["test_images"]),
          f"{len(test)} прикладів")
    x0, y0 = train[0]
    check("форма прикладу (C, 28, 28)", tuple(x0.shape) == (3, 28, 28),
          f"{tuple(x0.shape)}, канали першою розмірністю")
    xs = torch.stack([train[i][0] for i in range(500)])
    check("нормування в [-1, 1]", float(xs.min()) >= -1.0 and float(xs.max()) <= 1.0,
          f"min {float(xs.min()):.3f}, max {float(xs.max()):.3f}")
    raw = d["train_images"][7].transpose(2, 0, 1).astype(np.float64)
    mine = (raw / 255 - 0.5) / 0.5
    got = train[7][0].numpy().astype(np.float64)
    check("перетворення пікселів x' = (x / 255 − 0,5) / 0,5",
          float(np.abs(mine - got).max()) < 1e-6,
          f"найбільша різниця з незалежним перерахунком {np.abs(mine - got).max():.2e}")
    ys = np.array([int(train[i][1]) for i in range(len(train))])
    check("мітки 0…K−1", ys.min() == 0 and ys.max() == K - 1,
          f"класи {sorted(set(ys.tolist()))}")
    return train, test


def check_steps_per_epoch(out_dir):
    for stem, p, b in (("s2_train_ddp", T.VARIANT["p_max"], 48),
                       ("s1_single_train", 1, 96),
                       ("s4_train_p1_b", 1, 48)):
        r = load(os.path.join(out_dir, f"{stem}.json"))
        if not r:
            continue
        n_e = math.ceil(T.N_TRAIN / r["P"]) // r["b"]
        check(f"n_e = ⌊⌈N / P⌉ / b⌋ для {stem}",
              n_e == r["steps_per_epoch"] == r["steps_total"] // r["epochs"],
              f"P = {r['P']}, b = {r['b']}: формула {n_e}, у прогоні "
              f"{r['steps_per_epoch']}, кроків усього {r['steps_total']} за "
              f"{r['epochs']} епохи")


def check_sampler(train):
    p = T.VARIANT["p_max"]
    parts = []
    for rank in range(p):
        s = DistributedSampler(train, num_replicas=p, rank=rank, shuffle=True, seed=0)
        s.set_epoch(0)
        parts.append(list(s))
    sizes = {len(x) for x in parts}
    check("частини семплера однакової довжини ⌈N / P⌉",
          sizes == {math.ceil(len(train) / p)},
          f"довжини {sorted(len(x) for x in parts)}, ⌈{len(train)} / {p}⌉ = "
          f"{math.ceil(len(train) / p)}")
    union = set().union(*[set(x) for x in parts])
    inter = set(parts[0]).intersection(*[set(x) for x in parts[1:]])
    check("частини не перетинаються і разом дають усі приклади",
          not inter and union == set(range(len(train))),
          f"перетин {len(inter)} індексів, обʼєднання {len(union)} з {len(train)}")
    # перестановка та сама, процес r бере позиції r, r + P, …
    perm = [None] * (len(parts[0]) * p)
    for rank, part in enumerate(parts):
        perm[rank::p] = part
    rebuilt = [perm[i::p] for i in range(p)]
    check("процес r бере позиції r, r + P, … тієї самої перестановки",
          rebuilt == parts, "частини складаються в одну перестановку без залишку")
    s = DistributedSampler(train, num_replicas=p, rank=0, shuffle=True, seed=0)
    s.set_epoch(0)
    a = list(s)
    s.set_epoch(1)
    b2 = list(s)
    check("set_epoch змінює перестановку", a != b2,
          "епохи 0 і 1 дають різні порядки; без set_epoch порядок був би однаковий")
    loader = DataLoader(train, batch_size=48, sampler=s, drop_last=True)
    check("кількість батчів у DataLoader = n_e",
          len(loader) == math.ceil(len(train) / p) // 48,
          f"{len(loader)} батчів по 48 при drop_last=True")


def check_grad_equivalence(model, train):
    """ḡ = g у подвійній точності; у float32 різниця — округлення суми."""
    p = T.VARIANT["p_max"]
    B = T.VARIANT["batch"]
    xb = torch.stack([train[i][0] for i in range(B)])
    yb = torch.stack([train[i][1] for i in range(B)])
    out = {}
    for dtype, tag in ((torch.float64, "float64"), (torch.float32, "float32")):
        m = Net(3, 16, 32, 128, 7).to(dtype)
        torch.manual_seed(0)
        m2 = Net(3, 16, 32, 128, 7)
        m.load_state_dict({k: v.to(dtype) for k, v in m2.state_dict().items()})
        loss_fn = nn.CrossEntropyLoss()
        m.zero_grad(set_to_none=False)
        full = loss_fn(m(xb.to(dtype)), yb)
        full.backward()
        g = [q.grad.detach().clone().double() for q in m.parameters()]
        shard_losses, shards = [], None
        for rank in range(p):
            idx = list(range(rank, B, p))
            m.zero_grad(set_to_none=False)
            li = loss_fn(m(xb[idx].to(dtype)), yb[idx])
            li.backward()
            shard_losses.append(float(li.detach().double()))
            gi = [q.grad.detach().clone().double() for q in m.parameters()]
            shards = gi if shards is None else [a + b for a, b in zip(shards, gi)]
        gbar = [s / p for s in shards]
        rel = max(float((a - b).abs().max()) / float(a.abs().max())
                  for a, b in zip(g, gbar) if float(a.abs().max()) > 0)
        out[tag] = rel
        loss_rel = abs(statistics.fmean(shard_losses) - float(full.detach().double()))
        if tag == "float64":
            check("середнє локальних втрат = втрата на батчі B (float64)",
                  loss_rel < 1e-12,
                  f"різниця {loss_rel:.2e} при втраті {float(full.detach()):.6f}")
    check("ḡ = g у подвійній точності", out["float64"] < 1e-12,
          f"max |g − ḡ| / max |g| = {out['float64']:.2e} у float64 проти "
          f"{out['float32']:.2e} у float32 — різниця float32 є округленням суми, "
          f"а не різними обчисленнями")
    check("різниця float32 у межах, названих умовою (10⁻⁶–10⁻⁵)",
          1e-7 < out["float32"] < 1e-4, f"{out['float32']:.2e}")


def auc_ovr_independent(y, prob):
    """Macro-OVR AUC через U-статистику Манна-Вітні, без scikit-learn."""
    K = prob.shape[1]
    aucs = []
    for k in range(K):
        pos = prob[y == k, k]
        neg = prob[y != k, k]
        if len(pos) == 0 or len(neg) == 0:
            continue
        order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
        ranks = np.empty(len(order), dtype=np.float64)
        s = np.concatenate([pos, neg])[order]
        ranks[order] = np.arange(1, len(order) + 1, dtype=np.float64)
        # середні ранги для однакових значень
        i = 0
        while i < len(s):
            j = i
            while j + 1 < len(s) and s[j + 1] == s[i]:
                j += 1
            if j > i:
                ranks[order[i:j + 1]] = (i + j + 2) / 2
            i = j + 1
        u = ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2
        aucs.append(u / (len(pos) * len(neg)))
    return float(np.mean(aucs)), len(aucs)


def check_metrics(train, test):
    """ACC і AUC функції прогонів проти власної реалізації на навченій моделі."""
    from sklearn.metrics import roc_auc_score

    torch.manual_seed(0)
    model = Net(3, 16, 32, 128, 7)
    opt = torch.optim.SGD(model.parameters(), lr=T.ETA0, momentum=0.9)
    loss_fn = nn.CrossEntropyLoss()
    g = torch.Generator()
    g.manual_seed(0)
    loader = DataLoader(train, batch_size=96, shuffle=True, drop_last=True, generator=g)
    model.train()
    for i, (xb, yb) in enumerate(loader):
        opt.zero_grad(set_to_none=False)
        loss_fn(model(xb), yb).backward()
        opt.step()
        if i >= 29:
            break
    acc, auc = T.evaluate(model, test)
    model.eval()
    probs, ys = [], []
    with torch.no_grad():
        for xb, yb in DataLoader(test, batch_size=512):
            probs.append(torch.softmax(model(xb), dim=1).numpy())
            ys.append(yb.numpy())
    p = np.concatenate(probs)
    y = np.concatenate(ys)
    my_acc = float(sum(1 for i in range(len(y)) if int(p[i].argmax()) == int(y[i])) / len(y))
    my_auc, k_used = auc_ovr_independent(y, p)
    sk = float(roc_auc_score(y, p, multi_class="ovr", average="macro"))
    check("ACC прогонів = частка правильних, порахована окремо",
          abs(acc - my_acc) < 1e-12, f"{acc:.6f} проти {my_acc:.6f}")
    check("AUC прогонів = macro-OVR через U-статистику",
          abs(auc - my_auc) < 1e-9 and abs(auc - sk) < 1e-12,
          f"{auc:.6f}, незалежно {my_auc:.6f} за {k_used} класами, sklearn {sk:.6f}")
    check("сума ймовірностей класів = 1",
          float(np.abs(p.sum(axis=1) - 1).max()) < 1e-5,
          f"найбільше відхилення {np.abs(p.sum(axis=1) - 1).max():.2e}")


def check_throughput(out_dir):
    stems = ["s1_single_speed", "s2_speed_manual", "s2_speed_ddp", "s2_speed_ddp_nosync",
             "s3_speed_ddp_p1", "s3_speed_ddp_nosync_p1", "s4_speed_weak_p1"]
    bad = []
    n = 0
    for stem in stems:
        for r in runs(out_dir, stem):
            if not r:
                continue
            n += 1
            x = T.MEASURE_STEPS * r["B"] / r["t_block_s"]
            if abs(x - r["throughput"]) > 1e-6 * x:
                bad.append((stem, x, r["throughput"]))
            # t_block є сумою часу 50 кроків, тому дорівнює 50 · середньому
            if abs(r["t_block_s"] * 1e3 - T.MEASURE_STEPS * r["t_step_mean_ms"]) > 1e-6:
                bad.append((stem + " t_block", r["t_block_s"], r["t_step_mean_ms"]))
            if not (r["t_step_min_ms"] <= r["t_step_ms"] <= r["t_step_max_ms"]):
                bad.append((stem + " медіана поза межами", r["t_step_ms"], None))
    check("X = 50 · B / t і t_block = сума часу 50 кроків у всіх прогонах", not bad,
          f"перевірено {n} прогонів" if not bad else f"розбіжності: {bad[:3]}")


def check_scaling(out_dir):
    s = load(os.path.join(out_dir, "summary.json"))
    if not s:
        return
    bad = []
    x1 = s["strong"]["1"]["X"]
    for p_str, row in s["strong"].items():
        p = int(p_str)
        S = row["X"] / x1
        if abs(S - row["S"]) > 1e-9:
            bad.append(("S", p))
        if abs(S / p - row["E"]) > 1e-9:
            bad.append(("E", p))
        if p > 1:
            e = (1 / S - 1 / p) / (1 - 1 / p)
            if abs(e - row["e"]) > 1e-9:
                bad.append(("e", p))
        # c рахується з медіани різниці t_step по колах; перераховуємо з файлів прогонів
        stem = "s2_speed_ddp" if p == T.VARIANT["p_max"] else f"s3_speed_ddp_p{p}"
        ns_stem = ("s2_speed_ddp_nosync" if p == T.VARIANT["p_max"]
                   else f"s3_speed_ddp_nosync_p{p}")
        a = {i: r["t_step_ms"] for i, r in indexed(out_dir, stem).items()}
        b = {i: r["t_step_ms"] for i, r in indexed(out_dir, ns_stem).items()}
        diffs = [a[i] - b[i] for i in sorted(set(a) & set(b))]
        if diffs:
            c = statistics.median(diffs) / row["t_step_ms"]
            if abs(c - row["c"]) > 1e-9:
                bad.append(("c", p, c, row["c"]))
    xw1 = s["weak"]["1"]["X"]
    for p_str, row in s["weak"].items():
        p = int(p_str)
        if abs(row["X"] / xw1 - row["S_w"]) > 1e-9 or abs(row["X"] / xw1 / p - row["E_w"]) > 1e-9:
            bad.append(("S_w/E_w", p))
    check("S, E, e, c, S_w, E_w збігаються з формулами умови", not bad,
          "перерахунок із X і t_step дає ті самі числа" if not bad else str(bad))


def measure_division_cost(out_dir):
    """Скільки в t_sync займає ділення градієнтів на P між викликами all-reduce.

    Умова визначає t_sync як час від першого виклику all-reduce до повернення з
    останнього, а в циклі ручної синхронізації між викликами стоїть ще `p.grad /= P`.
    Замір показує, яку частку t_sync це ділення додає.
    """
    import time

    torch.manual_seed(0)
    model = Net(3, 16, 32, 128, 7)
    for p in model.parameters():
        p.grad = torch.randn_like(p)
    times = []
    for _ in range(200):
        t0 = time.perf_counter()
        for p in model.parameters():
            p.grad /= T.VARIANT["p_max"]
        times.append((time.perf_counter() - t0) * 1e3)
    div = statistics.median(times)
    s = load(os.path.join(out_dir, "summary.json")) or {}
    t_sync = (s.get("speed", {}).get("manual", {}) or {}).get("t_sync_ms")
    share = f", тобто {div / t_sync * 100:.1f} % від t_sync = {t_sync:.2f} мс" if t_sync else ""
    note("ділення градієнтів на P усередині t_sync",
         f"медіана 200 замірів {div:.3f} мс{share}")


def check_spec_compliance(out_dir):
    """Параметри кожного знятого прогону проти того, що вимагає умова варіанта."""
    v = T.VARIANT
    b_proc = v["batch"] // v["p_max"]

    # 1. параметри варіанта в коді
    check("параметри варіанта 19 задані за таблицею умови",
          (v["dataset"], v["width"], v["p_max"], v["batch"], v["ckpt_interval"],
           v["fail_step"]) == ("Д1", "Ш1", 2, 96, 15, 37),
          f"Д1, Ш1, P_max = {v['p_max']}, B = {v['batch']}, K_c = "
          f"{v['ckpt_interval']}, s_f = {v['fail_step']}")
    check("спільні для всіх варіантів параметри навчання за умовою",
          (T.ETA0, T.MOMENTUM, T.N_TRAIN, T.N_EPOCHS, T.SEED,
           T.WARMUP_STEPS, T.MEASURE_STEPS) == (0.02, 0.9, 7000, 3, 0, 10, 50),
          "η₀ = 0,02, момент 0,9, N = 7000, n_ep = 3, зерно 0, 10 кроків прогріву, "
          "50 вимірюваних")

    # 2. n_th у кожному прогоні
    all_runs = []
    for path in sorted(glob.glob(os.path.join(out_dir, "s*.json"))):
        name = os.path.basename(path)
        if name == "summary.json":  # зведення, а не прогін
            continue
        r = load(path)
        # у прогонів equiv і grad-single поля n_th немає, але воно є в описі стенду
        if r and ("n_th" in r or "torch_num_threads" in r.get("host", {})):
            all_runs.append((name, r))
    for extra in ("s5_failure/result.json", "s5_nofail/result.json"):
        r = load(os.path.join(out_dir, extra))
        if r:
            all_runs.append((extra, r))
    phys = T.physical_cores()
    want_th = max(1, phys // v["p_max"]) if phys else 1
    def threads_of(r):
        return r.get("n_th", r.get("host", {}).get("torch_num_threads"))

    bad = [n for n, r in all_runs if threads_of(r) != want_th]
    check("n_th = ⌊C_phys / P_max⌋ в усіх прогонах, включно з прогонами в одному процесі",
          not bad and want_th == 2,
          f"n_th = {want_th} (⌊{phys} / {v['p_max']}⌋) у всіх {len(all_runs)} прогонах"
          if not bad else f"інше значення у {bad}")
    bad = [n for n, r in all_runs if r.get("host", {}).get("omp_num_threads") != str(want_th)]
    check("OMP_NUM_THREADS дорівнює n_th в усіх прогонах", not bad,
          f"OMP_NUM_THREADS = {want_th}" if not bad else f"інше значення у {bad}")

    # 3. режими прогонів, які називає умова
    expect = {
        "s1_single_train.json": ("none", 1, 96, 96, 0.02, 3),
        "s2_train_ddp.json": ("ddp", 2, 48, 96, 0.02, 3),
        "s4_train_p1_b.json": ("ddp", 1, 48, 48, 0.02, 3),
        "s4_train_lin.json": ("ddp", 2, 48, 96, 0.04, 3),
        "s5_failure/result.json": ("ddp", 2, 48, 96, 0.02, 3),
    }
    bad = []
    for name, (sync, P, b, B, lr, ep) in expect.items():
        r = load(os.path.join(out_dir, name))
        if not r:
            bad.append((name, "немає файлу"))
            continue
        got = (r["sync"], r["P"], r["b"], r["B"], r["lr"], r["epochs"])
        if got != (sync, P, b, B, lr, ep):
            bad.append((name, got, (sync, P, b, B, lr, ep)))
    check("режими навчання збігаються з кроками 1, 2, 4 і 5 умови", not bad,
          "крок 1 — один процес без torch.distributed, B = 96, η₀; крок 2 — DDP на 2 "
          "процесах, b = 48; крок 4 — P = 1 з батчем b і η₀ та P_max з η_P = 0,04; "
          "крок 5 — DDP на 2 процесах з η₀" if not bad else str(bad))

    r = load(os.path.join(out_dir, "s1_single_train.json"))
    check("крок 1 виконано без torch.distributed",
          r and r["sync"] == "none" and r["P"] == 1,
          "прогін запущений прямо, не під torchrun, група процесів не створювалась")
    r = load(os.path.join(out_dir, "s3_speed_ddp_p1_r1.json"))
    check("рядок P = 1 таблиці 3 знято через torchrun з DDP",
          r and r["sync"] == "ddp" and r["P"] == 1 and r["B"] == v["batch"],
          f"P = 1, sync = ddp, B = {v['batch']}, як вимагає крок 3")
    r = load(os.path.join(out_dir, "s4_speed_weak_p1_r1.json"))
    check("рядок P = 1 таблиці 4 знято з батчем процесу b",
          r and r["b"] == b_proc and r["B"] == b_proc,
          f"b = {b_proc}, B_P = {b_proc}")

    # 4. еквівалентність градієнта і суми ваг
    e = load(os.path.join(out_dir, "s2_equiv.json"))
    check("перевірку еквівалентності зроблено на батчі з B прикладів і частках по b",
          e and e["B"] == v["batch"] and e["P"] == v["p_max"]
          and e["shard_sizes"] == b_proc and e["steps_before_sum"] == T.MEASURE_STEPS,
          f"B = {e['B']}, частка процесу {e['shard_sizes']} прикладів, суми ваг після "
          f"{e['steps_before_sum']} кроків" if e else "немає файлу")

    # 5. DDP з типовим розміром кошика
    src = inspect.getsource(T.wrap)
    check("DDP створюється з типовим розміром кошика",
          "DistributedDataParallel(model)" in src and "bucket_cap_mb" not in src,
          "bucket_cap_mb не задається, діє типове значення 25 МіБ (перший кошик 1 МіБ)")

    # 6. команда прогону з відмовою
    r = load(os.path.join(out_dir, "s5_failure", "result.json"))
    cmd = (r or {}).get("cmd", "")
    need = ["--max-restarts=1", "--nproc_per_node=2", "127.0.0.1:29500",
            "--ckpt-interval 15", "--fail-step 37"]
    missing = [x for x in need if x not in cmd]
    check("прогін з відмовою запущений з --max-restarts=1, статичною адресою, K_c і s_f",
          not missing,
          "у команді є " + ", ".join(need) if not missing else f"немає {missing}")
    check("відмову імітує процес rank P_max − 1 викликом os._exit(1) без контрольної точки",
          "os._exit(1)" in inspect.getsource(T.task_train)
          and "rank == world - 1" in inspect.getsource(T.task_train),
          "процес rank 1 створює файл-позначку і завершується на початку кроку s_f; "
          "контрольні точки пише лише процес 0")

    # 7. методика вимірів швидкості
    speed = [(n, r) for n, r in all_runs if r.get("task") == "speed"]
    bad = [n for n, r in speed
           if (r["warmup_steps"], r["measure_steps"]) != (T.WARMUP_STEPS, T.MEASURE_STEPS)]
    check("у кожному прогоні швидкості 10 кроків прогріву і 50 вимірюваних", not bad,
          f"перевірено {len(speed)} прогонів" if not bad else str(bad))
    check("t_step — медіана часу 50 кроків процесу 0, таймер time.perf_counter",
          "statistics.median(times)" in inspect.getsource(T.task_speed)
          and "time.perf_counter()" in inspect.getsource(T.task_speed),
          "медіана береться в процесі 0, який і пише файл прогону")

    # 8. умови прогонів
    conds = [(n, r.get("host", {}).get("conditions_before", {})) for n, r in all_runs]
    bad = [n for n, c in conds if c and (c.get("ac_power") is False or c.get("battery_saver"))]
    check("усі прогони зняті від мережі з вимкненою економією заряду", not bad,
          f"перевірено {len(conds)} прогонів" if not bad else str(bad))


def check_failure(out_dir):
    from scripts.lab6_tables import failure_summary, losses_by_step

    fail = load(os.path.join(out_dir, "s5_failure", "result.json"))
    ok = load(os.path.join(out_dir, "s2_train_ddp.json"))
    ctrl = load(os.path.join(out_dir, "s5_nofail", "result.json"))
    fs = failure_summary(os.path.join(out_dir, "s5_failure"))
    if not (fail and ok and fs):
        return
    lf, lo = losses_by_step(fail), losses_by_step(ok)
    common = sorted(set(lf) & set(lo))
    diff = [s for s in common if lf[s] != lo[s]]
    check("втрата на кожному кроці після відновлення збігається з прогоном без відмови",
          not diff,
          f"{len(common)} спільних кроків, розбіжностей {len(diff)}"
          + (f", перші {diff[:5]}" if diff else ""))
    check("ACC і AUC після n_ep епох однакові з прогоном без відмови",
          fail.get("acc") == ok.get("acc") and fail.get("auc") == ok.get("auc"),
          f"ACC {fail.get('acc')!r} проти {ok.get('acc')!r}, "
          f"AUC {fail.get('auc')!r} проти {ok.get('auc')!r}")
    if ctrl:
        lc = losses_by_step(ctrl)
        d2 = [s for s in sorted(set(lc) & set(lo)) if lc[s] != lo[s]]
        check("контрольні точки не впливають на навчання", not d2,
              f"прогін з точками кожні {T.VARIANT['ckpt_interval']} кроків дає ті самі "
              f"втрати на {len(set(lc) & set(lo))} кроках")
    redone = fs["steps_done_before_fail"] - fs["ckpt_step"]
    check("кроків виконано повторно = s_f − 1 − крок контрольної точки",
          redone == fs["steps_redone"] == T.VARIANT["fail_step"] - 1 - fs["ckpt_step"],
          f"до відмови виконано {fs['steps_done_before_fail']} кроків, остання точка на "
          f"кроці {fs['ckpt_step']}, повторно {redone} ≤ K_c = "
          f"{T.VARIANT['ckpt_interval']}")
    check("остання контрольна точка кратна K_c і лежить перед s_f",
          fs["ckpt_step"] % T.VARIANT["ckpt_interval"] == 0
          and fs["ckpt_step"] < T.VARIANT["fail_step"],
          f"крок {fs['ckpt_step']}, K_c = {T.VARIANT['ckpt_interval']}, s_f = "
          f"{T.VARIANT['fail_step']}")
    check("t_lost = (t_f − t_c) + t_r",
          abs(fs["t_lost"] - (fs["t_f_minus_t_c"] + fs["t_r"])) < 1e-9,
          f"t_lost = {fs['t_lost']:.2f} с = {fs['t_f_minus_t_c']:.2f} + {fs['t_r']:.2f}")
    src = inspect.getsource(T.save_ckpt)
    check("контрольна точка пишеться в тимчасовий файл і перейменовується атомарно",
          "ckpt.tmp" in src and "os.replace" in src,
          "torch.save у ckpt.tmp, далі os.replace на ckpt.pt")
    state = torch.load(os.path.join(out_dir, "s5_failure", "ckpt.pt"), weights_only=False)
    check("у контрольній точці є модель, оптимізатор, епоха і крок",
          {"model", "opt", "epoch", "step_in_epoch", "step"} <= set(state),
          f"ключі {sorted(state)}, останній крок {state['step']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="results/lab6")
    a = ap.parse_args(argv)
    utf8_console()
    torch.set_num_threads(T.n_threads_default())

    model, name, K, C = check_model()
    train, test = check_data(name, K)
    check_steps_per_epoch(a.out_dir)
    check_sampler(train)
    check_grad_equivalence(model, train)
    check_metrics(train, test)
    check_throughput(a.out_dir)
    check_scaling(a.out_dir)
    measure_division_cost(a.out_dir)
    check_spec_compliance(a.out_dir)
    check_failure(a.out_dir)

    verdicts = [c for c in CHECKS if c["ok"] is not None]
    failed = [c for c in verdicts if not c["ok"]]
    lines = [f"# Самоперевірка ЛР6 (варіант {T.VARIANT['number']})", "",
             f"Перевірок з вердиктом {len(verdicts)}, пройдено "
             f"{len(verdicts) - len(failed)}, не пройдено {len(failed)}.", "",
             "| Перевірка | Вердикт | Деталі |", "|---|---|---|"]
    for c in CHECKS:
        v = "—" if c["ok"] is None else ("пройдено" if c["ok"] else "**НЕ ПРОЙДЕНО**")
        lines.append(f"| {c['name']} | {v} | {c['detail']} |")
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "checks.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(a.out_dir, "checks.json"), "w", encoding="utf-8") as f:
        json.dump(CHECKS, f, ensure_ascii=False, indent=2)
    print(f"\nпройдено {len(verdicts) - len(failed)} з {len(verdicts)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
