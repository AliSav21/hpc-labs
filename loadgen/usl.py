"""Універсальний закон масштабованості
"""
import sys as _sys
_sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: cp1252
import argparse
import json
import math
from pathlib import Path


def model(n, lam, sigma, kappa):
    """X(N) = λN / (1 + σ(N − 1) + κN(N − 1))."""
    return lam * n / (1 + sigma * (n - 1) + kappa * n * (n - 1))


def fit(points):
    """points: {N: X(N)} з N = 1 обовʼязково. Повертає оцінки і проміжні суми."""
    x1 = points[1]
    rel = {n: x / x1 for n, x in sorted(points.items())}
    rows = []
    two_p = None  # сира двопараметрична оцінка до правила
    for n, c in rel.items():
        if n <= 1:
            continue
        rows.append({"N": n, "C": c, "y": n / c - 1, "u": n - 1, "v": n * (n - 1)})
    s = {k: sum(r[a] * r[b] for r in rows)
         for k, (a, b) in {"uy": ("u", "y"), "vy": ("v", "y"), "uu": ("u", "u"),
                           "vv": ("v", "v"), "uv": ("u", "v")}.items()}
    det = s["uu"] * s["vv"] - s["uv"] ** 2
    note = None
    if len(rows) < 2 or det == 0:
        # для двох невідомих потрібні щонайменше дві точки з N > 1
        sigma = s["uy"] / s["uu"] if s["uu"] else 0.0
        kappa = 0.0
        note = "менше двох точок для підбору: κ = 0, σ за однопараметричною формулою"
    else:
        sigma = (s["vv"] * s["uy"] - s["uv"] * s["vy"]) / det
        kappa = (s["uu"] * s["vy"] - s["uv"] * s["uy"]) / det
        two_p = {"sigma": sigma, "kappa": kappa}
        if sigma < 0:
            sigma = 0.0
            kappa = s["vy"] / s["vv"] if s["vv"] else 0.0
            note = "σ вийшла відʼємною: σ = 0, κ за однопараметричною формулою"
        elif kappa < 0:
            kappa = 0.0
            sigma = s["uy"] / s["uu"] if s["uu"] else 0.0
            note = "κ вийшла відʼємною: κ = 0, σ за однопараметричною формулою"

    out = {"lambda": x1, "sigma": sigma, "kappa": kappa, "note": note,
           "two_param": two_p,
           "sums": s, "det": det, "rows": rows,
           "C": {str(n): c for n, c in rel.items()},
           "fit_points": [r["N"] for r in rows]}
    if kappa > 0 and sigma < 1:
        out["N_star"] = math.sqrt((1 - sigma) / kappa)
        out["X_at_N_star"] = model(out["N_star"], x1, sigma, kappa)
    else:
        out["N_star"] = None
        out["X_limit"] = x1 / sigma if sigma > 0 else None
    out["model"] = {str(n): model(n, x1, sigma, kappa) for n in (1, 2, 3, 4)}
    return out


def report(res):
    lines = [f"λ = X(1) = {res['lambda']:.1f} rps",
             f"σ = {res['sigma']:.6f}", f"κ = {res['kappa']:.6f}"]
    s = res["sums"]
    if res.get("two_param"):
        t = res["two_param"]
        lines.append(f"двопараметрична оцінка: σ = {t['sigma']:.6f}, κ = {t['kappa']:.6f} "
                     f"(Δ = {res['det']:.4f})")
    lines.append(f"Σu² = {s['uu']:.4f}, Σv² = {s['vv']:.4f}, Σuv = {s['uv']:.4f}, "
                 f"Σuy = {s['uy']:.6f}, Σvy = {s['vy']:.6f}")
    for r in res["rows"]:
        lines.append(f"  N = {r['N']}: C = {r['C']:.4f}, y = {r['y']:.6f}, "
                     f"u = {r['u']}, v = {r['v']}")
    if res.get("N_star"):
        lines.append(f"N* = √((1 − σ) / κ) = {res['N_star']:.2f}, "
                     f"X(N*) = {res['X_at_N_star']:.1f} rps")
    elif res.get("X_limit"):
        lines.append(f"κ = 0: максимуму немає, межа λ/σ = {res['X_limit']:.1f} rps")
    else:
        lines.append("κ = 0 і σ = 0: закон дає лінійне зростання, межі немає")
    if res["note"]:
        lines.append("Зауваження: " + res["note"])
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--points", required=True,
                    help='X(N) у вигляді 1=480,2=790,3=900,4=950 або шлях до JSON {"1": ...}')
    ap.add_argument("--exclude", default="", help="точки, обмежені генератором: 4 або 3,4")
    ap.add_argument("--out")
    a = ap.parse_args()
    if Path(a.points).exists():
        raw = {int(k): float(v) for k, v in json.loads(Path(a.points).read_text()).items()}
    else:
        raw = {int(k): float(v) for k, v in (p.split("=") for p in a.points.split(","))}
    drop = {int(x) for x in a.exclude.split(",") if x.strip()}
    pts = {n: x for n, x in raw.items() if n == 1 or n not in drop}
    res = fit(pts)
    res["measured"] = {str(n): x for n, x in sorted(raw.items())}
    res["excluded"] = sorted(drop)
    print(report(res))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"-> {a.out}")
