# Лабораторна робота 4: як це зроблено і як перевірити руками

Варіант 19. Коротка послідовність команд — [RUN-LAB4.md](RUN-LAB4.md), результати і висновки —
[REPORT-LAB4.md](REPORT-LAB4.md). Тут — чому стенд зроблений саме так і як перевірити кожне
твердження звіту окремою командою.

Усе виконується з кореня репозиторію. Контекст kubectl усюди задається явно
(`--context kind-hpc`): без цього команда пішла б у поточний контекст kubeconfig.

## Що ЛР4 додала до стенду ЛР3

| Що | Де | Навіщо |
|---|---|---|
| таблиці `tasks` і `task_results`, тригер NOTIFY | [db/schema-lab4.sql](db/schema-lab4.sql) | черга задач і результати |
| Job міграції | [k8s/base/13-db-migrate-lab4-job.yaml](k8s/base/13-db-migrate-lab4-job.yaml) | накочує схему ЛР4 після схеми ЛР1 |
| три ендпоінти черги, вимірювачі `tasks_queued` і `tasks_running`, повернення задач зі спливлим lease | [app/main.py](app/main.py) | крок 1 і крок 5 |
| воркер: захоплення, обробка зрізу, запис результату | [worker/main.py](worker/main.py) | крок 2 і далі |
| крок обробки зрізу | [worker/slice.py](worker/slice.py) | генератор зрізу і точкове перетворення |
| наглядач процесу воркера | [worker/supervise.py](worker/supervise.py) | прогін кроку 9 без CrashLoopBackOff |
| образ воркера | [Dockerfile.worker](Dockerfile.worker) | окремий образ за умовою кроку 2 |
| ConfigMap і Deployment воркера, PodMonitor | `k8s/base/30-*`, `31-*`, `k8s/monitoring/worker-podmonitor.yaml` | розгортання і збір метрик |
| генератор задач | [loadgen/tasks.py](loadgen/tasks.py) | режим створення задач за умовою кроку 2 |
| прогони, таблиці, діаграми, самоперевірка | `scripts/lab4_*` | кроки 2-10 |
| звірка кроку обробки з незалежною реалізацією | [scripts/xcheck_slice.py](scripts/xcheck_slice.py) | перевірка після вимірювань |

Сервіс `web`, база, балансування і моніторинг не змінювались, крім доданих ендпоінтів і двох
вимірювачів.

## Схема черги

```sql
CREATE TABLE tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id BIGINT NOT NULL REFERENCES donor_registry (id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued'
           CHECK (status IN ('queued','running','done','failed')),
    seed BIGINT NOT NULL CHECK (seed >= 0 AND seed < 2147483648),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    token INTEGER NOT NULL DEFAULT 0,
    lease_until TIMESTAMPTZ, worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ);
CREATE INDEX idx_tasks_status_created ON tasks (status, created_at);
```

Два рішення поза дослівним текстом умови:

- **Частковий індекс `idx_tasks_running_lease`** `(lease_until) WHERE status = 'running'`.
  Повернення задач зі спливлим lease виконується кожні 5 секунд запитом
  `WHERE status = 'running' AND lease_until < now()`. Індекс за `(status, created_at)` для
  нього не годиться: `created_at` у предикаті немає.
- **`CHECK (seed >= 0 ...)` дозволяє нуль.** Сервіс генерує `seed` у домені генератора зрізу
  1 ≤ seed < 2³¹, а нуль лежить поза ним, і крок обробки на ньому викидає виняток. Це і є
  сценарій відмови Ф3, тому в таблиці нуль дозволений навмисно.

