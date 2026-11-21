"""Опис стенду і умови прогону.

Умови (живлення, економія заряду, роздільність таймера, фонове завантаження процесора)
рахує той самий код, що в ЛР5: прогони ЛР6 міряють час кроку в мілісекундах, і без
записаних умов два прогони в різному живленні за самими файлами не відрізнити.
"""

import os
import platform
import re
import subprocess
import sys

from lab5.bench import run_conditions  # noqa: F401 — реекспорт для прогонів ЛР6

_PHYS = None


def utf8_console():
    """Консоль Windows типово cp1252, а у виводі прогонів і таблиць є кирилиця."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def physical_cores():
    """Кількість фізичних ядер; None, якщо визначити не вдалося."""
    global _PHYS
    if _PHYS is not None:
        return _PHYS
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor |"
                 " Measure-Object -Property NumberOfCores -Sum).Sum"],
                capture_output=True, timeout=30).stdout.decode("ascii", "replace")
            m = re.search(r"\d+", out)
            if m:
                _PHYS = int(m.group(0))
        except (OSError, subprocess.SubprocessError):
            pass
    return _PHYS


def cpu_name():
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
            return out.strip().splitlines()[0].strip() if out.strip() else None
        except (OSError, subprocess.SubprocessError):
            return None
    return platform.processor() or None


def memory_gb():
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                capture_output=True, timeout=30).stdout.decode("ascii", "replace")
            m = re.search(r"\d+", out)
            if m:
                return round(int(m.group(0)) / 1024 ** 3, 1)
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def host_info(conditions=None, full=False):
    import torch

    info = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "torch": torch.__version__,
        "logical_cores": os.cpu_count(),
        "physical_cores": physical_cores(),
        "torch_num_threads": torch.get_num_threads(),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
    }
    if full:
        info["cpu"] = cpu_name()
        info["memory_gb"] = memory_gb()
    if conditions:
        info["conditions"] = conditions
    return info
