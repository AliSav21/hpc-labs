"""torchrun на цій машині: обхід збірки PyTorch для Windows без libuv.

Збірка `torch 2.14.0+cpu` для Windows не містить libuv, а рандеву агента elastic створює
`TCPStore` із типовим `use_libuv=True` і падає ще до старту процесів навчання:

    DistStoreError: use_libuv was requested but PyTorch was built without libuv support

Обгортка підставляє `use_libuv=False` там, де агент створює store (бекенди рандеву
`static`, `c10d` і `dynamic`), і далі викликає той самий `torch.distributed.run`, який
запускає `torchrun`. Решта поведінки не змінюється: кількість процесів, змінні середовища
RANK / WORLD_SIZE / LOCAL_RANK / MASTER_ADDR / MASTER_PORT, `--max-restarts`, статична
адреса групи. Подробиці підміни — [lab6/winuv.py](../lab6/winuv.py).

Запуск замість `torchrun`:

    python -m scripts.lab6_torchrun --standalone --nproc_per_node=2 -m lab6.train ...
"""

import sys

from lab6.winuv import patch_agent

patch_agent()

from torch.distributed.run import main  # noqa: E402 — після підміни TCPStore

if __name__ == "__main__":
    main(sys.argv[1:])
