"""Точка входу образу воркера.

SUPERVISE = 1 запускає наглядача (крок 9 ЛР4, прогін із FAIL_RATE), інакше сам воркер
стає процесом 1 контейнера — так, як цього вимагає умова для коректної зупинки за
SIGTERM і для перевірок кроків 2-8.
"""
import os
import sys

if __name__ == "__main__":
    mod = "worker.supervise" if os.environ.get("SUPERVISE") == "1" else "worker.main"
    # execv, а не subprocess: процесом 1 контейнера лишається вибраний модуль,
    # тому SIGTERM від kubelet приходить саме йому
    os.execv(sys.executable, [sys.executable, "-m", mod])
