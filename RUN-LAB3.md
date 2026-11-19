# Запуск лабораторної роботи 3 (варіант 19)

Параметри варіанта 19: **R = 4** реплік, кластер **kind** (один вузол control-plane і два worker),
`requests.cpu` **500m**, `limits.cpu` **1**, цільове використання CPU для HPA **50 %**, рівень
стрибка **min(1,5·X(1), 0,9·X(4))**, додаткова вимога **Д1** (`topologySpreadConstraints`).
Розмір пулу зʼєднань **10** узято з варіанта лабораторної роботи 2.

Команди подано для PowerShell з кореня репозиторію. Детальна інструкція з перевірками:
[HOWTO-LAB3.md](HOWTO-LAB3.md).

## 0. Що мати встановленим

`docker` (Docker Desktop запущений), `kind`, `kubectl`, `helm`. Перевірка:

```powershell
docker info --format "{{.NCPU}} CPU, {{.MemTotal}} bytes"; kind version; kubectl version --client; helm version --short
```

Поди web мають `requests.cpu` 500m, і за Д1 чотири поди стають по два на кожен worker-вузол,
тобто 1 ядро запитів на вузол. Разом із базою, Prometheus і kube-state-metrics на control-plane
стенду потрібно **не менше 6 CPU**, виділених Docker Desktop. Менше — і частина подів лишиться
в стані `Pending` з причиною `Insufficient cpu`.

Пароль бази береться з `.env` (той самий файл, що й у лабораторній роботі 2):

```powershell
copy .env.example .env   # і задати POSTGRES_PASSWORD
```

## 1. Розгортання (кроки 1–5)

```powershell
powershell -File scripts\k8s_up.ps1
```

Скрипт: створює кластер за [k8s/kind-cluster.yaml](k8s/kind-cluster.yaml) з перенаправленням
`30080 -> localhost:8080` і `30090 -> localhost:9090`, ставить на control-plane мітку
`role=infra`, збирає образ `donor-registry:lab3` і завантажує його в кластер, створює Secret
`db-secret` з `.env` і ConfigMap `db-schema` з [db/schema.sql](db/schema.sql), застосовує
[k8s/base](k8s/base), чекає базу, Job міграції і под web, потім ставить metrics-server
(з `--kubelet-insecure-tls`), `kube-prometheus-stack`, `prometheus-postgres-exporter` і PodMonitor.

Порти хоста — **8080** (сервіс) і **9090** (Prometheus), як вимагає умова. Опубліковані
вимірювання в `results/lab3/` зняті, коли кластер ще був створений із портами 18080 і 19090;
на шлях запиту всередині кластера (NodePort 30080, kube-proxy, поди) номер порту хоста не
впливає.

### 0. Звільнити порт 8080

Порт 8080 на машині, де робилась робота, тримає служба Windows **NI Application Web Server**
(`NIApplicationWebServer`, програма `ApplicationWebServer.exe` від National Instruments, запуск
автоматичний). Поки вона працює, `kind create cluster` падає з `ports are not available ...
bind`. Перед створенням кластера її треба зупинити від імені адміністратора, а щоб вона не
займала порт після перезавантаження — перевести на ручний запуск:

```powershell
Stop-Service NIApplicationWebServer
```

```powershell
Set-Service NIApplicationWebServer -StartupType Manual
```

Повернути, коли кластер не потрібен:

```powershell
Set-Service NIApplicationWebServer -StartupType Automatic; Start-Service NIApplicationWebServer
```

Перевірити, що 8080 і 9090 вільні (порожній вивід):

```powershell
Get-NetTCPConnection -LocalPort 8080,9090 -State Listen -ErrorAction SilentlyContinue
```

Повне видалення кластера:

```powershell
powershell -File scripts\k8s_down.ps1
```

Версії чартів записати в звіт:

```powershell
helm list -A
```

## 1a. Самоперевірка

```powershell
powershell -File scripts\lab3_selfcheck.ps1
```

Проходить усі 15 перевірок кроків 1–5 і Д1 та друкує ПРОЙДЕНО / НЕ ПРОЙДЕНО по кожній.
Нижче ті самі перевірки вручну.

## 2. Перевірка бази (крок 3)

Дані мають зберегтися після видалення пода:

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "insert into donor_registry (donor_code, birth_year, blood_group, hla_typing, registered_on, available) values ('PERSIST-1', 1990, 'O+', '{A1}', current_date, true)"
kubectl delete pod db-0 -n donors
kubectl rollout status statefulset/db -n donors
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "select count(*) from donor_registry where donor_code = 'PERSIST-1'"
```

`max_connections` і відсутність пароля в ConfigMap:

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "show max_connections"
kubectl get configmap web-config -n donors -o yaml
```

## 3. Перевірка сервісу (крок 4)

Повний цикл через NodePort; заголовок `X-Pod` показує под, що обробив запит:

```powershell
$body = '{"donor_code":"K8S-1","birth_year":1990,"blood_group":"O+","hla_typing":["A1"],"registered_on":"2026-01-01","available":true}'
$r = Invoke-WebRequest -Uri http://127.0.0.1:8080/donor-registry -Method Post -ContentType application/json -Body $body
$r.Headers["X-Pod"]; $id = ($r.Content | ConvertFrom-Json).id
(Invoke-WebRequest "http://127.0.0.1:8080/donor-registry/$id").Headers["X-Pod"]
Invoke-WebRequest "http://127.0.0.1:8080/donor-registry/$id" -Method Put -ContentType application/json -Body ($body -replace '"available":true','"available":false')
Invoke-WebRequest "http://127.0.0.1:8080/donor-registry/$id" -Method Delete
```

