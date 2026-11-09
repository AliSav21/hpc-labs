"""Генератор задач обробки (лабораторна робота 4, крок 2).
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from loadgen.step import default_base, sharpen_timer

TIMEOUT = 5.0
# скільки разів довелося повторити запит через закрите сервером keep-alive зʼєднання
RETRIES = {"n": 0}
CRLF = "\r\n"
CRLFB = b"\r\n"


async def read_response(reader):
    """Код статусу і тіло відповіді HTTP/1.1."""
    line = await reader.readuntil(CRLFB)
    status = int(line.split(b" ")[1])
    clen, chunked = 0, False
    while True:
        h = await reader.readuntil(CRLFB)
        if h == CRLFB:
            break
        k, _, v = h.partition(b":")
        k = k.lower()
        if k == b"content-length":
            clen = int(v.strip())
        elif k == b"transfer-encoding" and b"chunked" in v.lower():
            chunked = True
    body = b""
    if chunked:
        while True:
            size = int((await reader.readuntil(CRLFB)).split(b";")[0], 16)
            body += await reader.readexactly(size + 2)
            if size == 0:
                break
    elif clen:
        body = await reader.readexactly(clen)
    return status, body


class Client:
    """Невеликий набір keep-alive зʼєднань для POST."""

    def __init__(self, host, port, limit=16):
        self.host, self.port = host, port
        self.free = []
        self.limit = limit
        self.open = 0

    async def take(self, fresh_only=False):
        """Вільне зʼєднання і ознака того, що воно щойно відкрите.
        """
        if not fresh_only:
            while self.free:
                reader, writer = self.free.pop()
                if not writer.is_closing():
                    return reader, writer, False
                self.open -= 1
            while self.open >= self.limit:
                await asyncio.sleep(0.002)
                if self.free:
                    reader, writer = self.free.pop()
                    return reader, writer, False
        self.open += 1
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
        except BaseException:
            self.open -= 1
            raise
        return reader, writer, True

    def give(self, reader, writer):
        self.free.append((reader, writer))

    def drop(self, writer=None):
        if writer is not None:
            writer.close()
        self.open -= 1

    def close_all(self):
        for _, writer in self.free:
            writer.close()
        self.free.clear()


async def attempt_post(client, host, path, budget, fresh_only):
    """Одна спроба POST. Повертає (код, тіло, чи було зʼєднання свіжим)."""
    taken = None
    try:
        taken = await asyncio.wait_for(client.take(fresh_only), budget)
        reader, writer, fresh = taken
        writer.write(
            f"POST {path} HTTP/1.1{CRLF}Host: {host}{CRLF}Content-Length: 0{CRLF}{CRLF}".encode()
        )
        await writer.drain()
        status, body = await asyncio.wait_for(read_response(reader), budget)
    except Exception as e:
        if taken is not None:
            client.drop(taken[1])
            return 0, str(e).encode(), taken[2]
        return 0, str(e).encode(), True
    client.give(reader, writer)
    return status, body, fresh


async def post(client, host, path, budget):
    """POST без тіла з одним повтором на свіжому зʼєднанні.
    """
    t0 = time.perf_counter()
    status, body, fresh = await attempt_post(client, host, path, budget, False)
    if status == 0 and not fresh:
        left = budget - (time.perf_counter() - t0)
        if left > 0:
            RETRIES["n"] += 1
            status, body, _ = await attempt_post(client, host, path, left, True)
    return status, body


async def run_async(base, resource, ids, rate, count, poison, poison_at, out_ids):
    u = urlparse(base)
    host, port = u.hostname, u.port or 80
    client = Client(host, port)
    rng = random.Random(4919)
    per_sec = defaultdict(lambda: {"sent": 0, "ok": 0, "non2xx": 0})
    created = []
    codes = defaultdict(int)
    pending = set()
    poisoned = []
    t0 = time.perf_counter()

    def done_cb(task, sec, poison_flag):
        pending.discard(task)
        if task.cancelled():
            return
        status, body = task.result()
        codes[status] += 1
        row = per_sec[sec]
        if 200 <= status < 300:
            row["ok"] += 1
            try:
                tid = json.loads(body)["id"]
            except Exception:
                return
            (poisoned if poison_flag else created).append(tid)
        else:
            row["non2xx"] += 1

    i = 0
    poison_sent = 0
    while i < count:
        now = time.perf_counter() - t0
        sched = i / rate
        if sched > now:
            await asyncio.sleep(min(sched - now, 0.002))
            continue
        while i < count and i / rate <= now:
            sec = int(now)
            per_sec[sec]["sent"] += 1
            rid = rng.choice(ids)
            t = asyncio.create_task(post(client, host, f"{resource}/{rid}/tasks", TIMEOUT))
            pending.add(t)
            t.add_done_callback(lambda x, s=sec: done_cb(x, s, False))
            i += 1
        # отруєні задачі (Ф3) вставляються у вказану секунду прогону
        if poison and poison_sent < poison and now >= poison_at:
            for _ in range(poison):
                rid = rng.choice(ids)
                t = asyncio.create_task(
                    post(client, host, f"{resource}/{rid}/tasks/poison", TIMEOUT))
                pending.add(t)
                t.add_done_callback(lambda x, s=int(now): done_cb(x, s, True))
                poison_sent += 1
            print(f"  отруєні задачі надіслані на {now:.1f} с прогону", flush=True)

    if pending:
        await asyncio.wait(pending, timeout=TIMEOUT + 1)
    client.close_all()

    secs = sorted(per_sec)
    rows = [{"t_s": s, **per_sec[s]} for s in secs]
    ok_rates = [r["ok"] for r in rows[:-1]] or [r["ok"] for r in rows]
    summary = {
        "base": base, "resource": resource, "rate_target": rate, "count": count,
        "duration_s": round(time.perf_counter() - t0, 3),
        "created": len(created), "poisoned": len(poisoned),
        "non2xx": sum(r["non2xx"] for r in rows),
        "codes": {str(k): v for k, v in sorted(codes.items())},
        "stale_conn_retries": RETRIES["n"],
        "sent_rps_mean": statistics.fmean(r["sent"] for r in rows[:-1]) if len(rows) > 1 else None,
        "ok_rps_mean": statistics.fmean(ok_rates) if ok_rates else None,
        "rows": rows,
        "started_at_wall": None,
    }
    if out_ids:
        Path(out_ids).parent.mkdir(parents=True, exist_ok=True)
        Path(out_ids).write_text(json.dumps({"tasks": created, "poisoned": poisoned}),
                                 encoding="utf-8")
    return summary, created, poisoned


def run(base, resource, ids, rate, count, poison=0, poison_at=0.0, out=None, out_ids=None,
        label=None):
    sharpen_timer()
    start_wall = time.time()
    summary, created, poisoned = asyncio.run(
        run_async(base, resource, ids, rate, count, poison, poison_at, out_ids))
    summary["label"] = label
    summary["start_wall"] = start_wall
    summary["end_wall"] = time.time()
    summary["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start_wall))
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        # Умова кроку 2: генератор «записує ідентифікатори створених задач». Перелік
        # пишеться завжди поряд із підсумком, а не лише на явний ключ --out-ids:
        # у підсумку інакше лишалась сама кількість, і відтворити, які саме задачі
        # створив прогін, було б нічим.
        ids_path = out.with_name(out.name + "_task_ids").with_suffix(".json")
        ids_path.write_text(json.dumps({"tasks": created, "poisoned": poisoned}),
                            encoding="utf-8")
        summary["created_task_ids_file"] = ids_path.name
        out.with_suffix(".json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        with out.with_suffix(".csv").open("w", encoding="utf-8", newline="") as f:
            f.write("t_s,sent,ok,non2xx\n")
            for r in summary["rows"]:
                f.write(f"{r['t_s']},{r['sent']},{r['ok']},{r['non2xx']}\n")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default=default_base())
    ap.add_argument("--resource", default="/donor-registry")
    ap.add_argument("--ids", default=str(Path(__file__).with_name("ids.json")))
    ap.add_argument("--rate", type=float, required=True, help="інтенсивність створення, задач/с")
    ap.add_argument("--count", type=int, help="скільки задач створити")
    ap.add_argument("--duration", type=float, help="замість --count: тривалість прогону, с")
    ap.add_argument("--poison", type=int, default=0, help="скільки задач із seed = 0 (Ф3)")
    ap.add_argument("--poison-at", type=float, default=0.0,
                    help="на якій секунді прогону вставити отруєні задачі")
    ap.add_argument("--out", help="префікс файлів результату без розширення")
    ap.add_argument("--out-ids", help="куди скласти перелік ідентифікаторів створених задач")
    ap.add_argument("--label")
    a = ap.parse_args()

    if not a.count and not a.duration:
        ap.error("потрібно або --count, або --duration")
    count = a.count or int(round(a.rate * a.duration))
    ids = json.loads(Path(a.ids).read_text())
    print(f"створюю {count} задач з інтенсивністю {a.rate:g}/с для {len(ids)} записів",
          flush=True)
    s = run(a.base, a.resource, ids, a.rate, count, a.poison, a.poison_at,
            a.out, a.out_ids, a.label)
    print(json.dumps({k: v for k, v in s.items() if k != "rows"}, indent=2, ensure_ascii=False))