Перевірка:

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "\d tasks"
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "\d task_results"
```

## Крок обробки зрізу

[worker/slice.py](worker/slice.py), лише цикли Python, без NumPy і Numba.

```
x(0) = seed, x(i+1) = (1103515245·x(i) + 12345) mod 2^31, p(i) = x(i+1) >> 19
```

`mod 2^31` реалізовано як `& 0x7FFFFFFF`, зсув на 19 лишає 12 старших бітів із 31, тобто
значення 0..4095. Квантиль рівня q — найменший рівень, на якому накопичена частка гістограми
досягає q. Округлення `⌊4095·(p − p_lo)/(p_hi − p_lo)⌉` зроблено цілочисельно,
`(2a + b) // 2b`, щоб результат не залежав від порядку операцій із плаваючою комою і був
однаковим на будь-якій машині.

Детермінованість результату перевіряється прямо:

```powershell
.\.venv\Scripts\python.exe worker\slice.py 12345 0.02 0.98
.\.venv\Scripts\python.exe worker\slice.py 12345 0.02 0.98
```

Два виклики дають однакові `p_lo`, `p_hi`, `dropped`, `mean` і `sd`. На `seed = 0` виклик
падає з `SliceError`:

```powershell
.\.venv\Scripts\python.exe worker\slice.py 0
```

## Режими воркера

Один образ проходить усі кроки роботи; режим задається змінними середовища. Скрипт прогонів
перемикає їх через `kubectl set env deployment/worker`, тому ConfigMap лишається джерелом
значень варіанта, а не журналом експериментів.

| Змінна | Значення | Де вживається |
|---|---|---|
| `CLAIM_MODE` | `naive`, `for_update`, `skip_locked` | кроки 3 і 4 |
| `RESULT_MODE` | `insert`, `upsert` | крок 3 проти кроку 5 |
| `LEASE_MODE` | `off`, `on` | lease, heartbeat і умова за токеном |
| `HEARTBEAT` | `on`, `off` | крок 5 |
| `WAIT_MODE` | `poll`, `listen` | крок 10, вимога Д5 |
| `POLL_MS`, `LEASE_S` | 50, 5 за варіантом | кроки 3-10 |
| `FAIL_RATE` | 0, 0.15 | крок 9 |
| `SUPERVISE` | 0, 1 | крок 9 |
| `REAP_IN_WORKER` | 0, 1 | хто повертає задачі зі спливлим lease |

Побачити поточні:

```powershell
kubectl --context kind-hpc set env deployment/worker -n donors --list
```

## Де виконується повернення задач зі спливлим lease

Умова дозволяє робити це або в сервісі `web` кожні 5 секунд, або в кожному воркері перед
захопленням. Вибрано сервіс: один запит на 5 секунд проти W запитів на кожне захоплення.
За порожньої черги на чотирьох воркерах із `T_poll = 50 мс` варіант «у воркері» дав би
80 додаткових запитів до бази за секунду, і метрика «запитів до бази за порожньої черги» з
вимоги Д5 міряла б переважно їх. Варіант у воркері лишився під ключем `REAP_IN_WORKER=1`.

Там само, одним запитом, сервіс знімає довжину черги для вимірювачів `tasks_queued` і
`tasks_running`.

```powershell
kubectl --context kind-hpc exec -n donors deployment/web -- python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode().splitlines() if l.startswith('tasks_')])"
```

## Чому `kubectl exec ... -- kill -STOP 1` не діє

Умова кроку 5 велить зупинити воркер командою `kubectl exec ... -- kill -STOP 1`. На цьому
стенді вона не має дії, і це перевіряється:

```powershell
$p = kubectl --context kind-hpc get pods -n donors -l app=worker -o jsonpath="{.items[0].metadata.name}"
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "cut -d' ' -f3 /proc/1/stat"
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "kill -STOP 1"
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "cut -d' ' -f3 /proc/1/stat"
```

Третє поле `/proc/1/stat` лишається `S` замість `T`. Причина в ядрі: процес із номером 1 у
просторі імен PID є init цього простору, і сигнал, надісланий зсередини того самого простору,
доставляється йому лише за наявності обробника. Для SIGSTOP обробника не буває, тож сигнал
відкидається. Окремої програми `kill` в образі немає (`python:3.12-slim` без `procps`), тому
сигнал надсилає вбудована команда оболонки — на результат це не впливає.

