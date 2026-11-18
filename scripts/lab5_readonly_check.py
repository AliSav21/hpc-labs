"""Крок 3 ЛР5: перевірити, що жодна операція читання не змінює полів структури.

Перевірка двостороння:

1. Статична. Тіла ``peek_front``, ``peek_back`` і ``size`` розбираються в дерево
   розбору, і в них шукаються будь-які присвоєння в атрибут або елемент, ``del``,
   а також виклики методів самої структури, крім перелічених читальних. Так перевіряється
   не конкретний прогін, а весь код методу.

2. Динамічна. Знімок структури (розмір, обидва кінці, весь ланцюг вузлів разом із
   посиланнями ``prev`` і ``next``) порівнюється до і після великої кількості читань —
   спершу з одного потоку, потім з ``n_r`` потоків одночасно під RWLock.

Запуск: python -m scripts.lab5_readonly_check
"""

import ast
import inspect
import json
import sys
import threading
import time

from lab5 import structure
from lab5.rwlock import RWLock
from lab5.structure import BoundedDeque

READ_METHODS = ("peek_front", "peek_back", "size")
ALLOWED_CALLS = set(READ_METHODS)


def static_check():
    """Шукає в тілах читальних методів будь-яку зміну стану."""
    src = inspect.getsource(structure)
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "BoundedDeque")
    findings = []
    checked = []
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in READ_METHODS:
            continue
        checked.append(fn.name)
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, (ast.Attribute, ast.Subscript)):
                        findings.append(f"{fn.name}: присвоєння в {ast.dump(t)[:40]}")
            elif isinstance(node, ast.Delete):
                findings.append(f"{fn.name}: del")
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr not in ALLOWED_CALLS:
                    findings.append(f"{fn.name}: виклик .{f.attr}()")
    missing = [m for m in READ_METHODS if m not in checked]
    if missing:
        findings.append(f"у класі не знайдено методів: {missing}")
    return {"checked": checked, "findings": findings, "ok": not findings}


def _fill(dq, count):
    for i in range(count):
        dq.push_back(i)


def dynamic_single(ops=200_000):
    dq = BoundedDeque(1024)
    _fill(dq, 512)
    before = dq.fingerprint()
    reads = (dq.peek_front, dq.peek_back, dq.size)
    for i in range(ops):
        reads[i % 3]()
    after = dq.fingerprint()
    return {"ops": ops, "size": dq.size(), "unchanged": before == after}


def dynamic_threads(n_r=3, seconds=2.0):
    dq = BoundedDeque(1024)
    _fill(dq, 512)
    before = dq.fingerprint()
    lock = RWLock("reader")
    stop = [False]
    counts = [0] * n_r

    def worker(idx):
        reads = (dq.peek_front, dq.peek_back, dq.size)
        k = 0
        while not stop[0]:
            fn = reads[k % 3]
            k += 1
            lock.acquire_read()
            fn()
            lock.release_read()
        counts[idx] = k

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_r)]
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop[0] = True
    for t in threads:
        t.join(30)
    after = dq.fingerprint()
    return {
        "threads": n_r,
        "ops": sum(counts),
        "ops_per_thread": counts,
        "size": dq.size(),
        "unchanged": before == after,
        "invariants_ok": dq.check_invariants()["ok"],
    }


def main(argv=None):
    out_path = None
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--out":
        out_path = argv[1]

    st = static_check()
    ds = dynamic_single()
    dt = dynamic_threads()
    ok = st["ok"] and ds["unchanged"] and dt["unchanged"] and dt["invariants_ok"]

    print("Статична перевірка читальних методів:",
          "зміни стану не знайдено" if st["ok"] else "ЗНАЙДЕНО ЗМІНИ")
    print(f"  перевірено методів: {', '.join(st['checked'])}")
    for f in st["findings"]:
        print(f"  !! {f}")
    print(f"Один потік, {ds['ops']} читань: знімок структури "
          f"{'не змінився' if ds['unchanged'] else 'ЗМІНИВСЯ'}")
    print(f"{dt['threads']} потоки під RWLock, {dt['ops']} читань: знімок структури "
          f"{'не змінився' if dt['unchanged'] else 'ЗМІНИВСЯ'}, "
          f"інваріанти ok={dt['invariants_ok']}")
    print("ПІДСУМОК:", "читання не змінює структуру" if ok else "ПЕРЕВІРКА НЕ ПРОЙДЕНА")

    result = {"ok": ok, "static": st, "dynamic_single": ds, "dynamic_threads": dt}
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"підсумок: {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
