"""Прогони лабораторної роботи 4 (варіант 19): кроки 2-10.

Кожна підкоманда виконує один крок роботи цілком: готує стенд (режими воркера, кількість
реплік, стан черги), веде прогін, знімає метрики з Prometheus, журнали воркерів і зрізи
бази, і складає результат у results/lab4/<назва>.json.

Запуск:  python -m scripts.lab4_run <крок> [ключі]
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252

import argparse
import json
import re
import statistics
import subprocess
import tempfile as _tempfile
import threading
import time
from pathlib import Path

from loadgen import prom, tasks

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "lab4"
NS = "donors"
CTX = "kind-hpc"
BASE = "http://127.0.0.1:18080"
RESOURCE = "/donor-registry"
IDS = json.loads((ROOT / "loadgen" / "ids.json").read_text())

# параметри варіанта 19
T_POLL_MS = 50
LEASE_S = 5
Q_LO, Q_HI = 0.02, 0.98
R = 4                      # найбільша кількість воркерів із ЛР3


# ---------------------------------------------------------------- стенд

def kubectl(*args, check=True, capture=True, quiet=False):
    cmd = ["kubectl", "--context", CTX, *args]
    if not quiet:
        print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=capture,
                          text=True, encoding="utf-8", errors="replace")


def psql(sql, quiet=True):
    """Рядки як список списків (розділювач |)."""
    r = kubectl("exec", "-n", NS, "db-0", "-c", "postgres", "--",
                "psql", "-U", "donors", "-d", "donors", "-At", "-F", "|", "-c", sql,
                quiet=quiet)
    return [line.split("|") for line in r.stdout.strip().splitlines() if line != ""]


def psql_json(sql, quiet=True):
    """SELECT ... -> список словників.

    jsonb_agg, а не json_agg: json_agg вставляє переведення рядка після кожного елемента,
    і вивід psql у режимі -At розпадався б на кілька рядків.
    """
    r = kubectl("exec", "-n", NS, "db-0", "-c", "postgres", "--",
                "psql", "-U", "donors", "-d", "donors", "-At", "-c",
                f"SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM ({sql}) t", quiet=quiet)
    text = r.stdout.strip()
    return json.loads(text) if text else []


def psql_one(sql, quiet=True):
    rows = psql(sql, quiet=quiet)
    return rows[0][0] if rows and rows[0] else None


def scale_worker(n, wait=True):
    kubectl("scale", "deployment/worker", "-n", NS, f"--replicas={n}")
    if wait:
        kubectl("rollout", "status", "deployment/worker", "-n", NS, "--timeout=300s",
                capture=False)
        wait_worker_pods(n)


def worker_pods():
    r = kubectl("get", "pods", "-n", NS, "-l", "app=worker",
                "--field-selector=status.phase=Running", "-o",
                "jsonpath={range .items[*]}{.metadata.name}={.status.containerStatuses[0].ready}"
                "={.status.containerStatuses[0].restartCount} {end}", quiet=True)
    out = []
    for item in r.stdout.split():
        name, ready, restarts = item.split("=")
        out.append({"pod": name, "ready": ready == "true", "restarts": int(restarts)})
    return out


def wait_worker_pods(n, timeout=240.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = [p for p in worker_pods() if p["ready"]]
        if len(pods) == n:
            return pods
        time.sleep(1.0)
    raise RuntimeError(f"готових воркерів {len(worker_pods())}, а треба {n}")


def set_worker_env(**kw):
    """Режими воркера через kubectl set env: ConfigMap лишається джерелом значень варіанта."""
    unknown = [k for k in kw if k not in OVERRIDABLE]
    if unknown:
        # інакше таке перевизначення переживе reset_worker_env і потрапить у наступний прогін
        raise RuntimeError(f"змінні поза переліком OVERRIDABLE: {unknown}")
    args = [f"{k}={v}" for k, v in kw.items()]
    kubectl("set", "env", "deployment/worker", "-n", NS, *args)
    kubectl("rollout", "status", "deployment/worker", "-n", NS, "--timeout=300s",
            capture=False)


# Усе, що коли-небудь задає set_worker_env. Перелік один на обидві функції: окремий
# перелік у reset_worker_env уже лишав був SUPERVISE=1 висіти після кроку 9.
OVERRIDABLE = ["CLAIM_MODE", "RESULT_MODE", "LEASE_MODE", "HEARTBEAT", "WAIT_MODE",
               "POLL_MS", "LEASE_S", "FAIL_RATE", "MAX_TASKS", "REAP_IN_WORKER",
               "SUPERVISE", "GRACE_S", "Q_LO", "Q_HI", "SLICE_SIZE", "LISTEN_TIMEOUT_S"]


def reset_worker_env():
    """Знімає всі перевизначення: воркер повертається до значень ConfigMap."""
    kubectl("set", "env", "deployment/worker", "-n", NS,
            *[f"{k}-" for k in OVERRIDABLE], check=False)
    kubectl("rollout", "status", "deployment/worker", "-n", NS, "--timeout=300s",
            capture=False)
    left = [ln for ln in kubectl("set", "env", "deployment/worker", "-n", NS, "--list",
                                 quiet=True, check=False).stdout.splitlines()
            if ln.strip() and not ln.startswith("#")]
    if left:
        raise RuntimeError("після скидання лишились перевизначення: " + ", ".join(left))


def clear_queue(attempts=6):
    """Очищає чергу перед прогоном.

    TRUNCATE бере ACCESS EXCLUSIVE і конфліктує з воркерами, які в цей момент ще
    опитують таблицю після попереднього прогону: команда падає, і вся серія
    обривається на півдорозі. Тому пробуємо кілька разів, а як не вийде — видаляємо
    рядки звичайним DELETE, який уживається зі звичайними запитами.
    """
    for k in range(attempts):
        r = kubectl("exec", "-n", NS, "db-0", "-c", "postgres", "--",
                    "psql", "-U", "donors", "-d", "donors", "-At", "-c",
                    "SET lock_timeout = '5s'; "
                    "TRUNCATE task_results, tasks RESTART IDENTITY CASCADE",
                    check=False, quiet=(k > 0))
        if r.returncode == 0:
            return
        print(f"  TRUNCATE не вдався (спроба {k + 1}): "
              f"{(r.stderr or '').strip()[:120]}", flush=True)
        time.sleep(2)
    print("  TRUNCATE не вдався, чищу DELETE", flush=True)
    psql("DELETE FROM task_results; DELETE FROM tasks", quiet=False)


def results_pk(on):
    """Первинний ключ task_results (крок 3 вимагає тимчасово замінити його індексом)."""
    has_pk = psql_one("SELECT count(*) FROM pg_constraint "
                      "WHERE conname = 'task_results_pkey'") == "1"
    if on:
        if has_pk:
            return
        psql("DROP INDEX IF EXISTS idx_task_results_task_id", quiet=False)
        # дублі, лишені прогоном кроку 3, не дали б створити первинний ключ
        psql("DELETE FROM task_results a USING task_results b "
             "WHERE a.task_id = b.task_id AND a.ctid > b.ctid", quiet=False)
        psql("ALTER TABLE task_results ADD PRIMARY KEY (task_id)", quiet=False)
    else:
        psql("ALTER TABLE task_results DROP CONSTRAINT IF EXISTS task_results_pkey",
             quiet=False)
        psql("CREATE INDEX IF NOT EXISTS idx_task_results_task_id ON task_results (task_id)",
             quiet=False)


def drain(timeout=600.0, quiet_s=3.0):
    """Чекає, поки черга спорожніє (ні queued, ні running протягом quiet_s)."""
    deadline = time.time() + timeout
    calm_since = None
    while time.time() < deadline:
        n = int(psql_one("SELECT count(*) FROM tasks WHERE status IN ('queued','running')"))
        if n == 0:
            calm_since = calm_since or time.time()
            if time.time() - calm_since >= quiet_s:
                return True
        else:
            calm_since = None
        time.sleep(0.5)
    return False


def worker_logs(since_s=None):
    """Журнали всіх подів worker рядками JSON з іменем пода."""
    args = ["logs", "-n", NS, "-l", "app=worker", "-c", "worker", "--prefix",
            "--tail=-1", "--timestamps=false"]
    if since_s:
        args.append(f"--since={int(since_s)}s")
    r = kubectl(*args, check=False, quiet=True)
    out = []
    for line in r.stdout.splitlines():
        m = re.match(r"\[pod/([^/]+)/worker\]\s*(\{.*\})\s*$", line)
        if not m:
            continue
        try:
            rec = json.loads(m.group(2))
        except Exception:
            continue
        rec["pod"] = m.group(1)
        out.append(rec)
    return out


def save(name, data):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                 encoding="utf-8")
    print(f"-> {p.relative_to(ROOT)}", flush=True)
    return p


# ---------------------------------------------------------------- метрики

PROM = prom.default_url()
WRK = 'namespace="donors",container="worker"'


def pq(expr, at, default=None):
    try:
        return prom.scalar(PROM, expr, at, default)
    except Exception:
        return default


def step_quantile(name, start, end, q=0.99):
    """Квантиль тривалості кроку задачі за гістограмою всіх воркерів на вікні.

    Приріст рахується через increase(), а не різницею значень на краях вікна: воркери
    перестворюються між прогонами (kubectl set env, scale), і різниця двох знімків давала
    бʼємний «приріст», коли на початку вікна в Prometheus ще лежали лічильники старих подів.
    """
    # вікно дорівнює самому прогону: ширше вікно затягує порожні захоплення з часу
    # до прогону і змазує різницю між режимами захоплення
    win = max(int(round(end - start)), 10)
    expr = (f'sum by (le) (increase(worker_task_duration_seconds_bucket'
            f'{{step="{name}"}}[{win}s]))')
    try:
        cum = sorted(((float(m["le"]), v) for m, v in prom.query(PROM, expr, end)),
                     key=lambda x: x[0])
        return prom.quantile_from_buckets(cum, q)
    except Exception as e:
        return {"error": str(e)}


def claim_p99(start, end):
    """p99 часу захоплення за гістограмою воркерів на вікні."""
    return step_quantile("claim", start, end, 0.99)


def counter_delta(expr, start, end):
    a = pq(expr, start, 0.0) or 0.0
    b = pq(expr, end, 0.0) or 0.0
    return b - a


def db_metrics(start, end):
    """Транзакції бази за секунду і використання CPU пода бази на вікні."""
    # вікно rate не менше 60 с: інтервал збору 5 с, і на короткому прогоні
    # лічильники cAdvisor не дають двох точок у вікні
    win = max(int(end - start), 60)
    return {
        "db_tps": pq(f'sum(rate(pg_stat_database_xact_commit{{datname="donors"}}[{win}s]))'
                     f' + sum(rate(pg_stat_database_xact_rollback{{datname="donors"}}[{win}s]))',
                     end),
        "db_cpu_cores": pq(f'sum(rate(container_cpu_usage_seconds_total'
                           f'{{namespace="donors",container="postgres"}}[{win}s]))', end),
        "worker_cpu_cores": pq(f'sum(rate(container_cpu_usage_seconds_total'
                               f'{{{WRK}}}[{win}s]))', end),
    }


class LockSampler(threading.Thread):
    """Знімає щосекунди кількість рядків pg_stat_activity з wait_event_type = 'Lock'.

    Увесь цикл виконується одним kubectl exec усередині пода бази. Варіант «один exec на
    знімок» давав насправді знімок раз на ~2,3 с: сам виклик kubectl exec коштує понад
    секунду, і за 15-секундний прогін виходило 7 точок замість 15.
    """

    def __init__(self, period=1.0, seconds=600):
        super().__init__(daemon=True)
        self.period = period
        self.seconds = seconds
        self.samples = []
        self.proc = None

    def run(self):
        script = (
            "for i in $(seq 1 %d); do "
            "  psql -U donors -d donors -At -F'|' -c "
            "\"select extract(epoch from clock_timestamp()), "
            "    (select count(*) from pg_stat_activity where wait_event_type='Lock'), "
            "    (select count(*) from pg_locks where not granted), "
            "    coalesce((select string_agg(distinct wait_event || ' @ ' "
            "       || left(regexp_replace(query, '\s+', ' ', 'g'), 48), ' ; ') "
            "       from pg_stat_activity where wait_event_type='Lock'), '')\"; "
            "  sleep %g; done" % (int(self.seconds / max(self.period, 0.05)), self.period)
        )
        self.proc = subprocess.Popen(
            ["kubectl", "--context", CTX, "exec", "-n", NS, "db-0", "-c", "postgres",
             "--", "sh", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", cwd=ROOT)
        for line in self.proc.stdout:
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            try:
                rec = {"t": round(float(parts[0]), 2), "lock_waits": int(parts[1]),
                       "locks_ungranted": int(parts[2])}
            except ValueError:
                continue
            if len(parts) > 3 and parts[3]:
                rec["waiting_on"] = parts[3]
            self.samples.append(rec)

    def result(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.join(timeout=10)
        vals = [s["lock_waits"] for s in self.samples]
        ung = [s["locks_ungranted"] for s in self.samples]
        span = ((self.samples[-1]["t"] - self.samples[0]["t"])
                if len(self.samples) > 1 else 0.0)
        waits = sorted({s["waiting_on"] for s in self.samples if s.get("waiting_on")})
        return {"samples": len(self.samples),
                "waiting_on": waits[:10],
                "span_s": round(span, 2),
                "interval_s": round(span / (len(self.samples) - 1), 3)
                              if len(self.samples) > 1 else None,
                "lock_waits_max": max(vals) if vals else None,
                "lock_waits_mean": statistics.fmean(vals) if vals else None,
                "locks_ungranted_max": max(ung) if ung else None,
                "series": self.samples}


# ---------------------------------------------------------------- зрізи черги

class PodLogKeeper(threading.Thread):
    """Тримає останню копію журналів названих подів.

    Під час rollout restart старі поди зникають разом із журналами, тому рядки про
    SIGTERM і про повернення задач у чергу треба знімати доки под існує.
    """

    def __init__(self, pods, period=0.0):
        super().__init__(daemon=True)
        self.pods = list(pods)
        self.dir = Path(_tempfile.mkdtemp(prefix="lab4logs-"))
        self.procs = {}
        self.files = {}
        self.text = {}

    def start(self):
        """kubectl logs -f на кожен под: потік триває доки под існує."""
        for pod in self.pods:
            f = (self.dir / f"{pod}.log").open("w", encoding="utf-8", errors="replace")
            self.files[pod] = f
            self.procs[pod] = subprocess.Popen(
                ["kubectl", "--context", CTX, "logs", "-n", NS, pod, "-c", "worker",
                 "-f", "--tail=-1"], stdout=f, stderr=subprocess.DEVNULL, cwd=ROOT)

    def result(self):
        for pod, pr in self.procs.items():
            try:
                pr.wait(timeout=5)
            except Exception:
                pr.terminate()
        for f in self.files.values():
            f.close()
        for pod in self.pods:
            path = self.dir / f"{pod}.log"
            if path.exists():
                self.text[pod] = path.read_text(encoding="utf-8", errors="replace")
        out = []
        for pod, text in self.text.items():
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rec["pod"] = pod
                out.append(rec)
        return out


class PodWatcher(threading.Thread):
    """Стежить за подами worker і запамʼятовує завершені контейнери.

    Код завершення доступний лише поки под існує: після rollout restart старі поди
    видаляються за кілька секунд, і запит після прогону вже нічого не знаходить.
    """

    def __init__(self, period=0.5):
        super().__init__(daemon=True)
        self.period = period
        self.stop = threading.Event()
        self.terminated = {}      # (под, контейнер, finishedAt) -> запис
        self.seen = {}            # под -> остання кількість перезапусків

    JSONPATH = ("{range .items[*]}{.metadata.name}"
                "|{.status.containerStatuses[0].restartCount}"
                "|{.status.containerStatuses[0].state.terminated.exitCode}"
                "|{.status.containerStatuses[0].state.terminated.reason}"
                "|{.status.containerStatuses[0].state.terminated.finishedAt}"
                "|{.status.containerStatuses[0].lastState.terminated.exitCode}"
                "|{.status.containerStatuses[0].lastState.terminated.reason}"
                "|{.status.containerStatuses[0].lastState.terminated.finishedAt}"
                " {end}")

    def poll(self):
        # jsonpath, а не -o json: під час rollout restart под живе кілька секунд, і
        # розбір повного обʼєкта не встигав зняти код завершення всіх старих подів
        r = kubectl("get", "pods", "-n", NS, "-l", "app=worker", "-o",
                    "jsonpath=" + self.JSONPATH, quiet=True, check=False)
        for item in r.stdout.split():
            parts = item.split("|")
            if len(parts) < 8 or not parts[0]:
                continue
            pod, restarts = parts[0], parts[1]
            self.seen[pod] = restarts
            for where, (code, reason, fin) in (("state", parts[2:5]),
                                               ("lastState", parts[5:8])):
                if code == "":
                    continue
                self.terminated.setdefault((pod, fin), {
                    "pod": pod, "container": "worker", "exit_code": int(code),
                    "reason": reason, "finished_at": fin,
                    "restart_count": int(restarts or 0), "from": where,
                })

    def run(self):
        while not self.stop.wait(self.period):
            try:
                self.poll()
            except Exception:
                pass

    def result(self):
        self.stop.set()
        try:
            self.poll()
        except Exception:
            pass
        self.join(timeout=5)
        return sorted(self.terminated.values(), key=lambda r: str(r["finished_at"]))


def task_stats():
    """Стан черги і результатів після прогону."""
    by_status = {r["status"]: r["n"] for r in psql_json(
        "SELECT status, count(*)::int AS n FROM tasks GROUP BY status")}
    by_attempts = {str(r["attempts"]): r["n"] for r in psql_json(
        "SELECT attempts, count(*)::int AS n FROM tasks GROUP BY attempts ORDER BY attempts")}
    dup = psql_json(
        "SELECT task_id, count(*)::int AS n FROM task_results GROUP BY task_id "
        "HAVING count(*) > 1 ORDER BY task_id")
    return {
        "tasks_total": int(psql_one("SELECT count(*) FROM tasks")),
        "by_status": by_status,
        "by_attempts": by_attempts,
        "results_rows": int(psql_one("SELECT count(*) FROM task_results")),
        "tasks_with_result": int(psql_one("SELECT count(DISTINCT task_id) FROM task_results")),
        "tasks_with_many_results": len(dup),
        "duplicates": dup[:50],
        "attempts_gt1": int(psql_one("SELECT count(*) FROM tasks WHERE attempts > 1")),
    }


def wait_stats(where="TRUE"):
    """W_q = started_at − created_at за задачами: середнє, p50, p99."""
    rows = psql_json(
        "SELECT extract(epoch FROM (started_at - created_at)) AS w FROM tasks "
        f"WHERE started_at IS NOT NULL AND ({where})")
    vals = sorted(float(r["w"]) for r in rows if r["w"] is not None)
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean_s": statistics.fmean(vals),
            "p50_s": quant(vals, 0.5), "p99_s": quant(vals, 0.99),
            "min_s": vals[0], "max_s": vals[-1]}


def quant(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def duplicates_from_logs(logs):
    """Дублі за журналами: задачі, позначені виконаними більше одного разу."""
    done = {}
    for rec in logs:
        if rec.get("msg") == "виконано":
            done.setdefault(rec["task"], []).append(rec.get("pod") or rec.get("worker"))
    dup = {str(t): w for t, w in done.items() if len(w) > 1}
    many_workers = {t: w for t, w in dup.items() if len(set(w)) > 1}
    return {"tasks_done_lines": len(done),
            "tasks_done_more_than_once": len(dup),
            "of_them_by_different_workers": len(many_workers),
            "examples": dict(list(dup.items())[:20])}


def per_second_done(logs, start_wall, end_wall):
    """Посекундна кількість завершених задач за журналами воркерів."""
    per = {}
    for rec in logs:
        if rec.get("msg") != "виконано":
            continue
        t = rec["ts"]
        if start_wall <= t <= end_wall:
            per[int(t - start_wall)] = per.get(int(t - start_wall), 0) + 1
    return [{"t_s": s, "done": per.get(s, 0)}
            for s in range(int(end_wall - start_wall) + 1)]


def io_cpu_from_logs(logs):
    """t_io і t_cpu за рядками журналу «виконано»."""
    io = [r["t_io_s"] for r in logs if r.get("msg") == "виконано" and "t_io_s" in r]
    cpu = [r["t_cpu_s"] for r in logs if r.get("msg") == "виконано" and "t_cpu_s" in r]
    out = {"n": len(io)}
    for name, vals in (("t_io", io), ("t_cpu", cpu)):
        if vals:
            s = sorted(vals)
            out[name] = {"mean_s": statistics.fmean(vals), "p50_s": quant(s, 0.5),
                         "p99_s": quant(s, 0.99), "min_s": s[0], "max_s": s[-1]}
    return out


# ---------------------------------------------------------------- спільний прогін

def seeded_run(name, rate, count, w, label, poison=0, poison_at=0.0,
               sample_locks=False, pre_queue=False):
    """Серія: очищення черги, W воркерів, створення задач, очікування спорожнення.

    pre_queue: задачі створюються при нулі воркерів, щоб перед прогоном черга була повна.
    """
    clear_queue()
    if pre_queue:
        scale_worker(0)
    else:
        scale_worker(w)
    locks = LockSampler() if sample_locks else None
    if locks:
        locks.start()

    gen_start = time.time()
    gen = tasks.run(BASE, RESOURCE, IDS, rate, count, poison, poison_at,
                    out=str(OUT / f"{name}_gen"), label=label)
    gen_end = time.time()
    if pre_queue:
        queued = int(psql_one("SELECT count(*) FROM tasks WHERE status = 'queued'"))
        print(f"  у черзі перед прогоном: {queued}", flush=True)
        run_start = time.time()
        scale_worker(w)
    else:
        queued = None
        run_start = gen_start

    ok = drain()
    run_end = time.time()
    if locks:
        lock_res = locks.result()
    else:
        lock_res = None

    logs = worker_logs(since_s=run_end - run_start + 120)
    out = {
        "label": label, "workers": w, "rate": rate, "count": count,
        "pre_queued": queued, "drained": ok,
        "gen": {k: v for k, v in gen.items() if k != "rows"},
        "gen_rows": gen["rows"],
        "run_start_wall": run_start, "run_end_wall": run_end,
        "gen_start_wall": gen_start, "gen_end_wall": gen_end,
        "duration_s": run_end - run_start,
        "tasks": task_stats(),
        "wait": wait_stats(),
        "io_cpu": io_cpu_from_logs(logs),
        "log_duplicates": duplicates_from_logs(logs),
        "claim_p99": claim_p99(run_start, run_end),
        "claims": {
            "task": counter_delta('sum(worker_claims_total{result="task"})', run_start, run_end),
            "empty": counter_delta('sum(worker_claims_total{result="empty"})',
                                   run_start, run_end),
        },
        "db": db_metrics(run_start, run_end),
        "locks": lock_res,
        "pods": worker_pods(),
        "per_second_done": per_second_done(logs, run_start, run_end),
    }
    return out, logs


# ---------------------------------------------------------------- кроки

def per_second_done_db(start_wall, end_wall):
    """Посекундна кількість завершених задач за finished_at у базі.

    Надійніше за журнали: kubectl logs віддає лише поточний запуск контейнера, і після
    перезапуску (FAIL_RATE) рядки попередніх запусків зникають.
    """
    rows = psql_json(
        "SELECT floor(extract(epoch FROM finished_at) - "
        f"{start_wall:.3f})::int AS t_s, count(*)::int AS done FROM tasks "
        f"WHERE status = 'done' AND finished_at >= to_timestamp({start_wall:.3f}) "
        f"AND finished_at <= to_timestamp({end_wall:.3f}) GROUP BY 1 ORDER BY 1")
    per = {int(r["t_s"]): r["done"] for r in rows}
    return [{"t_s": t, "done": per.get(t, 0)}
            for t in range(int(end_wall - start_wall) + 1)]


def throughput_db(start_wall, end_wall, skip=30.0):
    """X за базою: завершені задачі за секунду в середині вікна."""
    per = per_second_done_db(start_wall + skip, end_wall - skip)
    vals = [r["done"] for r in per]
    return {"secs": len(vals), "window_wall": [start_wall + skip, end_wall - skip],
            "X_rps": statistics.fmean(vals) if vals else 0.0,
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0}


def queue_length_exact(win0, win1):
    """Точне середнє в часі число задач у статусі queued на вікні [win0; win1].

    Вимірювач tasks_queued сервіс оновлює раз на 5 секунд, тому за λ = 0,7·X(R) на вікні
    120 с виходить лише 25 цілих знімків зі значеннями 0, 1 або 2: стандартна похибка
    середнього сягає половини самого середнього. Той самий L_q рахується точно за
    позначками часу: задача лежить у черзі від created_at до started_at, і L_q є сумою
    перекриттів відрізків [created_at; started_at] з вікном, поділеною на довжину вікна.
    Це означення середнього в часі без будь-якої дискретизації.
    """
    sql = (
        "SELECT sum(LEAST(coalesce(extract(epoch FROM started_at), {w1:.3f}), {w1:.3f})"
        " - GREATEST(extract(epoch FROM created_at), {w0:.3f})) AS s,"
        " count(*)::int AS n FROM tasks"
        " WHERE created_at < to_timestamp({w1:.3f})"
        "   AND (started_at IS NULL OR started_at > to_timestamp({w0:.3f}))"
    ).format(w0=win0, w1=win1)
    rows = psql_json(sql)
    if not rows or rows[0]["s"] is None:
        return {"L_q_exact": 0.0, "tasks_overlapping": 0, "window_s": win1 - win0}
    return {"L_q_exact": float(rows[0]["s"]) / (win1 - win0),
            "tasks_overlapping": rows[0]["n"], "window_s": win1 - win0}


def cmd_step2(a):
    """20 задач на одному воркері: t_io і t_cpu."""
    reset_worker_env()
    set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                   HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS, LEASE_S=LEASE_S,
                   FAIL_RATE="0")
    res, logs = seeded_run("step2", rate=20, count=20, w=1, label="крок 2: 20 задач, W = 1")
    res["note"] = "t_io = крок 1 + крок 3 задачі, t_cpu = крок 2 (обробка зрізу)"
    save("step2", res)
    print(json.dumps(res["io_cpu"], indent=2, ensure_ascii=False))


def cmd_step3(a):
    """Наївне захоплення: D за T_poll варіанта і за 1 мс."""
    results_pk(False)
    out = {}
    for poll in (T_POLL_MS, 1):
        reset_worker_env()
        set_worker_env(CLAIM_MODE="naive", RESULT_MODE="insert", LEASE_MODE="off",
                       HEARTBEAT="off", WAIT_MODE="poll", POLL_MS=poll, FAIL_RATE="0")
        res, logs = seeded_run(f"step3_poll{poll}", rate=100, count=300, w=R,
                               label=f"крок 3: наївне захоплення, T_poll = {poll} мс")
        res["D_by_results"] = (res["tasks"]["tasks_with_many_results"]
                               / max(res["tasks"]["tasks_total"], 1))
        res["D_by_logs"] = (res["log_duplicates"]["tasks_done_more_than_once"]
                            / max(res["tasks"]["tasks_total"], 1))
        out[f"poll_{poll}ms"] = res
        save(f"step3_poll{poll}", res)
        print(f"T_poll = {poll} мс: D(результати) = {res['D_by_results']:.4f}, "
              f"D(журнали) = {res['D_by_logs']:.4f}", flush=True)
    save("step3", {k: {kk: vv for kk, vv in v.items()
                       if kk not in ("gen_rows", "per_second_done")} for k, v in out.items()})


def cmd_step4(a):
    """FOR UPDATE SKIP LOCKED, далі FOR UPDATE без SKIP LOCKED."""
    results_pk(True)
    out = {}
    pairs = [(m, k) for k in range(a.repeat) for m in ("skip_locked", "for_update")]
    for mode, k in pairs:
        tag = mode if a.repeat == 1 else f"{mode}_r{k + 1}"
        reset_worker_env()
        set_worker_env(CLAIM_MODE=mode, RESULT_MODE="upsert", LEASE_MODE="off",
                       HEARTBEAT="off", WAIT_MODE="poll", POLL_MS=1, FAIL_RATE="0")
        res, logs = seeded_run(f"step4_{tag}", rate=100, count=300, w=R,
                               label=f"крок 4: {mode}, T_poll = 1 мс, повтор {k + 1}",
                               sample_locks=True)
        res["D_by_results"] = (res["tasks"]["tasks_with_many_results"]
                               / max(res["tasks"]["tasks_total"], 1))
        res["D_by_logs"] = (res["log_duplicates"]["tasks_done_more_than_once"]
                            / max(res["tasks"]["tasks_total"], 1))
        res["mode"], res["repeat"] = mode, k + 1
        out[tag] = res
        save(f"step4_{tag}", res)
        print(f"{tag}: D = {res['D_by_results']:.4f}, порожніх захоплень "
              f"{res['claims']['empty']:.0f}, p99 захоплення "
              f"{res['claim_p99']}", flush=True)
    save("step4", {k: {kk: vv for kk, vv in v.items()
                       if kk not in ("gen_rows", "per_second_done")} for k, v in out.items()})
    reset_worker_env()


def pod_host_pid(pod):
    """PID контейнера воркера у просторі імен вузла kind."""
    node = kubectl("get", "pod", "-n", NS, pod, "-o", "jsonpath={.spec.nodeName}",
                   quiet=True).stdout.strip()
    cid = kubectl("get", "pod", "-n", NS, pod, "-o",
                  "jsonpath={.status.containerStatuses[?(@.name=='worker')].containerID}",
                  quiet=True).stdout.strip().replace("containerd://", "")
    r = subprocess.run(["docker", "exec", node, "crictl", "inspect", "--output",
                        "go-template", "--template", "{{.info.pid}}", cid],
                       check=True, capture_output=True, text=True)
    return node, int(r.stdout.strip())


def proc_state(pod):
    """Стан процесу воркера: S — спить, T — зупинений сигналом."""
    r = kubectl("exec", "-n", NS, pod, "-c", "worker", "--", "sh", "-c",
                "cut -d' ' -f3 /proc/1/stat", quiet=True, check=False)
    return r.stdout.strip()


def node_signal(node, hpid, sig):
    """Швидкий сигнал процесу воркера через простір імен вузла kind."""
    subprocess.run(["docker", "exec", node, "kill", f"-{sig}", str(hpid)], check=True,
                   capture_output=True, text=True)


def signal_worker(pod, sig):
    """Надсилає воркеру SIGSTOP або SIGCONT.

    Спершу командою з умови (kubectl exec ... -- kill -<sig> 1). Воркер є процесом 1
    у просторі імен контейнера, а ядро не доставляє init простору імен сигнал, надісланий
    зсередини цього ж простору, якщо для сигналу немає обробника; для SIGSTOP обробника
    не буває, тому команда не має дії. Тому, якщо стан процесу не змінився, той самий
    сигнал надсилається з простору імен вузла kind, де воркер є звичайним процесом.
    """
    kubectl("exec", "-n", NS, pod, "-c", "worker", "--", "sh", "-c", f"kill -{sig} 1",
            check=False)
    time.sleep(0.5)
    want = "T" if sig == "STOP" else "S"
    if sig == "CONT" or proc_state(pod) != want:
        node, hpid = pod_host_pid(pod)
        print(f"$ docker exec {node} kill -{sig} {hpid}   (через простір імен вузла)",
              flush=True)
        subprocess.run(["docker", "exec", node, "kill", f"-{sig}", str(hpid)], check=True)
        return {"signal": sig, "via": "node", "node": node, "host_pid": hpid}
    return {"signal": sig, "via": "kubectl exec"}


def cmd_step5(a):
    """Lease, heartbeat і fencing token: зупинка воркера на 2·L сигналом SIGSTOP."""
    results_pk(True)
    reset_worker_env()
    set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                   HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS, LEASE_S=LEASE_S,
                   FAIL_RATE="0")
    clear_queue()
    # Черга має бути непорожньою, інакше воркер на момент зупинки простоює між задачами:
    # задачі створюються при нулі воркерів, і лише потім піднімаються два.
    scale_worker(0)
    gen = tasks.run(BASE, RESOURCE, IDS, a.rate, a.count, out=str(OUT / "step5_gen"),
                    label=f"крок 5: {a.count} задач, W = 2")
    run_start = time.time()
    scale_worker(2)
    pods = sorted(p["pod"] for p in wait_worker_pods(2))
    target = pods[0]

    # Сигнал має прилітати швидко: адресу процесу у просторі імен вузла визначаємо
    # заздалегідь, інакше за час kubectl exec воркер уже переходить до наступної задачі.
    node, hpid = pod_host_pid(target)
    documented = kubectl("exec", "-n", NS, target, "-c", "worker", "--", "sh", "-c",
                         "kill -STOP 1", check=False)
    documented_worked = proc_state(target) == "T"
    if documented_worked:
        signal_worker(target, "CONT")
    print(f"  команда з умови (kubectl exec -- kill -STOP 1) "
          f"{'діє' if documented_worked else 'не діє'}; воркер {target} "
          f"= процес {hpid} на вузлі {node}", flush=True)

    # 1. зупиняємо воркер у момент, коли за ним є задача в обробці

    held, stop_at, state, refills = [], None, None, 0
    for attempt in range(a.attempts):
        if int(psql_one("SELECT count(*) FROM tasks WHERE status = 'queued'")) == 0:
            # черга спорожніла раніше, ніж вдалось застати задачу в обробці:
            # досипаємо ще партію, щоб воркери лишались зайняті
            refills += 1
            tasks.run(BASE, RESOURCE, IDS, a.rate, a.count,
                      out=str(OUT / f"step5_gen_refill{refills}"))
        stop_at = time.time()
        node_signal(node, hpid, "STOP")
        held = psql_json("SELECT id, attempts, token, worker_id, status, "
                         "extract(epoch FROM lease_until) AS lease_until_epoch "
                         f"FROM tasks WHERE status = 'running' AND worker_id = '{target}'")
        state = proc_state(target)
        if held:
            print(f"  спроба {attempt + 1}: зупинено {target} (стан процесу {state}), "
                  f"за ним задача {held[0]['id']}", flush=True)
            break
        print(f"  спроба {attempt + 1}: {target} зупинений між задачами, відновлюю",
              flush=True)
        node_signal(node, hpid, "CONT")
    if not held:
        raise RuntimeError("не вдалось застати воркер із задачею в обробці")

    # 2. чекаємо 2·L: lease спливає, сервіс web повертає задачу в чергу,
    #    інший воркер її забирає і збільшує token
    time.sleep(2 * LEASE_S)
    ids = [r["id"] for r in held]
    id_list = ",".join(str(i) for i in ids)
    snap = ("SELECT t.id, t.status, t.attempts, t.token, t.worker_id, "
            "(SELECT count(*)::int FROM task_results r WHERE r.task_id = t.id) AS results, "
            "(SELECT r.worker_id FROM task_results r WHERE r.task_id = t.id) AS result_worker "
            f"FROM tasks t WHERE t.id IN ({id_list})")
    after = psql_json(snap)

    # 3. відновлюємо воркер: його команди зі застарілим токеном оновлять нуль рядків
    cont_at = time.time()
    node_signal(node, hpid, "CONT")
    cont_how = {"signal": "CONT", "via": "node", "node": node, "host_pid": hpid}
    time.sleep(a.after)
    drain(timeout=600)
    run_end = time.time()
    final = psql_json(snap)
    logs = worker_logs(since_s=run_end - run_start + 120)
    zero_rows = [r for r in logs if "нуль оновлених рядків" in (r.get("msg") or "")]
    res = {
        "label": "крок 5: lease, heartbeat і fencing token",
        "lease_s": LEASE_S, "workers": 2, "pods": pods, "stopped_pod": target,
        "rate": a.rate, "count": a.count,
        "gen": {k: v for k, v in gen.items() if k != "rows"},
        "run_start_wall": run_start, "stop_at_wall": stop_at, "cont_at_wall": cont_at,
        "run_end_wall": run_end, "stopped_for_s": cont_at - stop_at,
        "documented_command_works": documented_worked,
        "documented_command_output": (documented.stderr or documented.stdout).strip()[:300],
        "stop_via": {"node": node, "host_pid": hpid}, "cont_how": cont_how,
        "proc_state_after_stop": state, "queue_refills": refills,
        "held_at_stop": held,
        "after_lease_expiry": after,
        "final": final,
        "zero_rows_log_lines": zero_rows,
        "zero_rows_from_stopped_pod": [r for r in zero_rows if r.get("pod") == target],
        "tasks": task_stats(),
        "log_duplicates": duplicates_from_logs(logs),
        "lease_requeued_total": pq("sum(tasks_lease_requeued_total)", run_end, 0.0),
        "stopped_pod_log": [r for r in logs if r.get("pod") == target][-40:],
    }
    save("step5", res)
    print(json.dumps({k: res[k] for k in ("held_at_stop", "after_lease_expiry", "final",
                                          "zero_rows_from_stopped_pod")},
                     indent=2, ensure_ascii=False, default=str))


def cmd_step6(a):
    """X(W) для W = 1, 2 і R з непорожньою чергою."""
    results_pk(True)
    out = {"runs": {}, "pre_queue": a.count}
    wlist = [int(x) for x in a.workers.split(",")] if a.workers else [1, 2, R]
    for w in wlist:
        reset_worker_env()
        set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                       HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS, LEASE_S=LEASE_S,
                       FAIL_RATE="0")
        name = f"step6{a.tag}_W{w}"
        res, logs = seeded_run(name, rate=200, count=a.count, w=w,
                               label=f"крок 6: W = {w}, {a.count} задач у черзі",
                               pre_queue=True)
        res["X"] = throughput_window(res, a.window)
        out["runs"][str(w)] = res
        save(name, res)
        print(f"W = {w}: X = {res['X']['X_rps']:.2f} ±{res['X']['sd']:.2f} "
              f"(вікно {res['X']['window_s']} с)", flush=True)
    save(f"step6{a.tag}", {"pre_queue": a.count, "window_s": a.window,
                   "runs": {k: {kk: vv for kk, vv in v.items() if kk != "gen_rows"}
                            for k, v in out["runs"].items()}})


def throughput_window(res, window_s=30.0):
    """X(W) за останні window_s секунд до спорожнення черги."""
    per = res["per_second_done"]
    nz = [r for r in per if r["done"] > 0]
    if not nz:
        return {"X_rps": 0.0, "sd": 0.0, "window_s": 0, "secs": 0}
    # Остання секунда неповна: черга спорожнюється десь усередині неї, і воркери
    # частину секунди вже простоюють. Якщо її врахувати, X занижується на 0,4-5 %.
    last = nz[-1]["t_s"] - 1
    first = nz[0]["t_s"]
    if last <= first:
        last = nz[-1]["t_s"]
    # вікно не може бути довшим за сам прогін: при великій кількості воркерів черга
    # спорожнюється швидше, ніж за 30 с
    begin = max(first + 1, last - int(window_s) + 1)
    vals = [r["done"] for r in per if begin <= r["t_s"] <= last]
    return {"X_rps": statistics.fmean(vals) if vals else 0.0,
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "window_s": last - begin + 1, "window": [begin, last], "secs": len(vals),
            "drain_s": nz[-1]["t_s"] - first + 1,
            "requested_window_s": int(window_s),
            "dropped_last_partial_s": nz[-1]["t_s"]}


def cmd_step7(a):
    """Сталий режим: закон Літтла для черги."""
    results_pk(True)
    out = {}
    want = a.variants.split(",")
    for tag, factor, dur in (("low", 0.7, a.duration_low), ("high", 1.3, a.duration_high)):
        if tag not in want:
            continue
        rate = round(factor * a.xr, 2)
        count = int(round(rate * dur))
        reset_worker_env()
        set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                       HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS, LEASE_S=LEASE_S,
                       FAIL_RATE="0")
        clear_queue()
        scale_worker(R)
        gen_start = time.time()
        gen = tasks.run(BASE, RESOURCE, IDS, rate, count,
                        out=str(OUT / f"step7_{tag}_gen"),
                        label=f"крок 7: λ = {factor}·X(R) = {rate} задач/с, {dur} с")
        gen_end = time.time()
        # вікно аналізу: останні window_s секунд прогону генератора
        win0 = gen_end - a.window
        win1 = gen_end
        created = psql_json(
            "SELECT count(*)::int AS n FROM tasks WHERE created_at >= to_timestamp("
            f"{win0}) AND created_at <= to_timestamp({win1})")
        lam = (created[0]["n"] / (win1 - win0)) if created else 0.0
        lq_series = prom.query_range(PROM, "max(tasks_queued)", win0, win1, 5.0)
        lq_vals = [v for _, vals in lq_series for _, v in vals]
        lq_exact = queue_length_exact(win0, win1)
        wq = wait_stats(
            f"created_at >= to_timestamp({win0}) AND created_at <= to_timestamp({win1})")
        res = {
            "label": f"{factor}·X(R)", "factor": factor, "rate_target": rate,
            "duration_s": dur, "count": count,
            "gen": {k: v for k, v in gen.items() if k != "rows"},
            "gen_rows": gen["rows"],
            "window_wall": [win0, win1], "window_s": a.window,
            "lambda_measured": lam,
            "L_q_mean": statistics.fmean(lq_vals) if lq_vals else None,
            "L_q_samples": len(lq_vals), "L_q_series": lq_vals,
            "L_q_sd": statistics.stdev(lq_vals) if len(lq_vals) > 1 else None,
            "L_q_stderr": (statistics.stdev(lq_vals) / (len(lq_vals) ** 0.5)
                           if len(lq_vals) > 1 else None),
            "L_q_exact": lq_exact,
            "W_q": wq,
            "little_lambda_Wq": lam * wq["mean_s"] if wq.get("mean_s") else None,
            "queue_at_end": int(psql_one("SELECT count(*) FROM tasks WHERE status='queued'")),
            "running_at_end": int(psql_one("SELECT count(*) FROM tasks WHERE status='running'")),
            "tasks": task_stats(),
        }
        if res["little_lambda_Wq"] and res["L_q_mean"]:
            res["discrepancy_pct"] = 100.0 * (res["little_lambda_Wq"] - res["L_q_mean"]) \
                / res["L_q_mean"]
        if res["little_lambda_Wq"] and lq_exact["L_q_exact"]:
            res["discrepancy_exact_pct"] = (
                100.0 * (res["little_lambda_Wq"] - lq_exact["L_q_exact"])
                / lq_exact["L_q_exact"])
        # для 1,3·X(R): W_q за першими і останніми 100 задачами
        # Останні 100 — серед тих, що встигли бути захопленими: за 1,3·X(R) хвіст черги
        # так і лишається у queued, і для нього W_q не визначений. Їхню кількість
        # окремо дає queue_at_end.
        ids_first = psql_json("SELECT id FROM tasks WHERE started_at IS NOT NULL "
                              "ORDER BY created_at, id LIMIT 100")
        ids_last = psql_json("SELECT id FROM (SELECT id FROM tasks "
                             "WHERE started_at IS NOT NULL "
                             "ORDER BY created_at DESC, id DESC LIMIT 100) s")
        res["never_started"] = int(psql_one(
            "SELECT count(*) FROM tasks WHERE started_at IS NULL"))
        if ids_first:
            res["W_q_first100"] = wait_stats(
                "id IN (" + ",".join(str(r["id"]) for r in ids_first) + ")")
        if ids_last:
            res["W_q_last100"] = wait_stats(
                "id IN (" + ",".join(str(r["id"]) for r in ids_last) + ")")
        out[tag] = res
        save(f"step7_{tag}", res)
        print(f"{tag}: λ = {lam:.2f}, L_q = {res['L_q_mean']}, W_q = {wq.get('mean_s')}, "
              f"λ·W_q = {res['little_lambda_Wq']}", flush=True)
        drain(timeout=900)
    merged = {}
    old = OUT / "step7.json"
    if old.exists():
        merged = json.loads(old.read_text(encoding="utf-8"))
    merged.update({k: {kk: vv for kk, vv in v.items() if kk != "gen_rows"}
                   for k, v in out.items()})
    save("step7", merged)


def cmd_step8(a):
    """Коректна зупинка: rollout restart під навантаженням."""
    results_pk(True)
    rate = round(0.7 * a.xr, 2)
    dur = a.duration
    count = int(round(rate * dur))
    reset_worker_env()
    set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                   HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS, LEASE_S=LEASE_S,
                   FAIL_RATE="0")
    clear_queue()
    scale_worker(R)
    before = worker_pods()
    watcher = PodWatcher()
    watcher.start()
    keeper = PodLogKeeper([p["pod"] for p in before])
    keeper.start()
    run_start = time.time()
    t = threading.Thread(target=tasks.run, daemon=True,
                         args=(BASE, RESOURCE, IDS, rate, count, 0, 0.0,
                               str(OUT / "step8_gen"), None, "крок 8: 0,7·X(R)"))
    t.start()
    time.sleep(dur * 0.35)
    restart_at = time.time()
    kubectl("rollout", "restart", "deployment/worker", "-n", NS)
    kubectl("rollout", "status", "deployment/worker", "-n", NS, "--timeout=300s",
            capture=False)
    t.join(timeout=dur + 60)
    drain(timeout=600)
    run_end = time.time()
    exits = watcher.result()
    old_logs = keeper.result()
    logs = worker_logs(since_s=run_end - run_start + 120) + old_logs
    requeued = [r for r in logs if r.get("msg") == "зупинка: задача повернута в чергу"]
    sigterms = [r for r in logs if r.get("msg", "").startswith("SIGTERM")]
    stopped = [r for r in logs if r.get("msg") == "зупинка завершена"]
    res = {
        "label": "крок 8: коректна зупинка під прогоном", "rate": rate,
        "duration_s": dur, "count": count,
        "run_start_wall": run_start, "restart_at_wall": restart_at, "run_end_wall": run_end,
        "pods_before": before, "pods_after": worker_pods(),
        "exit_codes": exits,
        "exit_codes_old_pods": [e for e in exits
                                if e["pod"] in {p["pod"] for p in before}],
        "sigterm_lines": sigterms, "graceful_exit_lines": stopped,
        "old_pod_log_lines": len(old_logs),
        "requeued_by_workers": len(requeued),
        "requeued_records": requeued,
        "tasks": task_stats(),
        "wait": wait_stats(),
        "log_duplicates": duplicates_from_logs(logs),
        "per_second_done": per_second_done(logs, run_start, run_end),
    }
    save("step8", res)
    print(json.dumps({k: res[k] for k in ("exit_codes", "requeued_by_workers")},
                     indent=2, ensure_ascii=False))
    print(json.dumps(res["tasks"]["by_status"], ensure_ascii=False))


def terminated_exit_codes(pods_before):
    """Коди завершення контейнерів worker: з подій і зі стану подів."""
    names = {p["pod"] for p in pods_before}
    out = []
    r = kubectl("get", "pods", "-n", NS, "-l", "app=worker", "-o", "json", quiet=True,
                check=False)
    try:
        items = json.loads(r.stdout).get("items", [])
    except Exception:
        items = []
    for it in items:
        name = it["metadata"]["name"]
        for cs in it.get("status", {}).get("containerStatuses", []):
            term = (cs.get("state", {}).get("terminated")
                    or cs.get("lastState", {}).get("terminated"))
            if term:
                out.append({"pod": name, "old": name in names,
                            "exit_code": term.get("exitCode"),
                            "reason": term.get("reason")})
    return out


def cmd_step9(a):
    """FAIL_RATE = 0,15: прогін за умовою і прогін із наглядачем процесу воркера.

    Варіанти:
      k8s     — Deployment як є, падіння лікує kubelet (спостереження про CrashLoopBackOff)
      poison  — наглядач процесу + сценарій Ф3 (10 задач із seed = 0)
      clean   — наглядач процесу, той самий FAIL_RATE, без отруєних задач
    """
    results_pk(True)
    rate = a.rate if a.rate else round(0.7 * a.xr, 2)
    dur = a.duration
    count = int(round(rate * dur))
    want = a.variants.split(",")
    out = {}

    for tag in want:
        tag = tag + a.suffix
        supervise = "0" if tag.startswith("k8s") else "1"
        poison = a.poison if tag.startswith("poison") else 0
        reset_worker_env()
        set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                       HEARTBEAT="on", WAIT_MODE="poll", POLL_MS=T_POLL_MS,
                       LEASE_S=LEASE_S, FAIL_RATE="0.15", SUPERVISE=supervise)
        clear_queue()
        scale_worker(R)
        before = worker_pods()
        watcher = PodWatcher(period=1.0)
        watcher.start()

        run_start = time.time()
        gen = tasks.run(BASE, RESOURCE, IDS, rate, count, poison, dur / 2,
                        out=str(OUT / f"step9_{tag}_gen"),
                        label=f"крок 9 ({tag}): FAIL_RATE = 0,15, λ = {rate} задач/с")
        gen_end = time.time()
        # Прогін триває 300 с за умовою. Чекати спорожнення черги не можна: у варіанті
        # k8s пул стоїть у CrashLoopBackOff, і черга не спорожніє ніколи.
        drained = drain(timeout=a.drain)
        run_end = time.time()
        exits = watcher.result()
        logs = worker_logs(since_s=run_end - run_start + 120)
        after = worker_pods()

        res = {
            "label": f"крок 9, варіант {tag}", "variant": tag, "supervise": supervise == "1",
            "fail_rate": 0.15, "workers": R, "rate": rate, "duration_s": dur,
            "count": count, "poison_count": poison,
            "run_start_wall": run_start, "gen_end_wall": gen_end, "run_end_wall": run_end,
            "poison_at_wall": run_start + dur / 2 if poison else None,
            "drained": drained,
            "gen": {k: v for k, v in gen.items() if k != "rows"},
            "gen_rows": gen["rows"],
            "pods_before": before, "pods_after": after,
            "restarts_delta": sum(p["restarts"] for p in after)
                              - sum(p["restarts"] for p in before),
            "restarts_after": sum(p["restarts"] for p in after),
            "container_exits": exits,
            "crashloop": [e for e in exits if e.get("reason") == "Error"],
            "supervisor_restarts": len([r for r in logs
                                        if r.get("msg", "").startswith("наглядач: воркер впав")]),
            # У варіанті k8s рядки «вбудоване падіння» попередніх запусків контейнера
            # недоступні, тому падіння рахуються і за перезапусками контейнерів.
            "builtin_exits": len([r for r in logs if r.get("msg") == "вбудоване падіння"]),
            "tasks": task_stats(),
            "wait": wait_stats(),
            "wait_without_poison": wait_stats("seed <> 0"),
            "poison_tasks": psql_json(
                "SELECT id, status, attempts, token, "
                "extract(epoch FROM (finished_at - created_at)) AS to_failed_s "
                "FROM tasks WHERE seed = 0 ORDER BY id"),
            "log_duplicates": duplicates_from_logs(logs),
            "claim_p99": claim_p99(run_start, gen_end),
            "db": db_metrics(run_start, gen_end),
            "per_second_done": per_second_done(logs, run_start, run_end),
            "pod_states": kubectl("get", "pods", "-n", NS, "-l", "app=worker",
                                  "-o", "wide", quiet=True, check=False).stdout,
        }
        # X у цьому прогоні: за базою, у середині вікна генератора (без перших і
        # останніх 30 с). За журналами його не взяти: після перезапуску контейнера
        # kubectl logs уже не віддає рядки попереднього запуску.
        res["X_during_run"] = throughput_db(run_start, gen_end, 30.0)
        res["per_second_done_db"] = per_second_done_db(run_start, run_end)
        res["done_total"] = res["tasks"]["by_status"].get("done", 0)
        res["X_overall"] = res["done_total"] / max(gen_end - run_start, 1)
        res["prom"] = prom.dump(
            PROM, run_start - 10, run_end + 10, 5.0,
            {"tasks_queued": "max(tasks_queued)", "tasks_running": "max(tasks_running)",
             "done_rps": 'sum(rate(worker_tasks_total{result="done"}[30s]))',
             "failed_rps": 'sum(rate(worker_tasks_total{result="failed"}[30s]))',
             "in_flight": "sum(worker_tasks_in_flight)",
             "ready_workers": 'count(up{namespace="donors",job="worker"} == 1)'})
        out[tag] = res
        save(f"step9_{tag}", res)
        print(f"{tag}: перезапусків контейнерів {res['restarts_delta']}, "
              f"перезапусків наглядачем {res['supervisor_restarts']}, падінь "
              f"{res['builtin_exits']}, X = {res['X_during_run']['X_rps']:.2f}, "
              f"статуси {res['tasks']['by_status']}", flush=True)

    merged = {}
    old = OUT / "step9.json"
    if old.exists():
        merged = json.loads(old.read_text(encoding="utf-8"))
    merged.update({k: {kk: vv for kk, vv in v.items()
                       if kk not in ("gen_rows", "prom", "per_second_done",
                                     "per_second_done_db")}
                   for k, v in out.items()})
    save("step9", merged)
    reset_worker_env()


def cmd_step10(a):
    """Вимога Д5: LISTEN/NOTIFY замість опитування."""
    results_pk(True)
    out = {}
    for tag, wait_mode, poll in (("poll100", "poll", 100), ("listen", "listen", 100)):
        reset_worker_env()
        set_worker_env(CLAIM_MODE="skip_locked", RESULT_MODE="upsert", LEASE_MODE="on",
                       HEARTBEAT="on", WAIT_MODE=wait_mode, POLL_MS=poll, LEASE_S=LEASE_S,
                       FAIL_RATE="0")
        clear_queue()
        scale_worker(R)
        # запити до бази за порожньої черги: 20 с без задач
        idle0 = time.time()
        time.sleep(a.idle)
        idle1 = time.time()
        idle_q = counter_delta('sum(worker_db_queries_total)', idle0 + 5, idle1)
        idle_claims = counter_delta('sum(worker_claims_total{result="empty"})',
                                    idle0 + 5, idle1)

        count = int(round(a.rate * a.duration))
        res, logs = seeded_run(f"step10_{tag}", rate=a.rate, count=count, w=R,
                               label=f"крок 10 (Д5): {wait_mode}, λ = {a.rate} задач/с")
        res["idle"] = {
            "window_s": idle1 - idle0 - 5,
            "db_queries": idle_q,
            "db_queries_per_s": idle_q / max(idle1 - idle0 - 5, 1),
            "empty_claims": idle_claims,
            "empty_claims_per_s": idle_claims / max(idle1 - idle0 - 5, 1),
        }
        res["wait_mode"] = wait_mode
        res["poll_ms"] = poll
        out[tag] = res
        save(f"step10_{tag}", res)
        print(f"{tag}: W_q p50 = {res['wait']['p50_s']:.4f} с, p99 = "
              f"{res['wait']['p99_s']:.4f} с, запитів до бази за порожньої черги "
              f"{res['idle']['db_queries_per_s']:.1f}/с", flush=True)
    save("step10", {k: {kk: vv for kk, vv in v.items() if kk != "gen_rows"}
                    for k, v in out.items()})
    reset_worker_env()


def cmd_recompute_x(a):
    """Перераховує X(W) для збережених прогонів кроку 6 із їхнього посекундного ряду."""
    for name in a.names.split(","):
        p = OUT / f"{name}.json"
        if not p.exists():
            print(f"немає {p.name}", flush=True)
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        old = dict(d.get("X") or {})
        d["X"] = throughput_window(d, a.window)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
        print(f"{name}: X {old.get('X_rps', 0):.2f} -> {d['X']['X_rps']:.2f} "
              f"(розкид {d['X']['sd']:.2f}, вікно {d['X']['window_s']} с)", flush=True)


def cmd_recompute(a):
    """Перераховує метрики Prometheus для вже збережених прогонів (дані ще в Prometheus)."""
    for name in a.names.split(","):
        p = OUT / f"{name}.json"
        if not p.exists():
            print(f"немає {p.name}", flush=True)
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        start = d.get("run_start_wall")
        end = d.get("run_end_wall")
        if not start or not end or end <= start:
            print(f"{name}: немає коректного вікна прогону", flush=True)
            continue
        d["claim_p99"] = claim_p99(start, end)
        d["claim_p50"] = step_quantile("claim", start, end, 0.5)
        d["db"] = db_metrics(start, end)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str),
                     encoding="utf-8")
        print(f"{name}: p99 захоплення {d['claim_p99']}", flush=True)


def cmd_reset(a):
    reset_worker_env()
    results_pk(True)
    clear_queue()
    scale_worker(a.workers)


COMMANDS = {
    "step2": cmd_step2, "step3": cmd_step3, "step4": cmd_step4, "step5": cmd_step5,
    "step6": cmd_step6, "step7": cmd_step7, "step8": cmd_step8,
    "step9": cmd_step9, "step10": cmd_step10, "reset": cmd_reset,
    "recompute": cmd_recompute, "recompute_x": cmd_recompute_x,
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        if name == "step4":
            p.add_argument("--repeat", type=int, default=1,
                           help="скільки разів повторити кожен режим")
        if name == "step5":
            p.add_argument("--rate", type=float, default=100.0)
            p.add_argument("--count", type=int, default=100)
            p.add_argument("--attempts", type=int, default=25,
                           help="скільки разів пробувати застати задачу в обробці")
            p.add_argument("--after", type=float, default=10.0,
                           help="скільки секунд спостерігати після kill -CONT")
        if name == "step6":
            p.add_argument("--count", type=int, default=400)
            p.add_argument("--window", type=float, default=30.0)
            p.add_argument("--workers", default="", help="перелік W, напр. 2,4")
            p.add_argument("--tag", default="", help="суфікс назви файлів результату")
        if name == "step7":
            p.add_argument("--xr", type=float, required=True)
            p.add_argument("--duration-low", type=float, default=180.0)
            p.add_argument("--duration-high", type=float, default=120.0)
            p.add_argument("--window", type=float, default=120.0)
            p.add_argument("--variants", default="low,high")
        if name in ("step8", "step9"):
            p.add_argument("--xr", type=float, required=True)
            p.add_argument("--duration", type=float,
                           default=120.0 if name == "step8" else 300.0)
        if name == "step9":
            p.add_argument("--poison", type=int, default=10)
            p.add_argument("--variants", default="k8s,poison,clean")
            p.add_argument("--rate", type=float, default=None,
                           help="абсолютна інтенсивність замість 0,7·X(R)")
            p.add_argument("--suffix", default="",
                           help="суфікс назви варіанта у файлах результату")
            p.add_argument("--drain", type=float, default=120.0,
                           help="скільки щонайбільше чекати спорожнення після прогону")
        if name == "step10":
            p.add_argument("--rate", type=float, default=5.0)
            p.add_argument("--duration", type=float, default=60.0)
            p.add_argument("--idle", type=float, default=25.0)
        if name == "recompute_x":
            p.add_argument("--names", required=True,
                           help="назви файлів без .json через кому")
            p.add_argument("--window", type=float, default=30.0)
        if name == "recompute":
            p.add_argument("--names", required=True,
                           help="назви файлів без .json через кому")
        if name == "reset":
            p.add_argument("--workers", type=int, default=1)
    a = ap.parse_args()
    COMMANDS[a.cmd](a)