Сигнал із простору імен вузла kind діє:

```powershell
$node = kubectl --context kind-hpc get pod -n donors $p -o jsonpath="{.spec.nodeName}"
$cid = (kubectl --context kind-hpc get pod -n donors $p -o jsonpath="{.status.containerStatuses[0].containerID}") -replace "containerd://",""
$pid = docker exec $node crictl inspect --output go-template --template "{{.info.pid}}" $cid
docker exec $node kill -STOP $pid
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "cut -d' ' -f3 /proc/1/stat"   # T
docker exec $node kill -CONT $pid
```

Саме це робить [scripts/lab4_run.py](scripts/lab4_run.py) у кроці 5: пробує команду з умови,
перевіряє стан процесу і за потреби переходить на простір імен вузла. Обидві спроби записані
в `results/lab4/step5.json` (`documented_command_works`, `stop_via`).

## Чому прогін кроку 9 робиться двічі

`FAIL_RATE = 0,15` на інтенсивності 0,7·X(R) ≈ 20 задач/с означає близько трьох падінь за
секунду на пул. kubelet збільшує паузу перед перезапуском контейнера, і за кілька хвилин усі
чотири пода стоять у `CrashLoopBackOff` з паузою 5 хвилин:

```powershell
kubectl --context kind-hpc get pods -n donors -l app=worker
kubectl --context kind-hpc get pod -n donors <под> -o jsonpath="{.status.containerStatuses[0].state}"
```

Пул зупиняється не через повторну обробку задач, а через цю паузу, і розподіл задач за
`attempts` зняти вже ні з чого. Тому крок 9 виконується трьома прогонами: `k8s` (за умовою, як
спостереження), `poison` і `clean` (із наглядачем процесу воркера). Наглядач лишає механізм
падіння незмінним — `os._exit(1)` посеред задачі, зʼєднання не закриваються, задача лишається в
`running` до спливання lease — але вартістю повернення воркера стає запуск процесу, а не пауза
kubelet. Перезапуски він рахує сам:

```powershell
kubectl --context kind-hpc exec -n donors deployment/worker -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9101/metrics').read().decode())"
```

## Ручні перевірки кроків

### Крок 1: ендпоінти

```powershell
$rid = (Invoke-RestMethod "http://127.0.0.1:18080/donor-registry?limit=1").items[0].id
$t = Invoke-RestMethod "http://127.0.0.1:18080/donor-registry/$rid/tasks" -Method Post
Invoke-RestMethod "http://127.0.0.1:18080/tasks/$($t.id)"
Invoke-RestMethod "http://127.0.0.1:18080/tasks?status=done&limit=3&offset=0"
```

404 на неіснуючий запис і на неіснуючу задачу:

```powershell
try { Invoke-RestMethod "http://127.0.0.1:18080/donor-registry/999999999/tasks" -Method Post } catch { $_.Exception.Response.StatusCode.value__ }
try { Invoke-RestMethod "http://127.0.0.1:18080/tasks/999999999" } catch { $_.Exception.Response.StatusCode.value__ }
```

### Крок 3: дублі наївного захоплення

Після прогону кроку 3 задачі з двома результатами видно прямо:

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select task_id, count(*) from task_results group by task_id having count(*) > 1 limit 10"
```

За журналами — та сама задача, позначена виконаною двома воркерами:

```powershell
kubectl --context kind-hpc logs -n donors -l app=worker -c worker --tail=-1 --prefix | Select-String '"msg": "виконано"'
```

### Крок 4: очікування блокувань

Під прогоном `for_update`:

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select count(*) from pg_stat_activity where wait_event_type = 'Lock'"
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select count(*) from pg_locks where not granted"
```

