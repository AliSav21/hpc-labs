"""Доступ до Prometheus і оцінка квантиля за гістограмою. Лише стандартна бібліотека."""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 30.0


def default_url():
    """Адреса Prometheus з .env."""
    env = Path(__file__).resolve().parent.parent / ".env"
    port = "19090"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PROM_PORT="):
                port = line.split("=", 1)[1].strip()
    return "http://127.0.0.1:" + port


def k8s_url():
    """Адреса Prometheus у кластері kind (ЛР3, ЛР4): порт хоста 9090 з умови, у .env —
    K8S_PROM_PORT. Окремо від PROM_PORT стенду Compose (ЛР1, ЛР2)."""
    env = Path(__file__).resolve().parent.parent / ".env"
    port = "9090"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("K8S_PROM_PORT="):
                port = line.split("=", 1)[1].strip()
    return "http://127.0.0.1:" + port


def api(url, path, **params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}/api/v1/{path}?{q}", timeout=TIMEOUT) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(body)
    return body["data"]


def query(url, expr, at):
    """Миттєвий запит у момент at (unix-час). Повертає [(labels, value)]."""
    data = api(url, "query", query=expr, time=f"{at:.3f}")
    return [(r["metric"], float(r["value"][1])) for r in data["result"]]


def query_range(url, expr, start, end, step=5.0):
    """Запит на відрізку. Повертає [(labels, [(t, value)])]."""
    data = api(url, "query_range", query=expr, start=f"{start:.3f}", end=f"{end:.3f}",
               step=f"{step:g}")
    return [(r["metric"], [(float(t), float(v)) for t, v in r["values"]])
            for r in data["result"]]


def scalar(url, expr, at, default=None):
    """Єдине значення миттєвого запиту; default, якщо ряду немає."""
    res = query(url, expr, at)
    return res[0][1] if res else default


# ---------------------------------------------------------------- квантиль за гістограмою

def buckets_at(url, metric, at, grouping=""):
    """Накопичені лічильники бакетів, просумовані по всіх екземплярах: {le: c}."""
    expr = f'sum by (le) ({metric}_bucket{grouping})'
    return {m["le"]: v for m, v in query(url, expr, at)}


def bucket_increments(url, metric, start, end, grouping=""):
    """Приріст лічильників бакетів за відрізок: [(межа або inf, приріст)] за зростанням."""
    a = buckets_at(url, metric, start, grouping)
    b = buckets_at(url, metric, end, grouping)
    out = []
    for le in set(a) | set(b):
        out.append((float(le), b.get(le, 0.0) - a.get(le, 0.0)))
    out.sort(key=lambda x: x[0])
    return out


def quantile_from_buckets(cum, q=0.99):
    """Оцінка квантиля за формулою з умови задачі.

    cum: [(верхня межа, накопичена кількість)] за зростанням, остання межа inf.
    Повертає dict з оцінкою, межами бакета, похибкою і ознакою виходу за останню межу.
    """
    cum = [(b, c) for b, c in cum]
    if not cum:
        return None
    n = cum[-1][1]
    if n <= 0:
        return None
    r = q * n
    finite = [(b, c) for b, c in cum if b != float("inf")]
    b_prev, c_prev = 0.0, 0.0
    for b_i, c_i in finite:
        if c_prev < r <= c_i:
            width = b_i - b_prev
            est = b_prev + width * (r - c_prev) / (c_i - c_prev) if c_i > c_prev else b_i
            return {"q": q, "n": n, "rank": r, "value_s": est,
                    "bucket": [b_prev, b_i], "max_error_s": width, "beyond_last": False}
        b_prev, c_prev = b_i, c_i
    b_k = finite[-1][0] if finite else 0.0
    # ранг за останньою скінченною межею: оцінка це b(k), похибка зверху не обмежена
    return {"q": q, "n": n, "rank": r, "value_s": b_k,
            "bucket": [b_k, float("inf")], "max_error_s": None, "beyond_last": True,
            "label": f"> {b_k}"}


def server_quantile(url, metric, start, end, q=0.99, grouping=""):
    """p-квантиль сервера на відрізку за приростами бакетів усіх екземплярів."""
    return quantile_from_buckets(bucket_increments(url, metric, start, end, grouping), q)


# ---------------------------------------------------------------- вивантаження рядів

SERIES = {
    # на екземпляр
    "rps": 'sum by (instance) (rate(http_requests_total{code=~"2.."}[15s]))',
    "cpu": 'rate(process_cpu_seconds_total{job="web"}[15s])',
    "in_flight": 'http_requests_in_flight{job="web"}',
    "pool_busy": 'db_pool_connections_busy{job="web"}',
    "pool_size": 'db_pool_size{job="web"}',
    # сумарно по стенду
    "rps_total": 'sum(rate(http_requests_total[15s]))',
    "rps_2xx_total": 'sum(rate(http_requests_total{code=~"2.."}[15s]))',
    "cpu_total": 'sum(rate(process_cpu_seconds_total[15s]))',
    "in_flight_total": 'sum(http_requests_in_flight)',
    "pool_busy_total": 'sum(db_pool_connections_busy)',
    # накопичені лічильники бакетів: за ними діаграми рахують p99 сервера на вікні 15 с
    "dur_buckets": 'sum by (le) (http_request_duration_seconds_bucket)',
    "wait_buckets": 'sum by (le) (db_pool_wait_seconds_bucket)',
}


def dump(url, start, end, step=5.0, exprs=None):
    """Вивантажує іменовані ряди на відрізку в структуру для діаграм."""
    exprs = exprs or SERIES
    out = {"url": url, "start": start, "end": end, "step": step, "series": {}}
    for name, expr in exprs.items():
        out["series"][name] = [
            {"labels": m, "values": vals} for m, vals in query_range(url, expr, start, end, step)
        ]
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default=default_url())
    ap.add_argument("--start", type=float, required=True, help="unix-час початку")
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = dump(a.url, a.start, a.end, a.step)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"{sum(len(v) for v in data['series'].values())} рядів -> {a.out}")
