"""Обхід збірки PyTorch для Windows без libuv.

`torch 2.14.0+cpu` для Windows зібрано без libuv, а частина коду `torch.distributed`
створює `TCPStore` із типовим `use_libuv=True` і падає:

    DistStoreError: use_libuv was requested but PyTorch was built without libuv support

Сам torch на win32 для `env://` типово бере `use_libuv=0` (`rendezvous.py`), тож підміна
нижче лише поширює цей самий типовий вибір на решту місць: store рандеву агента elastic
(бекенди `static`, `c10d`, `dynamic`) і store, який процес-воркер створює під агентом.
На інших ОС підміна не робиться.

Викликається двічі: `patch_worker()` у процесі навчання перед `init_process_group` і
`patch_agent()` в обгортці torchrun ([scripts/lab6_torchrun.py](../scripts/lab6_torchrun.py)).
"""

import os
import sys

from torch.distributed import TCPStore as _TCPStore


def _tcp_store(*args, **kwargs):
    kwargs.setdefault("use_libuv", False)
    return _TCPStore(*args, **kwargs)


def active():
    return sys.platform == "win32"


def patch_worker():
    """Store, який процес навчання створює в init_process_group через env://."""
    if not active():
        return False
    import importlib

    # у torch.distributed назву rendezvous займає функція, тому модуль беремо importlib
    rdzv = importlib.import_module("torch.distributed.rendezvous")
    rdzv.TCPStore = _tcp_store
    os.environ.setdefault("USE_LIBUV", "0")
    return True


def patch_agent():
    """Store рандеву агента elastic (torchrun)."""
    if not active():
        return False
    import torch.distributed as dist
    import torch.distributed.elastic.rendezvous.c10d_rendezvous_backend as c10d_rdzv
    import torch.distributed.elastic.rendezvous.static_tcp_rendezvous as static_rdzv

    static_rdzv.TCPStore = _tcp_store
    c10d_rdzv.TCPStore = _tcp_store
    dist.TCPStore = _tcp_store  # dynamic_rendezvous звертається до dist.TCPStore
    os.environ.setdefault("USE_LIBUV", "0")
    return True
