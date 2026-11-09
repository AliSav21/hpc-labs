# Протокол перевірки стенду ЛР4

Варіант 19, кластер **kind** `hpc` (1 control-plane + 2 worker), образи
`donor-registry:lab3` (сервіс) і `donor-worker:lab4` (воркер).

Скрипт: [scripts/lab4_selfcheck.ps1](../../scripts/lab4_selfcheck.ps1). Нічого не змінює,
окрім однієї тимчасової задачі в черзі, яку сам же і прибирає.

```powershell
powershell -File scripts\lab4_selfcheck.ps1
```

Що перевіряється:

| № | Перевірка | Що має бути |
|---|---|---|
| 1 | таблиці `tasks` і `task_results` створені міграцією | обидві в `information_schema.tables` |
| 2 | усі поля `tasks` за умовою | 12 полів, зокрема `token`, `lease_until`, `attempts` |
| 3 | значення за замовчуванням | `max_attempts = 3`, `token = 0` |
| 4 | індекс за `(status, created_at)` | `btree (status, created_at)` |
| 5 | первинний ключ `task_results` і зовнішній ключ `tasks.record_id` | по одному |
| 6 | тригер NOTIFY (вимога Д5) | `tasks_notify_insert` викликає `tasks_notify` |
| 7 | ресурси воркера | grace 30 с, requests 250m, limits 500m і 512Mi |
| 8 | у маніфесті Deployment немає `replicas` | кількість задає `kubectl scale` |
| 9 | `WORKER_ID` з Downward API | `metadata.name` |
| 10 | параметри варіанта 19 у ConfigMap | `50 \| 5 \| 0.02 \| 0.98 \| 256 \| 0` |
| 11 | `POST /<ресурс>/{id}/tasks` | 202, статус `queued`, `seed >= 1` |
| 12 | той самий POST для неіснуючого запису | 404 |
| 13 | `GET /tasks/{id}` і `GET /tasks?status=` | задача з результатом, перелік фільтрується |
| 14 | вимірювачі черги в `/metrics` сервісу | `tasks_queued`, `tasks_running` |
| 15 | метрики воркера | чотири метрики кроку 2 |
| 16 | мітки кроків у гістограмі | `claim`, `fetch`, `process`, `store` |
| 17 | поди `worker` у збору Prometheus | щонайменше одна ціль, усі `up` |

## Вивід

```
== самоперевірка стенду ЛР4, контекст kind-hpc
[ 1] ПРОЙДЕНО  таблиці tasks і task_results створені міграцією
      вийшло:   donor_registry,task_results,tasks
[ 2] ПРОЙДЕНО  усі поля tasks за умовою
      вийшло:   attempts,created_at,finished_at,id,lease_until,max_attempts,record_id,seed,started_at,status,token,work
er_id
[ 3] ПРОЙДЕНО  max_attempts = 3 і token = 0 за замовчуванням
      вийшло:   max_attempts=3 token=0
[ 4] ПРОЙДЕНО  індекс за (status, created_at)
      вийшло:   CREATE UNIQUE INDEX tasks_pkey ON public.tasks USING btree (id) | CREATE INDEX idx_tasks_status_created
 ON public.tasks USING btree (status, created_at) | CREATE INDEX idx_tasks_running_lease ON public.tasks USING btree (l
ease_until) WHERE (status = 'running'::text)
[ 5] ПРОЙДЕНО  первинний ключ task_results і зовнішній ключ tasks.record_id
      вийшло:   pkey=1, fkey=1
[ 6] ПРОЙДЕНО  тригер на вставку в tasks надсилає повідомлення (Д5)
      вийшло:   tasks_notify_insert:tasks_notify
[ 7] ПРОЙДЕНО  worker: grace 30 с, requests.cpu 250m, limits 500m і 512Mi
      вийшло:   30|250m|500m|512Mi
[ 8] ПРОЙДЕНО  у маніфесті Deployment worker немає поля replicas
[ 9] ПРОЙДЕНО  WORKER_ID заповнюється іменем пода через Downward API
      вийшло:   metadata.name
[10] ПРОЙДЕНО  параметри варіанта 19: T_poll 50 мс, L 5 с, 0,02/0,98, S 256, FAIL_RATE 0
      вийшло:   50|5|0.02|0.98|256|0
[11] ПРОЙДЕНО  POST /donor-registry/{id}/tasks віддає 202 і тіло задачі
      вийшло:   код 202, статус queued, seed 113998021
[12] ПРОЙДЕНО  POST задачі для неіснуючого запису віддає 404
      вийшло:   404
[13] ПРОЙДЕНО  GET /tasks/{id} віддає стан із результатом, GET /tasks фільтрує за status
      вийшло:   статус done, результат є, у переліку done 1
[14] ПРОЙДЕНО  у /metrics сервісу є tasks_queued і tasks_running
      вийшло:   tasks_queued 0.0 ; tasks_running 0.0
[15] ПРОЙДЕНО  у /metrics воркера є всі чотири метрики кроку 2
      вийшло:   worker_tasks_total, worker_claims_total, worker_task_duration_seconds, worker_tasks_in_flight
[16] ПРОЙДЕНО  гістограма має мітки кроків claim, fetch, process, store
      вийшло:   claim, fetch, process, store
[17] ПРОЙДЕНО  поди worker у збору Prometheus у стані up
      вийшло:   цілей 1, станів: up

ПРОЙДЕНО 17, НЕ ПРОЙДЕНО 0
```
