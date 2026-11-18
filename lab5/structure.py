"""Структура С9 варіанта 19: двобічна черга (дек) задач місткістю 1024.

Синхронізації тут немає зовсім: замок накладає стенд ([lab5/bench.py]), щоб той самий
код структури працював і під глобальним замком, і під RWLock. Готові потокобезпечні
колекції не використовуються — дек є власним двозвʼязним списком вузлів.
"""

CAPACITY = 1024


class Node:
    """Вузол двозвʼязного списку. ``__slots__`` — щоб вузол не мав словника атрибутів."""

    __slots__ = ("value", "prev", "next")

    def __init__(self, value):
        self.value = value
        self.prev = None
        self.next = None


class InvariantError(AssertionError):
    pass


class BoundedDeque:
    """Дек місткістю ``capacity`` на двозвʼязному списку.

    Операції запису: ``push_front``, ``push_back``, ``pop_front``, ``pop_back``.
    Операції читання: ``peek_front``, ``peek_back``, ``size`` — не змінюють жодного поля
    ні деку, ні вузлів (перевіряється [scripts/lab5_readonly_check.py]).
    """

    __slots__ = ("capacity", "_head", "_tail", "_size", "_pushed", "_popped",
                 "_rejected", "_empty_pops", "_max_size")

    def __init__(self, capacity=CAPACITY):
        if capacity < 1:
            raise ValueError("місткість має бути додатною")
        self.capacity = capacity
        self._head = None
        self._tail = None
        self._size = 0
        # лічильники для перевірки інваріантів після прогону
        self._pushed = 0
        self._popped = 0
        self._rejected = 0
        self._empty_pops = 0
        self._max_size = 0

    # ---------- запис ----------

    def push_front(self, value):
        if self._size >= self.capacity:
            self._rejected += 1
            return False
        node = Node(value)
        head = self._head
        if head is None:
            self._tail = node
        else:
            node.next = head
            head.prev = node
        self._head = node
        self._size += 1
        self._pushed += 1
        if self._size > self._max_size:
            self._max_size = self._size
        return True

    def push_back(self, value):
        if self._size >= self.capacity:
            self._rejected += 1
            return False
        node = Node(value)
        tail = self._tail
        if tail is None:
            self._head = node
        else:
            node.prev = tail
            tail.next = node
        self._tail = node
        self._size += 1
        self._pushed += 1
        if self._size > self._max_size:
            self._max_size = self._size
        return True

    def pop_front(self):
        node = self._head
        if node is None:
            self._empty_pops += 1
            return None
        nxt = node.next
        self._head = nxt
        if nxt is None:
            self._tail = None
        else:
            nxt.prev = None
        node.next = None
        self._size -= 1
        self._popped += 1
        return node.value

    def pop_back(self):
        node = self._tail
        if node is None:
            self._empty_pops += 1
            return None
        prv = node.prev
        self._tail = prv
        if prv is None:
            self._head = None
        else:
            prv.next = None
        node.prev = None
        self._size -= 1
        self._popped += 1
        return node.value

    # ---------- читання ----------

    def peek_front(self):
        node = self._head
        return None if node is None else node.value

    def peek_back(self):
        node = self._tail
        return None if node is None else node.value

    def size(self):
        return self._size

    # ---------- перевірка інваріантів ----------

    def check_invariants(self):
        """Інваріанти С9 з умови: розмір від 0 до місткості; сусідні вузли посилаються
        один на одного в обох напрямках. Повертає словник із підсумком або кидає
        ``InvariantError``."""
        if not 0 <= self._size <= self.capacity:
            raise InvariantError(f"розмір {self._size} поза межами 0..{self.capacity}")

        # прохід уперед: лічильник вузлів, звʼязок next -> prev, кінці списку
        if self._head is not None and self._head.prev is not None:
            raise InvariantError("у голови списку є prev")
        if self._tail is not None and self._tail.next is not None:
            raise InvariantError("у хвоста списку є next")
        if (self._head is None) != (self._tail is None):
            raise InvariantError("один кінець списку порожній, другий ні")
        if (self._size == 0) != (self._head is None):
            raise InvariantError(f"розмір {self._size} не відповідає порожньому списку")

        forward = 0
        node = self._head
        while node is not None:
            forward += 1
            if forward > self.capacity + 1:
                raise InvariantError("прохід уперед довший за місткість: цикл у списку")
            nxt = node.next
            if nxt is not None and nxt.prev is not node:
                raise InvariantError("node.next.prev не є node")
            if node.prev is not None and node.prev.next is not node:
                raise InvariantError("node.prev.next не є node")
            if nxt is None and node is not self._tail:
                raise InvariantError("прохід уперед закінчився не на хвості")
            node = nxt

        backward = 0
        node = self._tail
        while node is not None:
            backward += 1
            if backward > self.capacity + 1:
                raise InvariantError("прохід назад довший за місткість: цикл у списку")
            node = node.prev

        if forward != self._size or backward != self._size:
            raise InvariantError(
                f"вузлів уперед {forward}, назад {backward}, а розмір {self._size}")
        if self._pushed - self._popped != self._size:
            raise InvariantError(
                f"покладено {self._pushed}, вийнято {self._popped}, розмір {self._size}")

        return {
            "ok": True,
            "size": self._size,
            "max_size": self._max_size,
            "capacity": self.capacity,
            "pushed": self._pushed,
            "popped": self._popped,
            "rejected_full": self._rejected,
            "pops_on_empty": self._empty_pops,
            "nodes_forward": forward,
            "nodes_backward": backward,
        }

    def fingerprint(self):
        """Повний знімок стану структури: розмір і ланцюг значень разом із посиланнями.
        Використовується, щоб довести, що операції читання нічого не змінюють."""
        chain = []
        node = self._head
        while node is not None:
            chain.append((id(node), node.value, id(node.prev), id(node.next)))
            node = node.next
        return (self._size, id(self._head), id(self._tail), tuple(chain))
