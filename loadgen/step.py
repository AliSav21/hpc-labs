"""Ступінчастий відкритий контур: прогрів 10 с на 200 rps, далі сходинки 30 с від 200 rps
з приростом 200 rps. Посекундний запис і зупинка за правилами кроку 5 ЛР2.

Модель зʼєднань (крок 6 ЛР3): генератор тримає 8·R = 32 постійних зʼєднання і закриває
кожне після 100 запитів, відкриваючи замість нього нове. Тривалість сходинки 30 с,
середні значення сходинки беруться за останні 20 с.
Лише стандартна бібліотека.
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

def _env(name, default):
    """Профіль за умовою задачі; змінні середовища лишені для самоперевірки генератора."""
    return float(os.environ.get(name, default))


TIMEOUT = _env("STEP_TIMEOUT_S", 5.0)       # таймаут запиту, с; перевищення = відповідь не 2xx
WARMUP_S = _env("STEP_WARMUP_S", 10.0)      # прогрів
STEP_S = _env("STEP_DURATION_S", 20.0)      # тривалість сходинки (профіль ЛР2)
TRANSIENT_S = _env("STEP_TRANSIENT_S", 5.0)  # перехідна частина сходинки, не враховується
MEASURE_S = STEP_S - TRANSIENT_S            # вікно вимірювання сходинки, 15 с
RATE0 = _env("STEP_RATE0", 200.0)           # перша сходинка
RATE_STEP = _env("STEP_RATE_STEP", 200.0)   # приріст
# Прогрів на рівні першої сходинки. У ЛР2 перша сходинка дорівнювала 200 rps, тож поведінка
# не змінюється; у кластері сходинки дрібніші, і прогрів на 200 rps насичував одну репліку,
# а черга його таймаутів переливалась у першу сходинку і псувала її.
WARMUP_RATE = _env("STEP_WARMUP_RATE", RATE0)
MAX_STEPS = int(_env("STEP_MAX_STEPS", 40))
START_DELAY = 3.0
FLUSH_LAG = 0.3         # наскільки пізно закривати секунду, с
# Набір зʼєднань генератора. За замовчуванням профіль ЛР2: межа фактично не діє,
# зʼєднання не переробляються. Модель кроку 6 ЛР3 (8·R зʼєднань, по 100 запитів на кожне)
# вмикають явно через configure() або змінні середовища.
CONNS = int(_env("STEP_CONNS", 1500))
REQ_PER_CONN = int(_env("STEP_REQ_PER_CONN", 0))   # 0 — не закривати за кількістю запитів

def configure(step_s=None, transient_s=None, rate0=None, rate_step=None,
              warmup_s=None, warmup_rate=None, conns=None, req_per_conn=None):
    """Задає профіль прогону. Викликається оркестратором лабораторної роботи до run().

    Значення дублюються у змінні середовища: робочі процеси генератора на Windows
    породжуються заново (spawn) і читають їх під час повторного імпорту модуля.
    """
    global STEP_S, TRANSIENT_S, MEASURE_S, RATE0, RATE_STEP, WARMUP_S, WARMUP_RATE
    global CONNS, REQ_PER_CONN
    pairs = (("STEP_DURATION_S", step_s), ("STEP_TRANSIENT_S", transient_s),
             ("STEP_RATE0", rate0), ("STEP_RATE_STEP", rate_step),
             ("STEP_WARMUP_S", warmup_s), ("STEP_WARMUP_RATE", warmup_rate),
             ("STEP_CONNS", conns), ("STEP_REQ_PER_CONN", req_per_conn))
    for name, value in pairs:
        if value is not None:
            os.environ[name] = str(value)
    STEP_S = _env("STEP_DURATION_S", STEP_S)
    TRANSIENT_S = _env("STEP_TRANSIENT_S", TRANSIENT_S)
    MEASURE_S = STEP_S - TRANSIENT_S
    RATE0 = _env("STEP_RATE0", RATE0)
    RATE_STEP = _env("STEP_RATE_STEP", RATE_STEP)
    WARMUP_S = _env("STEP_WARMUP_S", WARMUP_S)
    WARMUP_RATE = _env("STEP_WARMUP_RATE", RATE0)
    CONNS = int(_env("STEP_CONNS", CONNS))
    REQ_PER_CONN = int(_env("STEP_REQ_PER_CONN", REQ_PER_CONN))


CRLF = "\r\n"
CRLFB = b"\r\n"

STATUS_TIMEOUT = -1     # запит не вклався в TIMEOUT
STATUS_CONNERR = 0      # помилка зʼєднання


# ---------------------------------------------------------------- клієнт

async def read_response(reader):
    """Читає відповідь HTTP/1.1 і повертає код статусу."""
    line = await reader.readuntil(CRLFB)
    status = int(line.split(b" ")[1])
    clen = 0
    chunked = False
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
    if chunked:
        while True:
            size = int((await reader.readuntil(CRLFB)).split(b";")[0], 16)
            await reader.readexactly(size + 2)
            if size == 0:
                break
    elif clen:
        await reader.readexactly(clen)
    return status


class Connections:
    """Фіксований набір keep-alive зʼєднань (крок 6 ЛР3).

    Одночасно відкритих зʼєднань не більше limit: поки всі зайняті, запит чекає вільного.
    Кожне зʼєднання закривається після max_requests запитів, і наступний запит відкриває
    замість нього нове. Саме через це нові поди взагалі отримують трафік: kube-proxy
    обирає под під час встановлення TCP-зʼєднання.
    """

    def __init__(self, host, port, limit=CONNS, max_requests=REQ_PER_CONN):
        self.host, self.port = host, port
        self.free = []            # [reader, writer, скільки запитів уже пройшло]
        self.limit = limit
        self.max_requests = max_requests
        self.open = 0
        self.opened_total = 0     # скільки зʼєднань відкрито за прогін
        self.recycled = 0         # скільки закрито після вичерпання ліміту запитів

    async def take(self, budget):
        """Вільне зʼєднання або None, якщо за budget секунд жодне не звільнилося.

        Термін відлічується тут, а не через asyncio.wait_for: скасування wait_for могло
        відкинути вже відкрите зʼєднання, лишивши лічильник open завищеним, і набір
        поступово ставав меншим за limit.
        """
        deadline = time.perf_counter() + budget
        while True:
            while self.free:
                reader, writer, used = self.free.pop()
                if not writer.is_closing():
                    return reader, writer, used
                self.open -= 1
            if self.open < self.limit:
                self.open += 1
                try:
                    reader, writer = await asyncio.open_connection(self.host, self.port)
                except BaseException:     # зокрема скасування: лічильник має лишитися цілим
                    self.open -= 1
                    raise
                self.opened_total += 1
                return reader, writer, 0
            if time.perf_counter() >= deadline:
                return None
            await asyncio.sleep(0.005)   # усі зʼєднання зайняті: чекаємо вільне

    def give(self, reader, writer, used):
        if self.max_requests and used >= self.max_requests:   # вичерпано: закриваємо
            writer.close()
            self.open -= 1
            self.recycled += 1
            return
        self.free.append((reader, writer, used))

    def drop(self):
        self.open -= 1

    def close_all(self):
        for _, writer, _ in self.free:
            writer.close()
        self.free.clear()


async def fetch(conns, host, path, budget):
    """Один GET у межах budget секунд разом з очікуванням вільного зʼєднання.

    Повертає код статусу, STATUS_TIMEOUT або STATUS_CONNERR.
    """
    started = time.perf_counter()
    taken = None
    try:
        taken = await conns.take(budget)
        if taken is None:                 # вільного зʼєднання не дочекались
            return STATUS_TIMEOUT
        reader, writer, used = taken
        left = budget - (time.perf_counter() - started)
        if left <= 0:                     # бюджет вичерпано очікуванням зʼєднання
            writer.close()
            conns.drop()
            return STATUS_TIMEOUT
        writer.write(f"GET {path} HTTP/1.1{CRLF}Host: {host}{CRLF}{CRLF}".encode())
        await writer.drain()
        status = await asyncio.wait_for(read_response(reader), left)
    except asyncio.TimeoutError:
        if taken is not None:
            taken[1].close()
            conns.drop()
        return STATUS_TIMEOUT
    except Exception:
        if taken is not None:
            taken[1].close()
            conns.drop()
        return STATUS_CONNERR
    conns.give(reader, writer, used + 1)
    return status


# ---------------------------------------------------------------- розклад

def step_rate(s):
    """Задана інтенсивність сходинки s (s = -1 це прогрів)."""
    return WARMUP_RATE if s < 0 else RATE0 + s * RATE_STEP


def step_window(s):
    """Початок і кінець сходинки s у секундах від початку прогону."""
    if s < 0:
        return 0.0, WARMUP_S
    return WARMUP_S + s * STEP_S, WARMUP_S + (s + 1) * STEP_S


# ---------------------------------------------------------------- робочий процес

class Buckets:
    """Посекундні агрегати одного процесу."""

    def __init__(self):
        self.hist = Counter()        # скільки яких кодів за весь прогін процесу
        self.sent = defaultdict(int)
        self.c2xx = defaultdict(int)
        self.non2xx = defaultdict(int)
        self.connerr = defaultdict(int)
        self.lat = defaultdict(list)

    def add_sent(self, sec):
        self.sent[sec] += 1

    def add_done(self, sec, status, latency):
        self.hist[status] += 1
        self.lat[sec].append(latency)
        if 200 <= status < 300:
            self.c2xx[sec] += 1
        else:
            self.non2xx[sec] += 1
            if status == STATUS_CONNERR:
                self.connerr[sec] += 1

    def drain(self, upto):
        """Віддає і прибирає всі секунди, менші за upto."""
        secs = {s for d in (self.sent, self.c2xx, self.non2xx, self.connerr, self.lat)
                for s in d if s < upto}
        out = []
        for s in sorted(secs):
            out.append((s, self.sent.pop(s, 0), self.c2xx.pop(s, 0), self.non2xx.pop(s, 0),
                        self.connerr.pop(s, 0), self.lat.pop(s, [])))
        return out


async def worker_main(a, queue, stop):
    (base, path_t, ids, start_wall, widx, nworkers, seed, max_steps, max_rate,
     conns_share, req_per_conn) = a
    u = urlparse(base)
    host, port = u.hostname, u.port or 80
    conns = Connections(host, port, conns_share, req_per_conn)
    rng = random.Random(seed)
    # perf_counter, а не годинник циклу подій: на Windows time.monotonic має крок 15,6 мс,
    # і розклад зі сном збився б у пачки з таким самим кроком.
    t0 = time.perf_counter() + (start_wall - time.time())
    buckets = Buckets()
    pending = set()

    def done_cb(task, sched):
        pending.discard(task)
        if task.cancelled():   # скасування під час згортання циклу подій, не помилка запиту
            return
        try:
            status = task.result()
        except BaseException:
            status = STATUS_CONNERR
        finish = time.perf_counter() - t0
        buckets.add_done(int(finish), status, finish - sched)

    async def flusher():
        while True:
            await asyncio.sleep(0.2)
            now = time.perf_counter() - t0
            for row in buckets.drain(int(now - FLUSH_LAG)):
                queue.put((widx, row))

    flush_task = asyncio.create_task(flusher())
    stopped = False
    for s in range(-1, max_steps):
        if stopped:
            break
        rate = step_rate(s)
        begin, end = step_window(s)
        n = int(round(rate * (end - begin)))
        # Надсилання пачками: на високих інтенсивностях інтервал менший за крок таймера
        # Windows, тому сон на кожен запит зіпсував би розклад.
        j = widx
        while j < n:
            if stop.is_set():
                stopped = True
                break
            now = time.perf_counter() - t0
            if begin + j / rate > now:
                await asyncio.sleep(min(begin + j / rate - now, 0.002))
                continue
            while j < n:
                sched = begin + j / rate
                if sched > now:
                    break
                j += nworkers
                buckets.add_sent(int(now))
                path = path_t.format(id=rng.choice(ids)) if ids and "{id}" in path_t else path_t
                budget = TIMEOUT - (now - sched)
                if budget <= 0:   # розклад відстав більше ніж на таймаут
                    buckets.add_done(int(sched + TIMEOUT), STATUS_TIMEOUT, TIMEOUT)
                    continue
                task = asyncio.create_task(fetch(conns, host, path, budget))
                pending.add(task)
                task.add_done_callback(lambda t, sc=sched: done_cb(t, sc))
        if max_rate and s >= 0 and rate >= max_rate:
            break

    if pending:
        await asyncio.wait(pending, timeout=TIMEOUT + 1.0)
    flush_task.cancel()
    for row in buckets.drain(10 ** 9):
        queue.put((widx, row))
    queue.put(("conns", widx, {"limit": conns.limit, "opened": conns.opened_total,
                               "recycled": conns.recycled, "statuses": dict(buckets.hist)}))
    conns.close_all()


def sharpen_timer():
    """Windows: стандартний крок таймера ~15 мс спотворив би розклад. 1 мс на час прогону."""
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def worker(a, queue, stop):
    sharpen_timer()
    asyncio.run(worker_main(a, queue, stop))


# ---------------------------------------------------------------- батьківський процес

def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return (vals[f] + (vals[c] - vals[f]) * (k - f)) * 1000.0


class Collector:
    """Посекундні агрегати всіх процесів."""

    def __init__(self):
        self.rows = defaultdict(lambda: {"sent": 0, "c2xx": 0, "non2xx": 0, "connerr": 0,
                                         "lat": []})
        self.conns = []           # підсумки зʼєднань від кожного процесу

    def absorb(self, queue):
        while True:
            try:
                msg = queue.get_nowait()
            except Exception:
                return
            if msg[0] == "conns":   # підсумок зʼєднань процесу, а не посекундний рядок
                self.conns.append(msg[2])
                continue
            _, (sec, sent, c2xx, non2xx, connerr, lat) = msg
            r = self.rows[sec]
            r["sent"] += sent
            r["c2xx"] += c2xx
            r["non2xx"] += non2xx
            r["connerr"] += connerr
            r["lat"].extend(lat)

    def measure(self, s):
        """Вимірювальне вікно сходинки s: останні 15 с, перші 5 с перехідні."""
        begin, end = step_window(s)
        secs = range(int(begin + TRANSIENT_S), int(end))
        achieved = [self.rows[k]["c2xx"] for k in secs]
        lat = [x for k in secs for x in self.rows[k]["lat"]]
        total = sum(self.rows[k]["c2xx"] + self.rows[k]["non2xx"] for k in secs)
        non2xx = sum(self.rows[k]["non2xx"] for k in secs)
        return {
            "step": s,
            "target_rps": step_rate(s),
            "window_s": [int(begin + TRANSIENT_S), int(end)],
            "achieved_rps": statistics.fmean(achieved) if achieved else 0.0,
            "achieved_sd": statistics.stdev(achieved) if len(achieved) > 1 else 0.0,
            "sent_rps": statistics.fmean([self.rows[k]["sent"] for k in secs]) if achieved else 0.0,
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "p99_ms": pct(lat, 99),
            "responses": total,
            "non2xx": non2xx,
            "non2xx_share": non2xx / total if total else None,
            "connerr": sum(self.rows[k]["connerr"] for k in secs),
        }


def run(base, path_t, ids, out, procs=None, max_rate=None, max_steps=MAX_STEPS, label=None,
        conns=CONNS, req_per_conn=REQ_PER_CONN):
    procs = procs or max(1, (os.cpu_count() or 2) // 2)
    procs = min(procs, conns)            # на процес має припасти хоча б одне зʼєднання
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    stop = ctx.Event()
    start_wall = time.time() + START_DELAY + 0.3 * procs
    # 8·R зʼєднань на весь генератор, порівну між процесами
    shares = [conns // procs + (1 if k < conns % procs else 0) for k in range(procs)]
    args = [(base, path_t, ids, start_wall, k, procs, 5000 + k, max_steps, max_rate,
             shares[k], req_per_conn)
            for k in range(procs)]
    workers = [ctx.Process(target=worker, args=(a, queue, stop), daemon=True) for a in args]
    for w in workers:
        w.start()

    col = Collector()
    steps, low_in_row = [], 0
    reason = "max_steps"
    for s in range(max_steps):
        _, end = step_window(s)
        deadline = start_wall + end + FLUSH_LAG + 0.6
        while time.time() < deadline:
            col.absorb(queue)
            time.sleep(0.05)
        col.absorb(queue)
        m = col.measure(s)
        steps.append(m)
        print(f"сходинка {s}: задано {m['target_rps']:.0f}, досягнуто {m['achieved_rps']:.1f}"
              f" ±{m['achieved_sd']:.1f}, p99 {m['p99_ms'] and round(m['p99_ms'])} мс,"
              f" не-2xx {m['non2xx_share'] and round(m['non2xx_share'] * 100, 1)} %", flush=True)
        low_in_row = low_in_row + 1 if m["achieved_rps"] < 0.9 * m["target_rps"] else 0
        if low_in_row >= 2:
            reason = "two_low_steps"
            break
        if max_rate and m["target_rps"] >= max_rate:
            reason = "generator_limit"
            break

    stop.set()
    until = time.time() + TIMEOUT + 3.0
    while time.time() < until:
        col.absorb(queue)
        time.sleep(0.05)
    for w in workers:
        w.join(timeout=5)
        if w.is_alive():
            w.terminate()
    col.absorb(queue)

    # X(N) береться зі сходинки, яку стенд витримав: досягнуто не менше 0,9 заданої
    # і частка не-2xx менша за 1 %. Сходинка в насиченні може показати більшу досягнуту
    # інтенсивність, але з таймаутами, і пропускною здатністю вона не є.
    held = [m for m in steps
            if m["achieved_rps"] >= 0.9 * m["target_rps"] and (m["non2xx_share"] or 0) < 0.01]
    best = max(held, key=lambda m: m["achieved_rps"]) if held else (
        max(steps, key=lambda m: m["achieved_rps"]) if steps else None)
    peak = max(steps, key=lambda m: m["achieved_rps"]) if steps else None
    last = steps[-1] if steps else None
    summary = {
        "label": label,
        "base": base, "path": path_t, "procs": procs,
        "profile": {"warmup_s": WARMUP_S, "warmup_rate": WARMUP_RATE, "step_s": STEP_S,
                    "rate0": RATE0, "rate_step": RATE_STEP, "transient_s": TRANSIENT_S,
                    "timeout_s": TIMEOUT},
        "connections": {"total": conns, "per_proc": shares, "req_per_conn": req_per_conn,
                        "opened": sum(c["opened"] for c in col.conns),
                        "recycled": sum(c["recycled"] for c in col.conns)},
        # коди відповідей за весь прогін: -1 таймаут, 0 помилка зʼєднання
        "statuses": dict(sum((Counter(c.get("statuses", {})) for c in col.conns), Counter())),
        "max_rate_G": max_rate,
        "stop_reason": reason,
        "steps": steps,
        "X": best["achieved_rps"] if best else None,
        "X_sd": best["achieved_sd"] if best else None,
        "X_step": best["step"] if best else None,
        "X_target_rps": best["target_rps"] if best else None,
        "X_p50_ms": best["p50_ms"] if best else None,
        "X_p95_ms": best["p95_ms"] if best else None,
        "X_p99_ms": best["p99_ms"] if best else None,
        "X_non2xx_share": best["non2xx_share"] if best else None,
        "X_held": bool(held),
        # найбільша досягнута серед усіх сходинок, зокрема в насиченні
        "peak_achieved_rps": peak["achieved_rps"] if peak else None,
        "peak_step": peak["step"] if peak else None,
        # точка вважається обмеженою генератором, якщо остання сходинка тримала задану
        "last_step_held": bool(last and last["achieved_rps"] >= 0.9 * last["target_rps"]),
        "start_wall": start_wall,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start_wall)),
    }
    if out:
        save(out, col, summary)
    return summary


def save(out, col, summary):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with out.with_suffix(".csv").open("w", encoding="utf-8", newline="") as f:
        f.write("t_s,step,target_rps,sent_rps,achieved_rps,c2xx,non2xx,connerr,"
                "p50_ms,p95_ms,p99_ms\n")
        for sec in sorted(col.rows):
            r = col.rows[sec]
            s = step_of(sec)
            p50, p95, p99 = (pct(r["lat"], p) for p in (50, 95, 99))
            f.write(f"{sec},{s},{step_rate(s):.0f},{r['sent']},{r['c2xx']},{r['c2xx']},"
                    f"{r['non2xx']},{r['connerr']},"
                    f"{fmt(p50)},{fmt(p95)},{fmt(p99)}\n")


def fmt(v):
    return "" if v is None else f"{v:.3f}"


def step_of(sec):
    return -1 if sec < WARMUP_S else int((sec - WARMUP_S) // STEP_S)


def default_base():
    """Адреса стенду з .env; 127.0.0.1 замість localhost через IPv4/IPv6."""
    env = Path(__file__).resolve().parent.parent / ".env"
    port = "18080"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("LB_PORT="):
                port = line.split("=", 1)[1].strip()
    return "http://127.0.0.1:" + port


def _env_port(key, default):
    env = Path(__file__).resolve().parent.parent / ".env"
    port = default
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                port = line.split("=", 1)[1].strip()
    return port


def k8s_base():
    """Адреса сервісу в кластері kind (ЛР3, ЛР4): порт хоста 8080 з умови, у .env —
    K8S_WEB_PORT. Окремо від LB_PORT стенду Compose (ЛР1, ЛР2)."""
    return "http://127.0.0.1:" + _env_port("K8S_WEB_PORT", "8080")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--base", default=default_base())
    ap.add_argument("--path", default="/donor-registry/{id}")
    ap.add_argument("--ids", default=str(Path(__file__).with_name("ids.json")))
    ap.add_argument("--out", help="префікс файлів результату без розширення")
    ap.add_argument("--procs", type=int, default=None)
    ap.add_argument("--max-rate", type=float, default=None,
                    help="межа генератора G: прогін завершується після сходинки з такою заданою")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--conns", type=int, default=CONNS,
                    help="постійних зʼєднань на весь генератор (крок 6: 8·R = 32)")
    ap.add_argument("--req-per-conn", type=int, default=REQ_PER_CONN,
                    help="після скількох запитів зʼєднання закривається (крок 6: 100)")
    ap.add_argument("--label")
    a = ap.parse_args()
    ids_file = Path(a.ids)
    ids = json.loads(ids_file.read_text()) if ("{id}" in a.path and ids_file.exists()) else []
    s = run(a.base, a.path, ids, a.out, a.procs, a.max_rate, a.max_steps, a.label,
            a.conns, a.req_per_conn)
    print(json.dumps({k: v for k, v in s.items() if k != "steps"}, indent=2, ensure_ascii=False))
