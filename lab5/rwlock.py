"""Замок читання-запису на ``threading.Condition`` (крок 3 ЛР5).

Готові колекції не використовуються; тут лише примітиви синхронізації — мʼютекс і
умовна змінна, як дозволяє умова.

Політика. Реалізовано дві, вибір — параметром ``policy``:

``"reader"`` — з перевагою читача (основна в роботі, крок 3). Читач заходить, щойно
немає активного писця, навіть якщо писці вже чекають; кілька читачів можуть тримати
замок одночасно. Писець заходить, коли немає ні активного писця, ні активних читачів,
тож читачі, що приходять один за одним, можуть відкладати його.

``"writer"`` — з перевагою писця (додатковий прогін). Новий читач стає в чергу, якщо є
хоча б один писець, що вже чекає (``_waiting_writers > 0``). Читачі, що приходять,
писця, який уже чекає, не обганяють. За умовою ЛР5 половина потоків пише без пауз,
тож ``_waiting_writers`` майже завжди більший за нуль, і чекати довго доводиться вже
читачам.

Спільне для обох політик. Пробудження через ``notify_all``, а чекати далі чи ні, кожен
потік вирішує сам за умовою в ``while``, тож втрачених пробуджень немає. Черги
(порядку FIFO) серед тих, хто чекає, немає: хто першим захопить внутрішній мʼютекс
умовної змінної після звільнення, той і заходить. Потік, що звільнив замок і одразу
просить його знову, зазвичай встигає раніше за розбуджених, бо ще тримає GIL. Тому
писець може довго чекати й за перевагою писця — за іншими писцями
(``scripts/lab5_selfcheck.py`` і розділ про перевагу писця у звіті).
"""

import threading


class RWLock:
    __slots__ = ("_cond", "_readers", "_writer", "_waiting_writers", "_writer_first")

    def __init__(self, policy="reader"):
        if policy not in ("writer", "reader"):
            raise ValueError("політика має бути 'writer' або 'reader'")
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0           # активних читачів
        self._writer = False        # активний писець
        self._waiting_writers = 0   # писців у черзі
        self._writer_first = policy == "writer"

    # ---------- читання ----------

    def acquire_read(self):
        with self._cond:
            if self._writer_first:
                while self._writer or self._waiting_writers:
                    self._cond.wait()
            else:
                while self._writer:
                    self._cond.wait()
            self._readers += 1

    def release_read(self):
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    # ---------- запис ----------

    def acquire_write(self):
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True

    def release_write(self):
        with self._cond:
            self._writer = False
            self._cond.notify_all()


class GlobalLock:
    """Глобальний замок кроку 2: той самий мʼютекс на запис і на читання.

    Обгортка потрібна лише для того, щоб стенд звертався до обох способів
    синхронізації через однаковий інтерфейс.
    """

    __slots__ = ("_lock", "acquire_read", "release_read", "acquire_write", "release_write")

    def __init__(self):
        self._lock = threading.Lock()
        # прямі посилання на методи мʼютекса: без зайвого рівня виклику в гарячому циклі
        self.acquire_read = self._lock.acquire
        self.release_read = self._lock.release
        self.acquire_write = self._lock.acquire
        self.release_write = self._lock.release


def make_lock(kind):
    if kind == "global":
        return GlobalLock()
    if kind == "rw":
        return RWLock("reader")
    if kind == "rw-writer":
        return RWLock("writer")
    raise ValueError(f"невідомий спосіб синхронізації: {kind}")
