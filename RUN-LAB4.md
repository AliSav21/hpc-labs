# Запуск лабораторної роботи 4 (варіант 19)

Параметри варіанта 19: квантилі вікна яскравості **q_lo = 0,02 / q_hi = 0,98**, інтервал
опитування **T_poll = 50 мс**, тривалість lease **L = 5 с**, сценарій відмови **Ф3** (10 задач
із `seed = 0`, на якому крок обробки викидає виняток), додаткова вимога **Д5**
(LISTEN/NOTIFY замість опитування). Найбільша кількість воркерів **R = 4** узята з ЛР3, розмір
зрізу **S = 256** однаковий для всіх варіантів.

Стенд той самий, що в ЛР3: кластер kind `hpc`, база `db-0`, сервіс `web`, kube-prometheus-stack.
ЛР4 додає міграцію `tasks` і `task_results`, ендпоінти черги в сервісі `web` і Deployment
`worker` із власним образом.

Команди подано для PowerShell з кореня репозиторію. Детальна інструкція з перевірками:
[HOWTO-LAB4.md](HOWTO-LAB4.md). Що мати встановленим

Те саме, що в ЛР3: `docker` (Docker Desktop запущений), `kind`, `kubectl`, `helm`, `.env` із
заданим `POSTGRES_PASSWORD`. Воркери мають `requests.cpu` 250m, тобто чотири воркери просять ще
одне ядро понад стенд ЛР3; разом потрібно **не менше 7 CPU**, виділених Docker Desktop.

## 1. Розгортання (кроки 1–2)

```powershell
powershell -File scripts\k8s_up.ps1
```

Той самий скрипт, що в ЛР3, доповнений кроками ЛР4: збирає образ воркера
`donor-worker:lab4` за [Dockerfile.worker](Dockerfile.worker) і завантажує його в кластер,
створює ConfigMap `db-schema-lab4` з [db/schema-lab4.sql](db/schema-lab4.sql), виконує Job
`db-migrate-lab4`, застосовує `worker-config`, Deployment `worker` і PodMonitor `worker`.

Образ сервісу лишається з тим самим тегом `donor-registry:lab3`: це той самий сервіс, якому
ЛР4 додала ендпоінти черги задач. Після збірки сервіс треба перезапустити, щоб под узяв новий
образ:

```powershell
kubectl rollout restart deployment/web -n donors; kubectl rollout status deployment/web -n donors
```

Сто записів сутності заносяться один раз (ті самі, що в ЛР1–ЛР3; їхні id лежать у
`loadgen/ids.json`):

```powershell
.\.venv\Scripts\python.exe -m loadgen.seed --base http://127.0.0.1:18080
```

## 1a. Самоперевірка

```powershell
powershell -File scripts\lab4_selfcheck.ps1
```

Проходить перевірки кроків 1–2: таблиці й індекси, зовнішній ключ, тригер NOTIFY, три
ендпоінти черги, вимірювачі `tasks_queued` і `tasks_running`, метрики воркера, збір подів
`worker` у Prometheus, ліміти й `terminationGracePeriodSeconds`, `WORKER_ID` з Downward API.

## 2. Крок 2: t_io і t_cpu на одному воркері

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step2
```

20 задач на одному воркері. Результат: `results/lab4/step2.json`. Кожен прогін генератора заразом пише перелік
ідентифікаторів створених задач у `<прогін>_gen_task_ids.json`, як вимагає крок 2.

## 3. Крок 3: наївне захоплення

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step3
```

Дві серії по 300 задач з інтенсивністю 100 за секунду на R = 4 воркерах: з `T_poll` варіанта
(50 мс) і з 1 мс. Скрипт сам перемикає воркер у режим `CLAIM_MODE=naive`, `RESULT_MODE=insert`,
`LEASE_MODE=off` і тимчасово заміняє первинний ключ `task_results` звичайним індексом.
Результат: `results/lab4/step3_poll50.json`, `step3_poll1.json`, `step3.json`.