### Крок 5: fencing token

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select id, status, attempts, token, worker_id from tasks where id = <id задачі зі звіту>"
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select count(*) from task_results where task_id = <id>"
```

### Крок 7: W_q і довжина черги

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "select count(*) n, avg(extract(epoch from started_at - created_at)) wq from tasks where started_at is not null"
```

### Крок 10: запити до бази за порожньої черги

```powershell
kubectl --context kind-hpc exec -n donors deployment/worker -- python -c "import urllib.request;print([l for l in urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode().splitlines() if l.startswith('worker_db_queries_total')])"
```

За порожньої черги в режимі опитування лічильник росте на `1 / T_poll` за секунду на воркер,
у режимі LISTEN — лише на пробудження за стелею `LISTEN_TIMEOUT_S`.

## Перевірки після вимірювань

### Крок обробки проти незалежної реалізації

Друга реалізація того самого кроку, написана інакше (квантилі сортуванням замість
гістограми, плаваюча кома замість цілочисельного округлення), має давати ті самі `p_lo`,
`p_hi` і кількість вилучених пікселів:

```powershell
.\.venv\Scripts\python.exe -m scripts.xcheck_slice
```

[scripts/xcheck_slice.py](scripts/xcheck_slice.py) проганяє шість `seed` і друкує для
кожного `p_lo`, `p_hi`, кількість вилучених, різницю середніх і кількість пікселів, що
лягли рівно на половину. Код виходу 0, якщо `p_lo`, `p_hi` і кількості збіглися скрізь.

Середнє і стандартне відхилення можуть розійтися на тисячні, і скрипт показує, чому саме:
вбудована `round()` у Python округлює рівно половину до парного, а формула
`(2a + b) // 2b` у [worker/slice.py](worker/slice.py) — вгору, як і вимагає `⌊ ⌉` з умови.
На `seed = 999983` таких пікселів 116 із 62 932, на решті перевірених — жодного. Саме через
це перетворення зроблене цілочисельним: банкірське округлення є особливістю Python, і з ним
вимога «однаковий seed дає однаковий результат у будь-якій мові» не трималася б.

Детермінованість — двома однаковими викликами:

```powershell
.\.venv\Scripts\python.exe worker\slice.py 777 0.02 0.98
```

### Межові випадки ендпоінтів

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:18080/tasks/999999999
curl.exe -s -o NUL -w "%{http_code}`n" "http://127.0.0.1:18080/tasks?limit=0"
curl.exe -s -o NUL -w "%{http_code}`n" "http://127.0.0.1:18080/tasks?status=bogus"
```

Очікувано 404, 400 і 400. Пагінація:

```powershell
Invoke-RestMethod "http://127.0.0.1:18080/tasks?limit=2&offset=2"
```

Те, що 404 від порушення зовнішнього ключа не псує зʼєднання пулу, перевіряється серією
невдалих запитів і одразу за ними валідних — усі мають дати 202:

```powershell
1..12 | ForEach-Object { try { Invoke-WebRequest "http://127.0.0.1:18080/donor-registry/999999999/tasks" -Method Post -UseBasicParsing | Out-Null } catch { } }
1..12 | ForEach-Object { (Invoke-WebRequest "http://127.0.0.1:18080/donor-registry/5/tasks" -Method Post -UseBasicParsing).StatusCode }
```

### Коректна зупинка окремо від rollout restart

`kubectl delete pod` прибирає обʼєкт швидше, ніж із нього можна зняти код завершення. Щоб
побачити код, достатньо надіслати SIGTERM, не видаляючи под: обробник на SIGTERM у воркера
є, тому цей сигнал процесу 1 доставляється (на відміну від SIGSTOP із кроку 5).

```powershell
$p = kubectl --context kind-hpc get pods -n donors -l app=worker -o jsonpath="{.items[0].metadata.name}"
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "kill -TERM 1"
kubectl --context kind-hpc get pod -n donors $p -o jsonpath="{.status.containerStatuses[0].lastState.terminated.exitCode}"
kubectl --context kind-hpc logs -n donors $p -c worker --previous --tail=3
```

Очікувано код **0** і рядки `SIGTERM: припиняю захоплення нових задач` та
`зупинка завершена` з `graceful: true`.

### Стан стенду чистий

Після прогонів перевизначень режимів у Deployment лишатись не має — інакше наступний
прогін піде в чужому режимі:

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run reset --workers 1
kubectl --context kind-hpc set env deployment/worker -n donors --list
```

