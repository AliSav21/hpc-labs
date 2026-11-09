"""Наглядач воркера для прогонів з FAIL_RATE (крок 9 ЛР4).

Чому він потрібен. Вбудоване падіння за умовою реалізується os._exit(1), і контейнер
перезапускає Kubernetes. На інтенсивності 0,7·X(R) ≈ 20 задач/с і FAIL_RATE = 0,15
падіння трапляється приблизно тричі за секунду на пул, а kubelet збільшує паузу перед
перезапуском контейнера (CrashLoopBackOff) до 5 хвилин. Пул перестає працювати не через
повторну обробку задач, а через цю паузу, і розподіл задач за attempts зняти вже ні з
чого. Прогін із самим Deployment наведений у звіті окремо саме як це спостереження.

Наглядач лишає механізм падіння незмінним — процес воркера зникає посеред задачі, не
закриваючи зʼєднань, задача лишається в running до спливання lease і повертається в
чергу — але вартість повернення воркера стає рівною запуску процесу, а не паузі kubelet.
Кількість перезапусків він рахує сам і віддає у метриці worker_supervisor_restarts_total
через окремий порт.

SIGTERM передається дочірньому процесу, і наглядач чекає його коректного завершення,
тому перевірка кроку 8 від цього не змінюється.
"""
import sys as _sys

_sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

RESTARTS = 0
STARTED_AT = time.time()
PORT = int(os.environ.get("SUPERVISOR_PORT", "9101"))
CHILD = None
STOPPING = threading.Event()


def log(msg, **kw):
    rec = {"ts": round(time.time(), 3), "worker": os.environ.get("WORKER_ID", "?"),
           "msg": msg, "supervisor": True}
    rec.update(kw)
    print(json.dumps(rec, ensure_ascii=False), flush=True)


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            "# HELP worker_supervisor_restarts_total Перезапуски процесу воркера наглядачем\n"
            "# TYPE worker_supervisor_restarts_total counter\n"
            f"worker_supervisor_restarts_total {RESTARTS}\n"
            "# HELP worker_supervisor_uptime_seconds Час роботи наглядача\n"
            "# TYPE worker_supervisor_uptime_seconds gauge\n"
            f"worker_supervisor_uptime_seconds {time.time() - STARTED_AT:.3f}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def on_sigterm(signum, _frame):
    STOPPING.set()
    log("наглядач: SIGTERM, передаю воркеру", signal=signum)
    if CHILD is not None and CHILD.poll() is None:
        try:
            CHILD.send_signal(signal.SIGTERM)
        except Exception:
            pass


def main():
    global CHILD, RESTARTS
    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)
    threading.Thread(target=HTTPServer(("", PORT), Metrics).serve_forever,
                     daemon=True).start()
    log("наглядач: старт", port=PORT)
    while not STOPPING.is_set():
        CHILD = subprocess.Popen([sys.executable, "-m", "worker.main"])
        code = CHILD.wait()
        if STOPPING.is_set():
            log("наглядач: воркер завершився після SIGTERM", exit_code=code)
            return 0 if code == 0 else code
        RESTARTS += 1
        log("наглядач: воркер впав, запускаю заново", exit_code=code, restarts=RESTARTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
