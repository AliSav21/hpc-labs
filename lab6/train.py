"""Навчання мережі MedMNIST: один процес, ручна синхронізація градієнтів і DDP.

Запускається або напряму (`python -m lab6.train --sync none ...`), або під torchrun
(`torchrun --standalone --nproc_per_node=2 -m lab6.train --sync ddp ...`). Готові обгортки
навчання не використовуються: цикл, синхронізація, контрольні точки і відновлення — свої.

Завдання (`--task`):
  stand       параметри стенду і моделі для таблиці 1;
  speed       10 кроків прогріву і 50 вимірюваних кроків: t_step, t_sync, X;
  grad-single градієнт g на першому батчі з B прикладів в одному процесі (у файл);
  equiv       ḡ ручною синхронізацією на P процесах, порівняння з g і суми ваг після
              50 кроків;
  train       навчання n_ep епох: втрата на кожному кроці, ACC і AUC, контрольні точки,
              імітація відмови і відновлення.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

import numpy as np
import torch
from sklearn.metrics import roc_auc_score
from torch import nn
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
import torch.distributed as dist

from lab6.data import DATASETS, WIDTHS, load_split
from lab6.host import host_info, physical_cores, run_conditions, utf8_console
from lab6.model import Net, n_params_formula
from lab6.winuv import patch_worker

# ---------------------------------------------------------------- варіант 19

VARIANT = {
    "number": 19,
    "dataset": "Д1",      # DermaMNIST
    "width": "Ш1",        # c1 = 16, c2 = 32, h = 128
    "p_max": 2,
    "batch": 96,          # глобальний батч B
    "ckpt_interval": 15,  # K_c
    "fail_step": 37,      # s_f
}

ETA0 = 0.02        # η₀
MOMENTUM = 0.9
N_TRAIN = 7000     # N, перші N прикладів розбиття train
N_EPOCHS = 3       # n_ep
WARMUP_STEPS = 10
MEASURE_STEPS = 50
SEED = 0


def n_threads_default():
    """n_th = ⌊C_phys / P_max⌋; якщо ядер менше за P_max, за умовою n_th = 1."""
    phys = physical_cores()
    if not phys:
        return 1
    return max(1, phys // VARIANT["p_max"])


# ---------------------------------------------------------------- група процесів


def world_rank():
    """(P, rank) із середовища torchrun; (1, 0) поза ним."""
    return int(os.environ.get("WORLD_SIZE", "1")), int(os.environ.get("RANK", "0"))


def restart_count():
    for key in ("TORCHELASTIC_RESTART_COUNT", "TORCHELASTIC_RUN_RESTART_COUNT"):
        if key in os.environ:
            try:
                return int(os.environ[key])
            except ValueError:
                pass
    return 0


def init_group():
    patch_worker()  # збірка PyTorch для Windows без libuv, див. lab6/winuv.py
    dist.init_process_group(backend="gloo")


# ---------------------------------------------------------------- модель і дані


def build_model(K, C, width):
    c1, c2, h = WIDTHS[width]
    torch.manual_seed(SEED)
    model = Net(C, c1, c2, h, K)
    n_params = sum(p.numel() for p in model.parameters())
    assert n_params == n_params_formula(C, c1, c2, h, K)
    return model, n_params


def datasets(name, with_test=True):
    path = os.path.join("data", f"{name}.npz")
    if not os.path.exists(path):
        raise SystemExit(
            f"немає {path}; завантажити: curl -L -o {path} "
            f"https://zenodo.org/records/10519652/files/{name}.npz?download=1")
    train = load_split(path, "train", n=N_TRAIN)
    test = load_split(path, "test") if with_test else None
    return train, test


def make_loader(train, b, world, rank, distributed):
    """DataLoader процесу і семплер (None поза групою)."""
    if distributed:
        sampler = DistributedSampler(train, num_replicas=world, rank=rank,
                                     shuffle=True, seed=SEED)
        loader = DataLoader(train, batch_size=b, sampler=sampler, drop_last=True,
                            num_workers=0)
        return loader, sampler
    g = torch.Generator()
    g.manual_seed(SEED)
    loader = DataLoader(train, batch_size=b, shuffle=True, drop_last=True,
                        num_workers=0, generator=g)
    return loader, None


def steps_per_epoch(n, world, b):
    """n_e = ⌊⌈N / P⌉ / b⌋."""
    return math.ceil(n / world) // b


def evaluate(model, test):
    """ACC і AUC на тестовій частині."""
    net = model.module if isinstance(model, DistributedDataParallel) else model
    was_training = net.training
    net.eval()
    probs, ys = [], []
    with torch.no_grad():
        for xb, yb in DataLoader(test, batch_size=512, num_workers=0):
            probs.append(torch.softmax(net(xb), dim=1).numpy())
            ys.append(yb.numpy())
    p = np.concatenate(probs)
    y = np.concatenate(ys)
    acc = float((p.argmax(axis=1) == y).mean())
    auc = float(roc_auc_score(y, p, multi_class="ovr", average="macro"))
    if was_training:
        net.train()
    return acc, auc


# ---------------------------------------------------------------- кроки навчання


def manual_sync(model, world):
    """Ручна синхронізація градієнтів; повертає час від першого all-reduce до останнього."""
    t0 = time.perf_counter()
    for p in model.parameters():
        dist.all_reduce(p.grad, op=dist.ReduceOp.SUM)
        p.grad /= world
    return time.perf_counter() - t0


def batch_step(model, opt, loss_fn, xb, yb, sync, world):
    """Один крок навчання. Повертає (локальна втрата, час синхронізації)."""
    opt.zero_grad(set_to_none=False)
    loss = loss_fn(model(xb), yb)
    t_sync = 0.0
    if sync == "ddp-nosync":
        with model.no_sync():
            loss.backward()
    else:
        loss.backward()
        if sync == "manual":
            t_sync = manual_sync(model, world)
    opt.step()
    return float(loss.detach()), t_sync


def wrap(model, sync):
    if sync in ("ddp", "ddp-nosync"):
        return DistributedDataParallel(model)
    return model


def broadcast_weights(model):
    for p in model.parameters():
        dist.broadcast(p.data, src=0)


# ---------------------------------------------------------------- завдання: speed


def task_speed(a, train, model, world, rank, b, B):
    """10 кроків прогріву, далі 50 вимірюваних кроків."""
    distributed = a.sync != "none"
    loader, sampler = make_loader(train, b, world, rank, distributed)
    if distributed and a.sync == "manual":
        broadcast_weights(model)
    model = wrap(model, a.sync)
    opt = torch.optim.SGD(model.parameters(), lr=a.lr, momentum=MOMENTUM)
    loss_fn = nn.CrossEntropyLoss()
    model.train()

    total = WARMUP_STEPS + MEASURE_STEPS
    n_e = steps_per_epoch(len(train), world, b)
    epoch, it = 0, None
    times, syncs, losses = [], [], []
    if distributed:
        dist.barrier()
    for s in range(total):
        if s == WARMUP_STEPS and distributed:
            dist.barrier()
        t0 = time.perf_counter()
        if it is None:
            if sampler is not None:
                sampler.set_epoch(epoch)
            it = iter(loader)
        try:
            xb, yb = next(it)
        except StopIteration:
            epoch += 1
            if sampler is not None:
                sampler.set_epoch(epoch)
            it = iter(loader)
            xb, yb = next(it)
        loss, t_sync = batch_step(model, opt, loss_fn, xb, yb, a.sync, world)
        t1 = time.perf_counter()
        if s >= WARMUP_STEPS:
            times.append(t1 - t0)
            syncs.append(t_sync)
            losses.append(loss)

    t_block = sum(times)
    out = {
        "task": "speed",
        "tag": a.tag,
        "sync": a.sync,
        "P": world,
        "b": b,
        "B": B,
        "lr": a.lr,
        "n_th": torch.get_num_threads(),
        "warmup_steps": WARMUP_STEPS,
        "measure_steps": MEASURE_STEPS,
        "steps_per_epoch": n_e,
        "t_step_ms": statistics.median(times) * 1e3,
        "t_step_mean_ms": statistics.fmean(times) * 1e3,
        "t_step_min_ms": min(times) * 1e3,
        "t_step_max_ms": max(times) * 1e3,
        "t_block_s": t_block,
        "throughput": MEASURE_STEPS * B / t_block,
        "t_sync_ms": statistics.median(syncs) * 1e3,
        "loss_last": losses[-1],
    }
    return out


# ---------------------------------------------------------------- завдання: градієнти


def first_batch(train, B):
    xs = torch.stack([train[i][0] for i in range(B)])
    ys = torch.stack([train[i][1] for i in range(B)])
    return xs, ys


def task_grad_single(a, train, model, B):
    """Градієнт g на першому батчі навчальної частини з B прикладів, в одному процесі."""
    xb, yb = first_batch(train, B)
    model.train()
    model.zero_grad(set_to_none=False)
    nn.CrossEntropyLoss()(model(xb), yb).backward()
    grads = [p.grad.detach().clone().double() for p in model.parameters()]
    torch.save(grads, a.grad_file)
    return {
        "task": "grad-single",
        "B": B,
        "grad_file": a.grad_file,
        "max_abs": max(float(g.abs().max()) for g in grads),
        "tensors": len(grads),
    }


def task_equiv(a, train, model, world, rank, b, B):
    """ḡ ручною синхронізацією на P процесах проти g одного процесу, і суми ваг."""
    broadcast_weights(model)
    xb, yb = first_batch(train, B)
    # процес r бере приклади з номерами r, r + P, …
    idx = list(range(rank, B, world))
    model.train()
    model.zero_grad(set_to_none=False)
    nn.CrossEntropyLoss()(model(xb[idx]), yb[idx]).backward()
    t_sync = manual_sync(model, world)
    gbar = [p.grad.detach().clone().double() for p in model.parameters()]

    out = {"task": "equiv", "P": world, "B": B, "b": b, "t_sync_first_ms": t_sync * 1e3,
           "shard_sizes": len(idx)}
    if rank == 0:
        g = torch.load(a.grad_file)
        rel, per_tensor = 0.0, []
        for i, (gi, bi) in enumerate(zip(g, gbar)):
            scale = float(gi.abs().max())
            r = float((gi - bi).abs().max()) / scale if scale else 0.0
            per_tensor.append({"tensor": i, "numel": gi.numel(),
                               "max_abs_g": scale,
                               "max_abs_diff": float((gi - bi).abs().max()),
                               "rel": r})
            rel = max(rel, r)
        out["max_rel_grad_diff"] = rel
        out["per_tensor"] = per_tensor

    # 50 кроків навчання з ручною синхронізацією, далі суми ваг у float64
    loader, sampler = make_loader(train, b, world, rank, True)
    opt = torch.optim.SGD(model.parameters(), lr=a.lr, momentum=MOMENTUM)
    loss_fn = nn.CrossEntropyLoss()
    sampler.set_epoch(0)
    done = 0
    for xb, yb in loader:
        batch_step(model, opt, loss_fn, xb, yb, "manual", world)
        done += 1
        if done == MEASURE_STEPS:
            break
    local = torch.tensor(
        [sum(float(p.data.double().sum()) for p in model.parameters())],
        dtype=torch.float64)
    gathered = [torch.zeros(1, dtype=torch.float64) for _ in range(world)]
    dist.all_gather(gathered, local)
    sums = [float(t[0]) for t in gathered]
    out["steps_before_sum"] = done
    out["weight_sums"] = sums
    out["weight_sum_spread"] = max(sums) - min(sums)
    return out


# ---------------------------------------------------------------- завдання: train


class EventLog:
    """Події прогону (крок, контрольна точка, відмова, старт) у файл, рядок на подію.

    Файл переживає аварійне завершення процесу, тому крок відмови і моменти контрольних
    точок лишаються записаними.
    """

    def __init__(self, path, rank, attempt):
        self.path = path
        self.rank = rank
        self.attempt = attempt
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def add(self, kind, **fields):
        rec = {"kind": kind, "rank": self.rank, "attempt": self.attempt,
               "t": time.time(), **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rec


def save_ckpt(model, opt, epoch, step_in_epoch, step, ckpt_dir):
    net = model.module if isinstance(model, DistributedDataParallel) else model
    state = {"model": net.state_dict(), "opt": opt.state_dict(), "epoch": epoch,
             "step_in_epoch": step_in_epoch, "step": step}
    tmp = os.path.join(ckpt_dir, "ckpt.tmp")
    torch.save(state, tmp)
    os.replace(tmp, os.path.join(ckpt_dir, "ckpt.pt"))


def task_train(a, train, test, model, world, rank, b, B):
    distributed = a.sync != "none"
    loader, sampler = make_loader(train, b, world, rank, distributed)
    if distributed and a.sync == "manual":
        broadcast_weights(model)
    model = wrap(model, a.sync)
    opt = torch.optim.SGD(model.parameters(), lr=a.lr, momentum=MOMENTUM)
    loss_fn = nn.CrossEntropyLoss()
    n_e = steps_per_epoch(len(train), world, b)

    attempt = restart_count()
    log = None
    if a.ckpt_dir:
        os.makedirs(a.ckpt_dir, exist_ok=True)
        log = EventLog(os.path.join(a.ckpt_dir, f"events-rank{rank}.jsonl"), rank, attempt)
        log.add("start", P=world, b=b, B=B, lr=a.lr, epochs=a.epochs,
                ckpt_interval=a.ckpt_interval, fail_step=a.fail_step, n_e=n_e)

    # відновлення з контрольної точки
    start_epoch, skip, step = 0, 0, 0
    ckpt_path = os.path.join(a.ckpt_dir, "ckpt.pt") if a.ckpt_dir else None
    if ckpt_path and os.path.exists(ckpt_path):
        state = torch.load(ckpt_path, weights_only=False)
        net = model.module if isinstance(model, DistributedDataParallel) else model
        net.load_state_dict(state["model"])
        opt.load_state_dict(state["opt"])
        start_epoch, skip, step = state["epoch"], state["step_in_epoch"], state["step"]
        if skip >= n_e:  # епоха була завершена рівно на контрольній точці
            start_epoch, skip = start_epoch + 1, 0
        if log:
            log.add("resume", step=step, epoch=start_epoch, skip=skip)

    marker = os.path.join(a.ckpt_dir, "failed.marker") if a.ckpt_dir else None
    model.train()
    history = []
    for epoch in range(start_epoch, a.epochs):
        if sampler is not None:
            sampler.set_epoch(epoch)
        to_skip = skip if epoch == start_epoch else 0
        for i, (xb, yb) in enumerate(loader):
            if i < to_skip:
                continue
            # імітація відмови: процес rank P − 1 завершується на початку кроку s_f
            if (a.fail_step and rank == world - 1 and step + 1 == a.fail_step
                    and marker and not os.path.exists(marker)):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"step": a.fail_step, "t": time.time()}))
                log.add("fail", step=a.fail_step, epoch=epoch, i=i)
                sys.stderr.write(f"rank {rank}: os._exit(1) на початку кроку {a.fail_step}\n")
                sys.stderr.flush()
                os._exit(1)
            loss_local, _ = batch_step(model, opt, loss_fn, xb, yb, a.sync, world)
            step += 1
            # втрата на глобальному батчі: частки однакового розміру, тому середнє
            # локальних втрат дорівнює втраті на всьому батчі B
            if distributed:
                t = torch.tensor([loss_local], dtype=torch.float64)
                dist.all_reduce(t, op=dist.ReduceOp.SUM)
                loss_global = float(t[0]) / world
            else:
                loss_global = loss_local
            history.append({"step": step, "epoch": epoch, "loss": loss_global,
                            "loss_local": loss_local, "attempt": attempt})
            if log:
                log.add("step", step=step, epoch=epoch, loss=loss_global,
                        loss_local=loss_local)
            if a.ckpt_interval and step % a.ckpt_interval == 0:
                if rank == 0:
                    save_ckpt(model, opt, epoch, i + 1, step, a.ckpt_dir)
                    if log:
                        log.add("ckpt", step=step, epoch=epoch, step_in_epoch=i + 1)
                if distributed:
                    dist.barrier()
        skip = 0

    out = {
        "task": "train", "tag": a.tag, "sync": a.sync, "P": world, "b": b, "B": B,
        "lr": a.lr, "epochs": a.epochs, "n_th": torch.get_num_threads(),
        "steps_per_epoch": n_e, "steps_total": step, "attempt": attempt,
        "ckpt_interval": a.ckpt_interval, "fail_step": a.fail_step,
        "loss_history": history,
        "loss_max_last_epoch": max(
            (h["loss"] for h in history if h["epoch"] == a.epochs - 1), default=None),
    }
    if rank == 0:
        acc, auc = evaluate(model, test)
        out["acc"], out["auc"] = acc, auc
        if log:
            log.add("eval", acc=acc, auc=auc, steps_total=step)
    return out


# ---------------------------------------------------------------- точка входу


def main(argv=None):
    name, K, C = DATASETS[VARIANT["dataset"]]
    c1, c2, h = WIDTHS[VARIANT["width"]]

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", default="speed",
                    choices=["stand", "speed", "grad-single", "equiv", "train"])
    ap.add_argument("--sync", default="ddp",
                    choices=["none", "manual", "ddp", "ddp-nosync"],
                    help="none — один процес без torch.distributed")
    ap.add_argument("--global-batch", type=int, default=None,
                    help="глобальний батч B; батч процесу b = B / P")
    ap.add_argument("--per-proc-batch", type=int, default=None,
                    help="батч процесу b; глобальний батч B = P · b (weak scaling)")
    ap.add_argument("--lr", type=float, default=ETA0)
    ap.add_argument("--epochs", type=int, default=N_EPOCHS)
    ap.add_argument("--threads", type=int, default=None, help="n_th, типово ⌊C_phys / P_max⌋")
    ap.add_argument("--ckpt-dir", default=None)
    ap.add_argument("--ckpt-interval", type=int, default=0, help="K_c, 0 — без точок")
    ap.add_argument("--fail-step", type=int, default=0, help="s_f, 0 — без відмови")
    ap.add_argument("--grad-file", default="results/lab6/grad_single.pt")
    ap.add_argument("--out", default=None)
    ap.add_argument("--tag", default=None)
    a = ap.parse_args(argv)
    utf8_console()

    n_th = a.threads if a.threads is not None else n_threads_default()
    torch.set_num_threads(n_th)

    distributed = a.sync != "none"
    world, rank = world_rank() if distributed else (1, 0)
    if not distributed and int(os.environ.get("WORLD_SIZE", "1")) > 1:
        raise SystemExit("--sync none не запускають під torchrun з кількома процесами")

    if a.global_batch and a.per_proc_batch:
        raise SystemExit("разом --global-batch і --per-proc-batch не задають")
    if a.per_proc_batch:
        b, B = a.per_proc_batch, a.per_proc_batch * world
    else:
        B = a.global_batch or VARIANT["batch"]
        if B % world:
            raise SystemExit(f"B = {B} не ділиться на P = {world}")
        b = B // world

    if a.task == "stand":
        model, n_params = build_model(K, C, VARIANT["width"])
        train, test = datasets(name)
        out = {
            "task": "stand", "variant": VARIANT, "dataset": name, "K": K, "C": C,
            "c1": c1, "c2": c2, "h": h, "n_params": n_params,
            "n_params_formula": n_params_formula(C, c1, c2, h, K),
            "M_bytes": 4 * n_params, "M_mib": 4 * n_params / 1024 ** 2,
            "param_tensors": sum(1 for _ in model.parameters()),
            "N_train": len(train), "N_test": len(test), "B": VARIANT["batch"],
            "eta0": ETA0, "momentum": MOMENTUM, "n_epochs": N_EPOCHS,
            "n_th": n_th, "host": host_info(run_conditions(), full=True),
        }
        emit(out, a.out, rank)
        return

    if distributed:
        init_group()

    train, test = datasets(name, with_test=(a.task == "train"))
    model, n_params = build_model(K, C, VARIANT["width"])

    if a.task == "speed":
        out = task_speed(a, train, model, world, rank, b, B)
    elif a.task == "grad-single":
        out = task_grad_single(a, train, model, B)
    elif a.task == "equiv":
        out = task_equiv(a, train, model, world, rank, b, B)
    else:
        out = task_train(a, train, test, model, world, rank, b, B)

    out["n_params"] = n_params
    out["rank"] = rank
    out["restart_count"] = restart_count()
    out["host"] = host_info(run_conditions() if rank == 0 else None)
    emit(out, a.out, rank)

    if distributed:
        dist.barrier()
        dist.destroy_process_group()


def emit(out, path, rank):
    if rank != 0:
        return
    utf8_console()
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    short = {k: v for k, v in out.items() if k not in ("loss_history", "per_tensor", "host")}
    sys.stdout.write(json.dumps(short, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
