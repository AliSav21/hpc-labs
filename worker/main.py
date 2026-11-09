"""Воркер пула обробки (лабораторна робота 4, варіант 19).

Окремий процес із власним образом. Бере задачу з черги в PostgreSQL, отримує запис через
HTTP від сервісу web, виконує крок обробки синтетичного зрізу і записує результат.

Режими захоплення, запису результату і очікування задаються змінними середовища: ті самі
двійкові файли проходять усі кроки роботи від наївного захоплення (крок 3) до lease з
fencing token (крок 5) і LISTEN/NOTIFY (крок 10, вимога Д5).
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
_sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

import json
import os
import random
import select
import signal
import socket
import threading
import time
import urllib.error
import urllib.request

import psycopg
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from worker.slice import SLICE_SIZE, SliceError, make_slice, process


# ---------------------------------------------------------------- налаштування

def env(name, default):
    v = os.environ.get(name, "")
    return v if v != "" else default


def envf(name, default):
    return float(env(name, default))


def envi(name, default):
    return int(float(env(name, default)))


WORKER_ID = env("WORKER_ID", socket.gethostname())
WEB_BASE = env("WEB_BASE", "http://web.donors.svc.cluster.local:8000")
RESOURCE = env("RESOURCE_PATH", "/donor-registry")

LEASE_S = envf("LEASE_S", 5.0)            # варіант 19: L = 5 с
POLL_MS = envf("POLL_MS", 50.0)           # варіант 19: T_poll = 50 мс
FAIL_RATE = envf("FAIL_RATE", 0.0)
Q_LO = envf("Q_LO", 0.02)                 # варіант 19: квантилі вікна яскравості
Q_HI = envf("Q_HI", 0.98)
SIZE = envi("SLICE_SIZE", SLICE_SIZE)

CLAIM_MODE = env("CLAIM_MODE", "skip_locked")    # naive | for_update | skip_locked
RESULT_MODE = env("RESULT_MODE", "upsert")       # insert | upsert
LEASE_MODE = env("LEASE_MODE", "on")             # off | on
HEARTBEAT = env("HEARTBEAT", "on") == "on"
WAIT_MODE = env("WAIT_MODE", "poll")             # poll | listen
REAP_IN_WORKER = env("REAP_IN_WORKER", "0") == "1"
MAX_TASKS = envi("MAX_TASKS", 0)                 # 0 — без обмеження
GRACE_S = envf("GRACE_S", 25.0)                  # < terminationGracePeriodSeconds = 30
METRICS_PORT = envi("METRICS_PORT", 9100)
HTTP_TIMEOUT = envf("HTTP_TIMEOUT_S", 5.0)

CONNINFO = (
    f"host={env('DB_HOST', 'db')} port={env('DB_PORT', '5432')} "
    f"dbname={os.environ['POSTGRES_DB']} user={os.environ['POSTGRES_USER']} "
    f"password={os.environ['POSTGRES_PASSWORD']} connect_timeout=5 "
    f"application_name=worker-{WORKER_ID}"
)

# ---------------------------------------------------------------- метрики

# Мітки за умовою: done, failed, lost. Лічильник рахує СПРОБИ, а не задачі:
#   done   — результат записано цим воркером;
#   failed — спроба не дала результату (задача повернена в чергу або, якщо
#            attempts досягло max_attempts, переведена у failed);
#   lost    — задача вже належить іншому воркеру (застарілий токен).
# Тому worker_tasks_total{result="failed"} не дорівнює кількості задач у статусі
# failed: отруєна задача (Ф3) дає три спроби і один рядок у статусі failed. Усі
# числа звіту про статуси задач беруться з бази, а не з цього лічильника.
TASKS = Counter("worker_tasks_total", "Спроби обробки за результатом", ["result"])
CLAIMS = Counter("worker_claims_total", "Спроби захоплення за результатом", ["result"])
STEP = Histogram(
    "worker_task_duration_seconds", "Тривалість кроку задачі, с", ["step"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
IN_FLIGHT = Gauge("worker_tasks_in_flight", "Задачі в обробці на цьому воркері")
# Під вимогу Д5: скільки запитів воркер робить до бази (зокрема за порожньої черги).
DB_QUERIES = Counter("worker_db_queries_total", "Запити воркера до бази", ["kind"])
RECONNECTS = Counter("worker_db_reconnects_total", "Перепідключення до бази")
STALE = Counter("worker_stale_token_total", "Команди, що оновили нуль рядків", ["op"])
INFO = Gauge("worker_config", "Налаштування воркера",
             ["claim_mode", "result_mode", "lease_mode", "wait_mode"])
INFO.labels(CLAIM_MODE, RESULT_MODE, LEASE_MODE, WAIT_MODE).set(1)
for _r in ("done", "failed", "lost"):
    TASKS.labels(_r).inc(0)
for _r in ("task", "empty"):
    CLAIMS.labels(_r).inc(0)

TASK_COLS = "id, record_id, seed, token, attempts, max_attempts"
TASK_KEYS = [c.strip() for c in TASK_COLS.split(",")]


def log(msg, **kw):
    """Рядок журналу у вигляді JSON: за ним рахуються дублі й застарілі токени."""
    rec = {"ts": round(time.time(), 3), "worker": WORKER_ID, "msg": msg}
    rec.update(kw)
    print(json.dumps(rec, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------- стан зупинки

class Stop:
    """SIGTERM: нові задачі не захоплюються, задача в обробці дочікується."""

    def __init__(self):
        self.flag = threading.Event()
        self.at = None

    def request(self, signum, _frame):
        if not self.flag.is_set():
            self.at = time.time()
            self.flag.set()
            log("SIGTERM: припиняю захоплення нових задач", signal=signum)

    @property
    def stopping(self):
        return self.flag.is_set()

    @property
    def over_grace(self):
        return self.at is not None and time.time() - self.at > GRACE_S


# ---------------------------------------------------------------- доступ до бази

class Db:
    """Зʼєднання з автоматичним перепідключенням. Кожен запит рахується в метриці."""

    def __init__(self, conninfo):
        self.conninfo = conninfo
        self.conn = None

    def get(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.conninfo, autocommit=True)
        return self.conn

    def drop(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def run(self, sql, params=(), kind="other", fetch=None):
        """Один запит. fetch: None, 'one' або 'all'. Повертає (рядки, rowcount)."""
        DB_QUERIES.labels(kind).inc()
        for attempt in (1, 2):
            try:
                with self.get().cursor() as cur:
                    cur.execute(sql, params)
                    rows = None
                    if fetch == "one":
                        rows = cur.fetchone()
                    elif fetch == "all":
                        rows = cur.fetchall()
                    return rows, cur.rowcount
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                self.drop()
                if attempt == 2:
                    raise
                RECONNECTS.inc()
                log("зʼєднання з базою втрачено, перепідключаюсь",
                    error=str(e).strip(), sql_kind=kind)


# ---------------------------------------------------------------- захоплення

CLAIM_SQL = {
    # крок 4: одна команда, рядок обирається з блокуванням і пропуском заблокованих
    "skip_locked": f"""
        UPDATE tasks SET status = 'running', worker_id = %s, token = token + 1,
               lease_until = now() + %s::interval, attempts = attempts + 1, started_at = now()
        WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
                    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
        RETURNING {TASK_COLS}""",
    # крок 4, друга частина: без SKIP LOCKED усі воркери чекають на той самий рядок
    "for_update": f"""
        UPDATE tasks SET status = 'running', worker_id = %s, token = token + 1,
               lease_until = now() + %s::interval, attempts = attempts + 1, started_at = now()
        WHERE id = (SELECT id FROM tasks WHERE status = 'queued'
                    ORDER BY created_at LIMIT 1 FOR UPDATE)
        RETURNING {TASK_COLS}""",
}

NAIVE_SELECT = (f"SELECT {TASK_COLS} FROM tasks WHERE status = 'queued' "
                f"ORDER BY created_at LIMIT 1")
NAIVE_UPDATE = ("UPDATE tasks SET status = 'running', worker_id = %s, "
                "attempts = attempts + 1, started_at = now() WHERE id = %s")


def claim(db):
    """Одна спроба захоплення. Повертає задачу або None."""
    t0 = time.perf_counter()
    try:
        if CLAIM_MODE == "naive":
            # Крок 3: дві команди, без транзакції і без блокувань. Між SELECT одного воркера
            # і його UPDATE інший воркер читає той самий рядок — звідси дублі.
            row, _ = db.run(NAIVE_SELECT, (), kind="claim_select", fetch="one")
            if row is None:
                return None
            db.run(NAIVE_UPDATE, (WORKER_ID, row[0]), kind="claim_update")
        else:
            lease = f"{LEASE_S if LEASE_MODE == 'on' else 10 ** 6} seconds"
            row, _ = db.run(CLAIM_SQL[CLAIM_MODE], (WORKER_ID, lease),
                            kind="claim", fetch="one")
            if row is None:
                return None
    finally:
        STEP.labels("claim").observe(time.perf_counter() - t0)
    task = dict(zip(TASK_KEYS, row))
    CLAIMS.labels("task").inc()
    log("захоплено", task=task["id"], token=task["token"], attempts=task["attempts"])
    return task


REAP_SQL = """
    UPDATE tasks SET
        status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
        worker_id = NULL, lease_until = NULL,
        finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE finished_at END
    WHERE status = 'running' AND lease_until < now()
    RETURNING id, attempts, status"""


def reap(db):
    """Повертає в чергу задачі зі спливлим lease; після max_attempts — у failed."""
    rows, _ = db.run(REAP_SQL, (), kind="reap", fetch="all")
    for tid, attempts, status in rows or ():
        log("lease сплив, задача знята з воркера", task=tid, attempts=attempts, to=status)
    return rows or []


# ---------------------------------------------------------------- кроки задачі

def fetch_record(record_id):
    """Крок 1: запис за record_id через HTTP від сервісу web."""
    url = f"{WEB_BASE}{RESOURCE}/{record_id}"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read())


RELEASE_SQL = """
    UPDATE tasks SET
        status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
        worker_id = NULL, lease_until = NULL,
        finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE finished_at END
    WHERE id = %s AND token = %s
    RETURNING status"""

REQUEUE_SQL = ("UPDATE tasks SET status = 'queued', worker_id = NULL, lease_until = NULL "
               "WHERE id = %s AND token = %s")

DONE_SQL = "UPDATE tasks SET status = 'done', finished_at = now() WHERE id = %s AND token = %s"
DONE_SQL_NAIVE = "UPDATE tasks SET status = 'done', finished_at = now() WHERE id = %s"

RESULT_SQL = {
    # крок 5: повторне виконання задачі не створює другого результату
    "upsert": ("INSERT INTO task_results (task_id, token, worker_id, result) "
               "VALUES (%s, %s, %s, %s) ON CONFLICT (task_id) DO NOTHING"),
    # крок 3: без ON CONFLICT, первинний ключ тимчасово замінений звичайним індексом
    "insert": ("INSERT INTO task_results (task_id, token, worker_id, result) "
               "VALUES (%s, %s, %s, %s)"),
}


def store(db, task, result):
    """Крок 3 задачі: завершення задачі і запис результату.

    У режимі lease обидві команди йдуть однією транзакцією з умовою за токеном; задача,
    що вже належить іншому воркеру, дає нуль оновлених рядків і результат не пишеться.
    """
    payload = json.dumps(result, ensure_ascii=False)
    if LEASE_MODE == "off":
        db.run(RESULT_SQL[RESULT_MODE], (task["id"], task["token"], WORKER_ID, payload),
               kind="store")
        db.run(DONE_SQL_NAIVE, (task["id"],), kind="finish")
        return True
    conn = db.get()
    DB_QUERIES.labels("store_tx").inc()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(DONE_SQL, (task["id"], task["token"]))
            if cur.rowcount == 0:
                STALE.labels("finish").inc()
                log("нуль оновлених рядків: задача більше не моя", task=task["id"],
                    token=task["token"])
                return False
            cur.execute(RESULT_SQL[RESULT_MODE],
                        (task["id"], task["token"], WORKER_ID, payload))
    return True


# ---------------------------------------------------------------- heartbeat

HEARTBEAT_SQL = ("UPDATE tasks SET lease_until = now() + %s::interval "
                 "WHERE id = %s AND token = %s")


class Heartbeat(threading.Thread):
    """Продовжує lease кожні L / 3 с з умовою за токеном.

    Нуль оновлених рядків означає, що задачу вже забрав інший воркер: потік це фіксує в
    журналі й у метриці, а головний цикл перевіряє прапорець lost перед записом результату.
    Той самий потік стежить за бюджетом коректної зупинки: якщо після SIGTERM задача не
    завершилась за GRACE_S, вона повертається в чергу і процес виходить з кодом 0.
    """

    def __init__(self, task, stop):
        super().__init__(daemon=True)
        self.task = task
        self.stop = stop
        self.done = threading.Event()
        self.lost = False
        self.db = Db(CONNINFO)

    def run(self):
        period = max(LEASE_S / 3.0, 0.1)
        try:
            while not self.done.wait(period):
                if self.stop.over_grace:
                    self.requeue_and_exit()
                if not HEARTBEAT or LEASE_MODE == "off":
                    continue
                _, n = self.db.run(HEARTBEAT_SQL,
                                   (f"{LEASE_S} seconds", self.task["id"], self.task["token"]),
                                   kind="heartbeat")
                if n == 0:
                    self.lost = True
                    STALE.labels("heartbeat").inc()
                    log("heartbeat оновив нуль рядків: задача більше не моя",
                        task=self.task["id"], token=self.task["token"])
                    return
        except Exception as e:
            log("heartbeat не вдався", task=self.task["id"], error=str(e).strip())
        finally:
            self.db.drop()

    def requeue_and_exit(self):
        """Бюджет коректної зупинки вичерпано: задача повертається в чергу."""
        try:
            _, n = self.db.run(REQUEUE_SQL, (self.task["id"], self.task["token"]),
                               kind="requeue")
            log("зупинка: задача повернута в чергу", task=self.task["id"],
                token=self.task["token"], rows=n)
        except Exception as e:
            log("зупинка: не вдалось повернути задачу", task=self.task["id"],
                error=str(e).strip())
        os._exit(0)


# ---------------------------------------------------------------- очікування задачі

class Waiter:
    """Очікування наступної задачі: опитування або LISTEN/NOTIFY (вимога Д5).

    У режимі listen окреме зʼєднання слухає канал tasks_new; порожнє захоплення чекає
    повідомлення, а не спить T_poll. Пробудження лишається обмеженим стелею
    LISTEN_TIMEOUT_S, щоб задача, повернена в чергу без вставки (lease, SIGTERM), не
    лишилась незабраною: NOTIFY надсилає лише тригер на вставку.
    """

    def __init__(self):
        self.conn = None
        self.timeout = envf("LISTEN_TIMEOUT_S", 1.0)

    def listen_conn(self):
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(CONNINFO, autocommit=True)
            self.conn.execute("LISTEN tasks_new")
            DB_QUERIES.labels("listen").inc()
            log("слухаю канал tasks_new")
        return self.conn

    def wait(self, stop):
        if WAIT_MODE != "listen":
            stop.flag.wait(POLL_MS / 1000.0)
            return
        try:
            conn = self.listen_conn()
            # select на сокеті зʼєднання: пробудження лише на повідомлення або на стелю,
            # запитів до бази під час очікування немає
            ready, _, _ = select.select([conn], [], [], self.timeout)
            if ready:
                conn.execute("SELECT 1")   # змушує psycopg вичитати вхідні повідомлення
                DB_QUERIES.labels("listen_drain").inc()
        except Exception as e:
            log("очікування повідомлення не вдалося", error=str(e).strip())
            self.close()
            stop.flag.wait(POLL_MS / 1000.0)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


# ---------------------------------------------------------------- одна задача

def handle(db, task, stop):
    """Три кроки задачі: отримання запису, обробка зрізу, запис результату."""
    IN_FLIGHT.set(1)
    hb = Heartbeat(task, stop)
    hb.start()
    try:
        t0 = time.perf_counter()
        record = fetch_record(task["record_id"])
        t_fetch = time.perf_counter() - t0
        STEP.labels("fetch").observe(t_fetch)

        t0 = time.perf_counter()
        result = process(make_slice(task["seed"], SIZE), Q_LO, Q_HI)
        t_cpu = time.perf_counter() - t0
        STEP.labels("process").observe(t_cpu)
        result["record"] = {"id": record.get("id"), "donor_code": record.get("donor_code")}
        result["q_lo"], result["q_hi"], result["size"] = Q_LO, Q_HI, SIZE

        # Вбудоване падіння: після кроку обробки і до запису результату, без обробки
        # сигналів і без закриття зʼєднань.
        if FAIL_RATE > 0 and random.random() < FAIL_RATE:
            log("вбудоване падіння", task=task["id"], token=task["token"],
                fail_rate=FAIL_RATE)
            os._exit(1)

        if hb.lost:
            TASKS.labels("lost").inc()
            log("задача втрачена до запису результату", task=task["id"])
            return "lost"

        t0 = time.perf_counter()
        ok = store(db, task, result)
        t_store = time.perf_counter() - t0
        STEP.labels("store").observe(t_store)
        if not ok:
            TASKS.labels("lost").inc()
            return "lost"
        TASKS.labels("done").inc()
        log("виконано", task=task["id"], token=task["token"],
            t_io_s=round(t_fetch + t_store, 6), t_cpu_s=round(t_cpu, 6),
            p_lo=result["p_lo"], p_hi=result["p_hi"], dropped=result["dropped"])
        return "done"

    except (SliceError, urllib.error.URLError, OSError, ValueError,
            psycopg.Error) as e:
        # Крок обробки, отримання запису або запис результату не вдались: задача
        # повертається в чергу, а після max_attempts переходить у failed (Ф3).
        # psycopg.Error тут обовʼязково: без нього обрив зʼєднання під час запису
        # результату вилітав би з handle() і валив увесь процес воркера.
        log("задача не виконана", task=task["id"], token=task["token"],
            error=f"{type(e).__name__}: {e}")
        try:
            if LEASE_MODE == "off":
                db.run("UPDATE tasks SET status = 'failed', finished_at = now() WHERE id = %s",
                       (task["id"],), kind="release")
                status = "failed"
            else:
                row, _ = db.run(RELEASE_SQL, (task["id"], task["token"]),
                                kind="release", fetch="one")
                status = row[0] if row else None
            if status is None:
                STALE.labels("release").inc()
                log("нуль оновлених рядків під час повернення", task=task["id"])
                TASKS.labels("lost").inc()
                return "lost"
            log("задача повернута", task=task["id"], to=status, attempts=task["attempts"])
        except Exception as e2:
            log("не вдалось повернути задачу", task=task["id"], error=str(e2).strip())
        TASKS.labels("failed").inc()
        return "failed"
    finally:
        hb.done.set()
        hb.join(timeout=2)
        IN_FLIGHT.set(0)


# ---------------------------------------------------------------- головний цикл

def main():
    stop = Stop()
    signal.signal(signal.SIGTERM, stop.request)
    signal.signal(signal.SIGINT, stop.request)
    start_http_server(METRICS_PORT)
    log("старт", claim_mode=CLAIM_MODE, result_mode=RESULT_MODE, lease_mode=LEASE_MODE,
        wait_mode=WAIT_MODE, heartbeat=HEARTBEAT, lease_s=LEASE_S, poll_ms=POLL_MS,
        fail_rate=FAIL_RATE, q_lo=Q_LO, q_hi=Q_HI, size=SIZE, web=WEB_BASE,
        max_tasks=MAX_TASKS, reap_in_worker=REAP_IN_WORKER)

    db = Db(CONNINFO)
    waiter = Waiter()
    handled = 0
    while not stop.stopping:
        try:
            if REAP_IN_WORKER and LEASE_MODE == "on":
                reap(db)
            task = claim(db)
        except Exception as e:
            CLAIMS.labels("empty").inc()
            log("захоплення не вдалося", error=str(e).strip())
            db.drop()
            stop.flag.wait(min(1.0, POLL_MS / 1000.0 * 10))
            continue
        if task is None:
            CLAIMS.labels("empty").inc()
            waiter.wait(stop)
            continue
        try:
            handle(db, task, stop)
        except Exception as e:
            # Запобіжник: несподівана помилка не має валити воркер. Задача лишається
            # в running і повертається в чергу за спливанням lease.
            log("непередбачена помилка під час обробки", task=task["id"],
                error=f"{type(e).__name__}: {e}")
            db.drop()
        handled += 1
        if MAX_TASKS and handled >= MAX_TASKS:
            log("виконано задану кількість задач, виходжу", handled=handled)
            break

    waiter.close()
    db.drop()
    log("зупинка завершена", handled=handled, graceful=stop.stopping)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
