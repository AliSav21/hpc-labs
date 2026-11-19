"""Прогін за профілем навантаження в кластері: кроки 10 і 11 ЛР3.
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import argparse
import json
import subprocess
import time
from pathlib import Path

from loadgen import profile, prom, step as step_mod
from loadgen.run_k8s import CTX, NS, SEL, WEB, check_conditions, kubectl, scale, wait_ready
from loadgen.seed import ensure_ids
from loadgen.step import k8s_base

ROOT = Path(__file__).resolve().parent.parent
HPA_FILE = "k8s/hpa/web-hpa.yaml"
REQUESTS_CPU = 0.5          # requests.cpu пода web, ядер (варіант 19)

# ряди для кроків 11 і 12, крок знімання 1 с
SERIES = {
    "replicas_ready": f'kube_deployment_status_replicas_ready{{{SEL},deployment="web"}}',
    "replicas_spec": f'kube_deployment_spec_replicas{{{SEL},deployment="web"}}',
    "hpa_desired": f'kube_horizontalpodautoscaler_status_desired_replicas{{{SEL}}}',
    "hpa_current": f'kube_horizontalpodautoscaler_status_current_replicas{{{SEL}}}',
    "cpu_per_pod": f'rate(container_cpu_usage_seconds_total{{{WEB}}}[1m])',
    "cpu_utilization": (f'avg(rate(container_cpu_usage_seconds_total{{{WEB}}}[1m]))'
                        f' / {REQUESTS_CPU}'),
    "rps_per_pod": f'sum by (pod) (rate(http_requests_total{{{SEL},code=~"2.."}}[15s]))',
    "rps_total": f'sum(rate(http_requests_total{{{SEL},code=~"2.."}}[15s]))',
    "throttle_per_pod": (f'rate(container_cpu_cfs_throttled_periods_total{{{WEB}}}[1m])'
                         f' / rate(container_cpu_cfs_periods_total{{{WEB}}}[1m])'),
}


def hpa_state():
    """(поточні, бажані, використання CPU) з kubectl get hpa; None, якщо HPA немає."""
    r = kubectl("get", "hpa", "web", "-n", NS, "-o", "json", check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    st = json.loads(r.stdout).get("status", {})
    return (st.get("currentReplicas"), st.get("desiredReplicas"),
            (st.get("currentMetrics") or [{}])[0].get("resource", {})
            .get("current", {}).get("averageUtilization"))


def wait_hpa_one(seconds=60.0, timeout=600.0):
    """Крок 11: чекаємо, поки HPA показує одну репліку протягом seconds підряд."""
    deadline = time.time() + timeout
    since = None
    while time.time() < deadline:
        st = hpa_state()
        ok = st is not None and st[0] == 1 and st[1] == 1
        now = time.time()
        if ok:
            since = since or now
            if now - since >= seconds:
                print(f"HPA тримає одну репліку {now - since:.0f} с", flush=True)
                return True
        else:
            since = None
            print(f"  HPA: поточні {st and st[0]}, бажані {st and st[1]},"
                  f" CPU {st and st[2]}%", flush=True)
        time.sleep(5)
    raise RuntimeError("HPA не вийшов на одну репліку")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=("fixed", "hpa"), required=True)
    ap.add_argument("--replicas", type=int, default=4, help="R для режиму fixed")
    ap.add_argument("--x1", type=float, required=True, help="X(1) з кроку 7")
    ap.add_argument("--xr", type=float, required=True, help="X(R) з кроку 7")
    ap.add_argument("--spike-factor", type=float, default=profile.SPIKE_FACTOR)
    ap.add_argument("--path", default="/donor-registry/{id}")
    ap.add_argument("--out-dir", default="results/lab3")
    ap.add_argument("--name", default=None)
    ap.add_argument("--procs", type=int, default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--prom", default=None)
    ap.add_argument("--max-bg", type=float, default=35.0,
                    help="поріг завантаження CPU хоста перед прогоном, %% (кластер теж входить)")
    ap.add_argument("--allow-battery", action="store_true")
    a = ap.parse_args()

    # Умови прогону: на батареї або при зайнятому сторонніми програмами процесорі прогін
    # не показує стенд (спроба 19.11.2026 у results/lab3/v4/ — саме такий випадок).
    cond = check_conditions(a.max_bg, a.allow_battery)

    # той самий клієнт, що й у серії кроку 7: 32 зʼєднання по 100 запитів
    step_mod.configure(conns=32, req_per_conn=100)
    base = a.base or k8s_base()
    url = a.prom or prom.k8s_url()
    name = a.name or ("profile_fixed" if a.mode == "fixed" else "profile_hpa")
    out = ROOT / a.out_dir / name
    segments = profile.build_profile(a.x1, a.xr, a.spike_factor)
    print("профіль: " + ", ".join(f"{d:.0f} с на {r:.0f} rps" for d, r in segments),
          flush=True)

    if a.mode == "fixed":
        kubectl("delete", "hpa", "web", "-n", NS, "--ignore-not-found")
        scale(a.replicas)
        pods = wait_ready(base, a.replicas)
        print(f"подів видно: {len(pods)}", flush=True)
    else:
        scale(1)                       # HPA підніматиме від однієї репліки
        kubectl("apply", "-f", HPA_FILE)
        wait_ready(base, 1)
        wait_hpa_one()
        pods = []

    ids = ensure_ids(base, 100) if "{id}" in a.path else []
    summary = profile.run(base, a.path, ids, segments, out, a.procs,
                          label=f"{a.mode}, R={a.replicas}")

    end_wall = time.time()
    series = prom.dump(url, summary["start_wall"] - 30, end_wall + 15, 1.0, SERIES)
    Path(f"{out}_prom.json").write_text(json.dumps(series, ensure_ascii=False),
                                        encoding="utf-8")
    summary["mode"] = a.mode
    summary["replicas_fixed"] = a.replicas if a.mode == "fixed" else None
    summary["x1"] = a.x1
    summary["xr"] = a.xr
    summary["spike_rps"] = segments[1][1]
    summary["pods_at_start"] = pods
    summary["conditions"] = cond
    Path(f"{out}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                   encoding="utf-8")

    for m in summary["segment_stats"]:
        print(f"відрізок {m['segment']}: задано {m['target_rps']:.0f}, досягнуто "
              f"{m['achieved_rps']:.1f} ±{m['achieved_sd']:.1f}, p99 "
              f"{m['p99_ms_mean'] and round(m['p99_ms_mean'])} мс, не-2xx {m['non2xx']}",
              flush=True)
    print(f"  {out}.json, {out}.csv, {out}_prom.json")

    if a.mode == "hpa":
        kubectl("delete", "hpa", "web", "-n", NS, "--ignore-not-found")


if __name__ == "__main__":
    main()