Поля пода, Д1 і коректна зупинка. Поле `replicas` перевіряється саме в маніфесті: `kubectl get`
показав би поточну кількість реплік, яку підставив сервер, а вимога стосується файла.

```powershell
Select-String -Path k8s\base\21-web-deployment.yaml -Pattern "^\s*replicas:"   # порожньо: поля немає
kubectl get pods -n donors -l app=web -o wide                               # розподіл по вузлах
kubectl describe deployment web -n donors | Select-String "preStop|Grace|topology|cpu|memory"
```

## 4. Перевірка моніторингу (крок 5)

```powershell
kubectl top pods -n donors
kubectl get podmonitor,servicemonitor -A
```

Prometheus на http://127.0.0.1:9090 — перевірити, що є ряди:

| Запит | Що показує |
|---|---|
| `sum by (pod) (rate(http_requests_total[30s]))` | запити за секунду на кожен под web |
| `container_cpu_cfs_periods_total{namespace="donors",container="web"}` | лічильник періодів CFS |
| `container_cpu_cfs_throttled_periods_total{namespace="donors",container="web"}` | лічильник дроселювання |
| `kube_deployment_status_replicas_ready{namespace="donors",deployment="web"}` | кількість готових реплік |
| `pg_up` | postgres-exporter |

Частка дроселювання за проміжок:
`rate(container_cpu_cfs_throttled_periods_total{container="web"}[1m]) / rate(container_cpu_cfs_periods_total{container="web"}[1m])`.

## 5. Серія кроку 7

Перед серією HPA має бути видалений. Масштабування, очікування подів і збір метрик робить сам
скрипт прогону — по одному запуску на кожне N:

```powershell
kubectl delete hpa web -n donors --ignore-not-found
.\.venv\Scripts\python.exe -m loadgen.run_k8s --replicas 1 --max-rate 700
.\.venv\Scripts\python.exe -m loadgen.run_k8s --replicas 2 --max-rate 700
.\.venv\Scripts\python.exe -m loadgen.run_k8s --replicas 3 --max-rate 700
.\.venv\Scripts\python.exe -m loadgen.run_k8s --replicas 4 --max-rate 700
```

Профіль ЛР3 задає сам `run_k8s`: сходинка 30 с із середніми за останні 20 с, 32 постійних
зʼєднання по 100 запитів, сходинки по 50 rps (прапорці `--rate0` і `--rate-step`). Сходинка
дрібніша за 200 rps із ЛР2, бо в кластері X(N) у кілька разів менші, і на грубій сітці
похибка робила підбір σ і κ беззмістовним (пояснення у звіті). `--max-rate 700` це межа стенду,
виміряна на `/livez`; вона ж зупиняє прогін, якщо сервіс її дістає.

Кожен прогін дає `results/lab3/N<k>.json` (зведення, сходинки, метрики Prometheus за кожною
сходинкою: rps на под, дисбаланс, p99 сервера, дроселювання, розподіл по вузлах),
`N<k>.csv` (посекундний запис) і `N<k>_prom.json` (ряди для діаграм).

Межу стенду без бази (крок 7, контроль обмеження генератором) можна перезняти так:

```powershell
.\.venv\Scripts\python.exe -m loadgen.step --base http://127.0.0.1:8080 --path /livez --out results/lab3/ladder_livez --max-steps 8
```

> У Git Bash шлях у `--path` треба брати в лапки і ставити `MSYS_NO_PATHCONV=1`, інакше `/livez`
> перетвориться на шлях Windows і сервіс відповідатиме 400. У PowerShell такого немає.

## 6. Прогони за профілем (кроки 10 і 11)

X(1) і X(R) беруться з результатів кроку 7 за визначенням (найбільша серед сходинок середня
досягнута, `results/lab3/tables.md`); рівні профілю скрипт рахує сам: база 0,3·X(1) ≈ 81 rps,
стрибок min(1,5·X(1), 0,9·X(R)) ≈ 404 rps. Опубліковані прогони `profile_fixed` і `profile_hpa`
зняті раніше з `--x1 240.7 --xr 377.1` (власне правило), тобто з рівнями 72 і 339 rps — див.
розділ «Кроки 10 і 11. Прогони за профілем» звіту.

```powershell
.\.venv\Scripts\python.exe -m loadgen.run_elastic --mode fixed --replicas 4 --x1 269.15 --xr 472.95
.\.venv\Scripts\python.exe -m loadgen.run_elastic --mode hpa --x1 269.15 --xr 472.95
```

Режим `fixed` знімає HPA і фіксує 4 репліки, режим `hpa` масштабує до однієї репліки, застосовує
[k8s/hpa/web-hpa.yaml](k8s/hpa/web-hpa.yaml), чекає стабільної однієї репліки 60 с і після прогону
HPA видаляє. Результати: `profile_fixed.*` і `profile_hpa.*` разом із рядами Prometheus
(готові й бажані репліки, CPU відносно `requests.cpu`).

## 7. Таблиці, метрики еластичності і діаграми (кроки 8, 9, 12)

```powershell
.\.venv\Scripts\python.exe scripts\lab3_tables.py
.\.venv\Scripts\python.exe scripts\lab3_elastic.py
.\.venv\Scripts\python.exe scripts\lab3_charts.py
```

Дають `results/lab3/tables.md` (X(N), дисбаланс, Д1, σ і κ, порівняння з Compose),
`elasticity.md` (θ_U, θ_O, a_U, a_O, W, порушення порога і часові показники HPA) і чотири
діаграми в `results/lab3/charts/`. Усі числа звіту беруться саме з цих файлів.