## 4. Крок 4: FOR UPDATE SKIP LOCKED і FOR UPDATE

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step4 --repeat 3
```

Та сама серія з `T_poll = 1 мс` спочатку з `SKIP LOCKED`, потім без нього, **по три прогони
на режим**. Одного прогону на режим, як каже умова, недосить: між першими двома серіями
висновок змінювався на протилежний за всіма показниками, тож у звіті наведено розмах по
повторах. Під час прогону щосекунди знімаються `pg_stat_activity` з
`wait_event_type = 'Lock'` і `pg_locks` з `granted = false`; увесь цикл виконується одним
`kubectl exec` усередині пода бази. Результат: `results/lab4/step4_<режим>_r1..r3.json`.

Щоб показати, що зафіксовані очікування блокувань створює тригер `NOTIFY` вимоги Д5, а не
захоплення задачі, та сама пара прогонів повторюється з вимкненим тригером:

```powershell
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "alter table tasks disable trigger tasks_notify_insert"
.\.venv\Scripts\python.exe -m scripts.lab4_run step4 --repeat 1
kubectl --context kind-hpc exec -n donors db-0 -c postgres -- psql -U donors -d donors -c "alter table tasks enable trigger tasks_notify_insert"
```

Файли цих двох прогонів перейменовуються на `step4_<режим>_nonotify.json`, щоб таблиця
відрізняла їх від звичайних повторів.

## 5. Крок 5: lease, heartbeat і fencing token

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step5
```

Два воркери, 100 задач у черзі; один воркер зупиняється сигналом SIGSTOP на 2 · L = 10 с і
відновлюється SIGCONT. Результат: `results/lab4/step5.json`.

Команда з умови `kubectl exec ... -- kill -STOP 1` на цьому стенді **не діє**: воркер є процесом
1 у просторі імен контейнера, а ядро не доставляє init простору імен сигнал, надісланий
зсередини цього ж простору, якщо для сигналу немає обробника — для SIGSTOP обробника не буває.
Скрипт пробує команду з умови, перевіряє стан процесу в `/proc/1/stat` і, якщо той не змінився,
надсилає той самий сигнал із простору імен вузла kind:

```powershell
docker exec hpc-worker kill -STOP <pid процесу воркера на вузлі>
```

## 6. Крок 6: X(W) на 1, 2 і R воркерах

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step6
.\.venv\Scripts\python.exe -m scripts.lab4_run step6 --count 1500 --tag _long
```

Перша команда — прогін за умовою: 400 задач у черзі перед прогоном. Друга — додатковий прогін
на 1500 задач: за 400 задач черга при W = 2 і W = 4 спорожнюється швидше, ніж за 30 с, і вікно
вимірювання виходить коротшим за те, що вимагає умова. Результат: `results/lab4/step6_W*.json`,
`step6_long_W*.json`.

Пропускна здатність цього стенду між прогонами гуляє на 19–24 %, і через це параметри закону
масштабованості за одним прогоном не визначаються. Тому знімається ще серія повторів, і X(W)
береться як середнє:

```powershell
1..3 | ForEach-Object { .\.venv\Scripts\python.exe -m scripts.lab4_run step6 --count 1000 --workers 1,4 --tag "_s$_" }
```

Результат: `results/lab4/step6_s1_W1.json` і далі. Якщо потрібно перерахувати X(W) вже
знятих прогонів (наприклад після зміни правила вікна), посекундний ряд для цього збережено
в самих файлах:

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run recompute_x --names step6_long_W1,step6_long_W2,step6_long_W4
```