Друга команда має показати лише коментарі. `reset_worker_env` сама перевіряє це і
зупиняється з помилкою, якщо щось лишилось.

### Шляхи, що не спрацьовують у звичайних прогонах

Задача триває ~100 мс, тому бюджет коректної зупинки і перехід у `failed` прибирачем у
звичайному прогоні не задіюються. Перевіряються так.

**Повернення задачі за вичерпанням бюджету зупинки.** Довга задача і нульовий бюджет:

```powershell
kubectl --context kind-hpc set env deployment/worker -n donors SLICE_SIZE=2048 LEASE_S=3 GRACE_S=0
kubectl --context kind-hpc rollout status deployment/worker -n donors
1..3 | ForEach-Object { Invoke-WebRequest "http://127.0.0.1:18080/donor-registry/5/tasks" -Method Post -UseBasicParsing | Out-Null }
$p = kubectl --context kind-hpc get pods -n donors -l app=worker -o jsonpath="{.items[0].metadata.name}"
kubectl --context kind-hpc exec -n donors $p -c worker -- sh -c "kill -TERM 1"
kubectl --context kind-hpc logs -n donors $p -c worker --previous --tail=6
kubectl --context kind-hpc set env deployment/worker -n donors SLICE_SIZE- LEASE_S- GRACE_S-
```

Очікувано: рядок `зупинка: задача повернута в чергу` з `rows: 1`, код завершення 0, задача
в базі `queued` з незмінним `token`.

**Перехід у `failed` прибирачем lease.** Задача з вичерпаними спробами і спливлим lease:

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "insert into tasks (record_id, status, seed, attempts, max_attempts, token, lease_until, worker_id, started_at) values (5,'running',777,3,3,1, now() - interval '10 s','fake', now())"
```

Через 5 секунд її статус має стати `failed` із заданим `finished_at`. Те саме з
`attempts = 1` має дати `queued`.

**Задача, повернена без вставки, у режимі LISTEN.** `NOTIFY` шле лише тригер на вставку,
тому така задача чекає стелі `LISTEN_TIMEOUT_S`:

```powershell
kubectl --context kind-hpc set env deployment/worker -n donors WAIT_MODE=listen
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "update tasks set status='queued', worker_id=null, started_at=null, finished_at=null where id=<id виконаної задачі>"
```

Очікувано: задача підхоплена менш ніж за секунду; для задачі, створеної через API, те саме
очікування становить десятки мілісекунд.

**Повторність міграцій.** `scripts\k8s_up.ps1` видаляє і застосовує обидва Job заново;
обидва мають завершитись `condition met` на вже накоченій схемі.

## Що лишилось осторонь

- **Вимірювач `worker_config`** віддає налаштування воркера мітками: за ним у Prometheus видно,
  у якому режимі йшов кожен прогін. Умова цього не вимагає, але без нього прогони кроків 3, 4
  і 10 не відрізнити один від одного у вже знятих даних.
- **Лічильник `worker_db_queries_total`** із міткою виду запиту. Потрібен вимозі Д5: без нього
  «кількість запитів до бази за секунду за порожньої черги» довелось би оцінювати за
  `pg_stat_statements`, якого в образі бази немає.
- **Лічильник `tasks_lease_requeued_total`** у сервісі: скільки задач повернуто в чергу за
  спливлим lease і скільки з них пішло у `failed`.