## 7. Крок 7: закон Літтла

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step7 --xr 29.1
```

`--xr` це X(R) із кроку 6. У наведених прогонах використано значення **29,1** задач/с —
оцінку, яку крок 6 дав на початку роботи. Уточнене середнє за пʼятьма прогонами становить
28,67 задач/с, тож фактично інтенсивності відповідали 0,71 і 1,32 від нього; на висновки
різниця в 1,6 % не впливає, і прогони не перезнімались. Два прогони: 0,7 · X(R) протягом 180 с з вікном аналізу 120 с і
1,3 · X(R) протягом 120 с. Результат: `results/lab4/step7_low.json`, `step7_high.json`,
`step7.json`.

## 8. Крок 8: коректна зупинка

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step8 --xr 29.1
```

Під прогоном з інтенсивністю 0,7 · X(R) виконується `kubectl rollout restart
deployment/worker`. Коди завершення контейнерів і журнали старих подів знімаються потоково,
доки поди існують. Результат: `results/lab4/step8.json`.

## 9. Крок 9: FAIL_RATE = 0,15 і сценарій Ф3

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step9 --xr 29.1
```

Три прогони по 300 с з інтенсивністю 0,7 · X(R) і `FAIL_RATE = "0.15"`:

| Варіант | Що в ньому | Навіщо |
|---|---|---|
| `k8s` | Deployment як є, падіння лікує kubelet | прогін за умовою; показує CrashLoopBackOff |
| `poison` | наглядач процесу воркера + 10 задач із `seed = 0` на 150-й секунді | сценарій Ф3 |
| `clean` | наглядач процесу, той самий FAIL_RATE, без отруєних задач | база для порівняння p99 |

Варіант `k8s` зупиняється не через повторну обробку задач, а через паузу kubelet перед
перезапуском контейнера: на 0,7 · X(R) ≈ 20 задач/с і FAIL_RATE = 0,15 падіння трапляється
близько трьох разів за секунду на пул, і CrashLoopBackOff доходить до 5 хвилин. Щоб зняти
розподіл задач за `attempts`, два інші варіанти ставлять процесом 1 контейнера наглядача
([worker/supervise.py](worker/supervise.py)): механізм падіння лишається тим самим
(`os._exit(1)` посеред задачі, зʼєднання не закриваються, задача лишається в `running` до
спливання lease), але воркер повертається за час запуску процесу. Вмикається змінною
`SUPERVISE=1`.

На інтенсивності 0,7 · X(R) пул із `FAIL_RATE = 0,15` не встигає, черга росте, і отруєні
задачі за 300 секунд просто не доходять до воркера. Тому сценарій Ф3 знімається ще раз на
інтенсивності, яку пул витримує:

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step9 --xr 29.1 --variants poison,clean --rate 5 --suffix _lo --drain 300
```

Саме ці два прогони дають статус отруєних задач, їхні `attempts` і порівняння p99 очікування
решти задач з прогоном без отруєних.

Окремі варіанти: `--variants k8s` або `--variants poison,clean`.
Результат: `results/lab4/step9_k8s.json`, `step9_poison.json`, `step9_clean.json`,
`step9_poison_lo.json`, `step9_clean_lo.json`, `step9.json`.

## 10. Крок 10 (вимога Д5): LISTEN/NOTIFY

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run step10
```

Два прогони на R воркерах з λ = 5 задач за секунду: з опитуванням 100 мс і з LISTEN. Перед
кожним — 25 с із порожньою чергою для заміру кількості запитів до бази. Результат:
`results/lab4/step10_poll100.json`, `step10_listen.json`, `step10.json`.

## 11. Таблиці і діаграми

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_tables
.\.venv\Scripts\python.exe -m scripts.lab4_charts
```

Таблиці — `results/lab4/tables.md`, діаграми — `results/lab4/charts/`.

## 12. Повернути стенд у початковий стан

```powershell
.\.venv\Scripts\python.exe -m scripts.lab4_run reset --workers 1
```

Знімає всі перевизначення режимів воркера (він повертається до значень `worker-config`),
повертає первинний ключ `task_results`, очищає чергу і лишає один воркер.
