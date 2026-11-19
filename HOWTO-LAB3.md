# Як цим користуватися (лабораторна робота 3)

Детальна інструкція до стенду в Kubernetes: як підняти, як перевірити, що все працює
належним чином, і як переконатися, що зроблено саме те, що вимагає завдання.
Коротка інструкція запуску: [RUN-LAB3.md](RUN-LAB3.md). Інструкція до стенду ЛР1: [HOWTO.md](HOWTO.md).

Усі команди виконуються **з кореня репозиторію** в PowerShell.

**Параметри варіанта 19**, за якими перевіряється все інше:

| Параметр | Значення | Де задано |
|---|---|---|
| R, найбільша кількість реплік | **4** | `k8s/hpa/web-hpa.yaml`, `maxReplicas` |
| Кластер | **kind**, 1 control-plane + 2 worker | `k8s/kind-cluster.yaml` |
| `requests.cpu` / `limits.cpu` | **500m / 1** | `k8s/base/21-web-deployment.yaml` |
| `limits.memory` | 512Mi | там само |
| Цільове використання CPU для HPA | **50 %** | `k8s/hpa/web-hpa.yaml` |
| Рівень стрибка | **min(1,5·X(1), 0,9·X(4))** | буде в генераторі (крок 10) |
| Розмір пулу зʼєднань | **10** (з ЛР2) | `k8s/base/20-web-configmap.yaml` |
| Додаткова вимога | **Д1**, `topologySpreadConstraints` | `k8s/base/21-web-deployment.yaml` |

---

## 1. Що де лежить

| Шлях | Що це |
|---|---|
| `k8s/kind-cluster.yaml` | конфігурація кластера: control-plane + два worker, перенаправлення портів |
| `k8s/base/00-namespace.yaml` | namespace `donors` |
| `k8s/base/10-db-service.yaml` | headless Service бази (стале імʼя `db-0.db...`) |
| `k8s/base/11-db-statefulset.yaml` | база: StatefulSet, PVC, `max_connections=100`, вузол `role=infra` |
| `k8s/base/12-db-migrate-job.yaml` | Job міграції схеми |
| `k8s/base/20-web-configmap.yaml` | `DB_POOL_SIZE`, адреса бази, користувач і база (без пароля) |
| `k8s/base/21-web-deployment.yaml` | сервіс web: ресурси, проби, `preStop`, Downward API, Д1 |
| `k8s/base/22-web-service.yaml` | Service типу NodePort (30080) |
| `k8s/hpa/web-hpa.yaml` | HPA; **навмисно окремо** від `k8s/base`, бо серія кроку 7 йде без нього |
| `k8s/monitoring/kube-prometheus-stack.values.yaml` | values Helm-чарта Prometheus + kube-state-metrics |
| `k8s/monitoring/postgres-exporter.values.yaml` | values чарта postgres-exporter |
| `k8s/monitoring/web-podmonitor.yaml` | пошук подів web для Prometheus |
| `scripts/k8s_up.ps1` | **одна** команда розгортання (крок 2) |
| `scripts/k8s_down.ps1` | повне видалення кластера (крок 2) |
| `.env` | **локальний**, не в git: пароль бази |
| `app/`, `db/`, `Dockerfile`, `loadgen/` | без змін із ЛР1–ЛР2 |

Усе, що стосується Compose (`docker-compose.yml`, `lb/`, `prometheus/`), лишається для ЛР2 і в ЛР3
не використовується.

---

## 2. Разова підготовка

**1. Docker Desktop** запущений і має **не менше 6 CPU**. Чому: за Д1 чотири поди web по
`requests.cpu` 500m стають по два на кожен worker (1 ядро запитів на вузол), плюс база,
Prometheus і kube-state-metrics на control-plane. Перевірка:

```powershell
docker info --format "{{.NCPU}} CPU, {{.MemTotal}} bytes"
```

Якщо ядер менше, у Docker Desktop: Settings -> Resources -> CPUs. Симптом нестачі — поди в
стані `Pending` з причиною `Insufficient cpu`.

**Порти 8080 і 9090.** Кластер створюється з портами хоста з умови.

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


**2. Інструменти.** Потрібні `kind`, `kubectl` і `helm`:

```powershell
winget install -e --id Kubernetes.kind; winget install -e --id Kubernetes.kubectl; winget install -e --id Helm.Helm
```

Після встановлення відкрити нове вікно термінала і перевірити:

```powershell
kind version; kubectl version --client; helm version --short
```

**3. Файл `.env`** — той самий, що в ЛР2 (пароль бази). Якщо його ще немає:

```powershell
copy .env.example .env
```

і замінити `change-me` на свій пароль. Скрипт розгортання відмовиться працювати, поки там
лишається `change-me`.

---

## 3. Стенд: підняти, перевірити, видалити

### Підняти все одною командою

```powershell
powershell -File scripts\k8s_up.ps1
```

Перший запуск триває довго (створення кластера, збірка образу, завантаження чартів) — орієнтовно
10–20 хвилин. Команда безпечна для повторного запуску: кластер, якщо вже є, не створюється
наново, маніфести застосовуються повторно, Helm робить `upgrade --install`.

Без моніторингу (швидше, для перевірки лише сервісу):

```powershell
powershell -File scripts\k8s_up.ps1 -SkipMonitoring
```

### Перевірити, що живий

```powershell
kubectl get nodes -o wide; kubectl get pods -A -o wide
```

Очікується: три вузли в стані `Ready`, усі поди `Running` (крім `db-migrate`, який має бути
`Completed`), жодного `Pending`, `CrashLoopBackOff` чи `ImagePullBackOff`.

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/healthz).Content
```

Має бути `{"status":"ok"}`.

### Видалити повністю

```powershell
powershell -File scripts\k8s_down.ps1
```

Видаляє кластер разом із томами бази й моніторингом. Образ `donor-registry:lab3` лишається в
Docker на хості.

### Подивитись логи

```powershell
kubectl logs -n donors -l app=web --tail 50
```

```powershell
kubectl get events -n donors --sort-by=.lastTimestamp | Select-Object -Last 20
```

---

## 4. Перевірка за кроками завдання

Головний розділ: для кожної вимоги є команда і те, що має вийти.

### Крок 1. Конфігурація кластера

```powershell
kubectl get nodes -L role
```

Очікується: `hpc-control-plane` з `ROLES=control-plane` і `ROLE=infra`, два `hpc-worker*` з
`ROLES=<none>`. Перенаправлення портів:

```powershell
docker port hpc-control-plane
```

Має показати `30080 -> 127.0.0.1:8080` і `30090 -> 127.0.0.1:9090`.

### Крок 2. Одна команда розгортання і одна видалення

Перевірка — повний цикл з нуля: `scripts\k8s_down.ps1`, потім `scripts\k8s_up.ps1` і знову
перевірки цього розділу. Мітка `role=infra` ставиться самим скриптом:

```powershell
kubectl get node -l role=infra
```

### Крок 3. База

Чи на тому вузлі і чи з tolerations:

```powershell
kubectl get pod db-0 -n donors -o wide
```

```powershell
kubectl get pod db-0 -n donors -o jsonpath="{.spec.nodeSelector}{'\n'}{.spec.tolerations}{'\n'}"
```

`max_connections`, PVC, Secret, Job:

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "show max_connections"
```

```powershell
kubectl get pvc,secret,job -n donors
```

Очікується `100`, PVC у стані `Bound`, Secret `db-secret`, Job `COMPLETIONS 1/1`. Таблиця на місці:

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "\d donor_registry"
```

**Персистентність після видалення пода** (окрема вимога кроку 3):

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "insert into donor_registry (donor_code, birth_year, blood_group, hla_typing, registered_on, available) values ('PERSIST-1', 1990, 'O+', '{A1}', current_date, true)"
```

```powershell
kubectl delete pod db-0 -n donors; kubectl rollout status statefulset/db -n donors --timeout=300s
```

```powershell
kubectl exec -n donors db-0 -- psql -U donors -d donors -c "select count(*) from donor_registry where donor_code = 'PERSIST-1'"
```

Має вийти `1`. Якщо `0` — дані лежать не на PVC, і це помилка маніфеста.

### Крок 4. Сервіс web

Поля `replicas` у маніфесті **не має бути** — порожній результат пошуку це правильно:

```powershell
Select-String -Path k8s\base\21-web-deployment.yaml -Pattern "^\s*replicas:"
```

> Увага: `kubectl get deployment web -o yaml` **покаже** `replicas`, бо це поточний стан, який
> підставив сервер. Вимога стосується саме маніфеста.

Ресурси, проби, зупинка:

```powershell
kubectl get deployment web -n donors -o jsonpath="{range .spec.template.spec.containers[*]}{.resources}{'\n'}{.readinessProbe}{'\n'}{.livenessProbe}{'\n'}{.lifecycle}{'\n'}{end}"
```

```powershell
kubectl get deployment web -n donors -o jsonpath="{.spec.template.spec.terminationGracePeriodSeconds}{'\n'}"
```

Очікується: `requests` 500m CPU, `limits` 1 CPU і 512Mi, `readinessProbe` на `/healthz` з
`timeoutSeconds 3`, `periodSeconds 5`, `failureThreshold 3`, `livenessProbe` на `/livez`,
`preStop.sleep.seconds 5`, `terminationGracePeriodSeconds 15`.

Крім того, у поді є `startupProbe` і `initContainer wait-db` — без них на свіжому кластері
сервіс не піднімається (розділ «Холодний старт» звіту):

```powershell
kubectl get deployment web -n donors -o jsonpath="{.spec.template.spec.containers[0].startupProbe}{'
'}{.spec.template.spec.initContainers[0].name}{'
'}"
```

Має вивести пробу на `/livez` з `failureThreshold 36` і імʼя `wait-db`.

**Пароля не має бути ні в образі, ні в ConfigMap, ні в репозиторії:**

```powershell
kubectl get configmap web-config -n donors -o yaml | Select-String -Pattern "(?i)password"
```

```powershell
docker run --rm --entrypoint env donor-registry:lab3 | Select-String PASSWORD
```

```powershell
git check-ignore -v .env
```

Перші дві команди не повинні вивести нічого, `git check-ignore` має показати правило з
`.gitignore`. Пароль у поді приходить лише через `secretKeyRef`:

```powershell
kubectl get deployment web -n donors -o jsonpath="{.spec.template.spec.containers[0].env}" | Select-String secretKeyRef
```

**Повний цикл CRUD через `localhost:8080` із заголовком `X-Pod`:**

```powershell
$body = '{"donor_code":"K8S-1","birth_year":1990,"blood_group":"O+","hla_typing":["A1"],"registered_on":"2026-01-01","available":true}'; $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8080/donor-registry -Method Post -ContentType application/json -Body $body; "POST -> $($r.StatusCode) под $($r.Headers['X-Pod'])"; $id = ($r.Content | ConvertFrom-Json).id; $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8080/donor-registry/$id"; "GET -> $($r.StatusCode) под $($r.Headers['X-Pod'])"; $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8080/donor-registry/$id" -Method Put -ContentType application/json -Body ($body -replace '"available":true','"available":false'); "PUT -> $($r.StatusCode) под $($r.Headers['X-Pod'])"; $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8080/donor-registry/$id" -Method Delete; "DELETE -> $($r.StatusCode) под $($r.Headers['X-Pod'])"
```

Очікується `201`, `200`, `200`, `204` і непорожнє імʼя пода в кожному рядку. На кількох репліках
імена в різних рядках можуть відрізнятися — це і є доказ, що запити розходяться по подах.

### Д1. Рівномірний розподіл по вузлах

```powershell
kubectl scale deployment/web -n donors --replicas=4; kubectl rollout status deployment/web -n donors
```

```powershell
kubectl get pods -n donors -l app=web -o custom-columns="POD:.metadata.name,NODE:.spec.nodeName"
```

На чотирьох репліках має бути **2 + 2** по двох worker-вузлах. Саме обмеження:

```powershell
kubectl get deployment web -n donors -o jsonpath="{.spec.template.spec.topologySpreadConstraints}{'\n'}"
```

`maxSkew 1`, `topologyKey kubernetes.io/hostname`, `whenUnsatisfiable DoNotSchedule`.

### Крок 5. Моніторинг

```powershell
kubectl top pods -n donors; kubectl top nodes
```

```powershell
kubectl get pods -n monitoring -o wide; kubectl get podmonitor,servicemonitor -A
```

`kubectl top` має повертати числа (а не `error: Metrics API not available`) — це перевірка
metrics-server. Усі поди моніторингу мають бути на вузлі `hpc-control-plane`.

Далі Prometheus на http://127.0.0.1:9090 -> Graph. Перевірити кожен ряд:

| Запит | Що має бути |
|---|---|
| `sum by (pod) (rate(http_requests_total{namespace="donors"}[30s]))` | окремий ряд **на кожен** под web |
| `container_cpu_cfs_periods_total{namespace="donors",container="web"}` | лічильник на кожен под |
| `container_cpu_cfs_throttled_periods_total{namespace="donors",container="web"}` | те саме |
| `kube_deployment_status_replicas_ready{namespace="donors",deployment="web"}` | один ряд, значення = кількість готових подів |
| `pg_up` | `1` (postgres-exporter) |
| `up{job=~".*kubelet.*"}` | `1` на кожен вузол (cAdvisor через kubelet) |

Частка дроселювання CPU за проміжок (знадобиться в кроках 7–8):

```
rate(container_cpu_cfs_throttled_periods_total{namespace="donors",container="web"}[1m])
  / rate(container_cpu_cfs_periods_total{namespace="donors",container="web"}[1m])
```

Інтервал збору:

```powershell
kubectl get prometheus -n monitoring -o jsonpath="{.items[0].spec.scrapeInterval}{'\n'}"
```

Має бути `5s`. Якщо рядів `http_requests_total` немає, у Prometheus -> Status -> Targets
має бути таргет `podMonitor/donors/web/0` зі станом `UP`.

### Кроки 6–13

Виконані; команди для повторення — у розділах 5–7 [RUN-LAB3.md](RUN-LAB3.md), числа — у
[REPORT-LAB3.md](REPORT-LAB3.md). Що перевіряти в результатах:

| Де | Що має бути |
|---|---|
| `results/lab3/N<k>.json`, поле `connections` | `total` 32, `opened` сотні, `recycled` сотні — зʼєднання справді закриваються після 100 запитів (крок 6) |
| те саме, поле `profile` | `step_s` 30, `transient_s` 10 — сходинка 30 с із середніми за останні 20 с |
| те саме, поле `statuses` | переважно `200`; `-1` це таймаут, `0` помилка зʼєднання |
| `results/lab3/tables.md` | X(N), дисбаланс I, розподіл по вузлах (Д1), σ, κ, N\* і порівняння з ЛР2 |
| `results/lab3/elasticity.md` | θ_U, θ_O, a_U, a_O, W, секунди порушення порога і чотири часові показники HPA |
| `results/lab3/charts/` | чотири діаграми, зокрема `elasticity.png` з однаковими шкалами для обох прогонів |

Готовність HPA (крок 11) можна перевірити й окремо:

```powershell
kubectl apply -f k8s/hpa/web-hpa.yaml; kubectl get hpa web -n donors
```

У колонці `TARGETS` має бути щось на кшталт `3%/50%` — тобто HPA бачить метрики. Якщо там
`<unknown>/50%`, не працює metrics-server. **Перед серією кроку 7 HPA треба видалити:**

```powershell
kubectl delete hpa web -n donors
```

---

## 5. Швидкий самоконтроль перед здачею

Усе з розділу 4 виконує один скрипт — він сам порівнює вивід з очікуваним і друкує
ПРОЙДЕНО / НЕ ПРОЙДЕНО по кожному пункту:

```powershell
powershell -File scripts\lab3_selfcheck.ps1
```

Повна перевірка триває близько двох хвилин і дещо змінює стенд: видаляє под бази (перевірка
персистентності), масштабує web до 4 реплік (перевірка Д1), на 30 секунд створює HPA. Запис у
базу прибирається самим скриптом. Щоб пропустити видалення пода і масштабування:

```powershell
powershell -File scripts\lab3_selfcheck.ps1 -Quick
```

Останній рядок має бути `ПРОЙДЕНО 15, НЕ ПРОЙДЕНО 0`; код виходу 1 означає, що якийсь пункт не
зійшовся, і тоді під ним надруковано, що мало бути і що вийшло.

Те саме вручну — усі перевірки розділу 4 з очікуваним результатом:

| # | Перевірка | Очікуване |
|---|---|---|
| 1 | `kubectl get nodes -L role` | 3 вузли `Ready`, control-plane має `role=infra` |
| 2 | `docker port hpc-control-plane` | 30080 -> 8080, 30090 -> 9090 |
| 3 | `kubectl get pods -A` | немає `Pending`, `Error`, `CrashLoopBackOff` |
| 4 | `kubectl get pod db-0 -n donors -o wide` | на `hpc-control-plane` |
| 5 | `show max_connections` | 100 |
| 6 | видалення `db-0` і `select count(*)` | 1, дані збереглися |
| 7 | пошук `replicas:` у маніфесті web | нічого не знайдено |
| 8 | ресурси пода web | 500m / 1 CPU, 512Mi |
| 9 | проби і зупинка | `/healthz` 3-5-3, `/livez`, sleep 5, grace 15 |
| 10 | пароль у ConfigMap, образі, git | ніде немає, лише в Secret |
| 11 | CRUD через `localhost:8080` | 201, 200, 200, 204 і `X-Pod` у кожній відповіді |
| 12 | розподіл 4 подів по вузлах | 2 + 2 (Д1) |
| 13 | `kubectl top pods -n donors` | числа, не помилка |
| 14 | ряди в Prometheus із таблиці вище | усі присутні |
| 15 | `kubectl get hpa web -n donors` | `TARGETS` показує відсоток, не `<unknown>` |

---

## 6. Якщо щось не працює

**`error during connect ... dockerDesktopLinuxEngine`**
Docker Desktop не запущений. Відкрити і дочекатися `Engine running`.

**Поди web у `Pending`, подія `Insufficient cpu`**
Docker Desktop має менше ядер, ніж потрібно стенду. Додати CPU (розділ 2) або для перевірок
працювати на меншій кількості реплік.

**Поди web у `Pending`, подія про `topology spread constraints`**
Реплік більше, ніж вміщує рівномірний розподіл по двох воркерах, або один воркер `NotReady`.
Для R = 4 це 2 + 2 і має сходитися; перевірити `kubectl get nodes`.

**`ImagePullBackOff` на поді web**
Образ не завантажений у кластер:

```powershell
docker build -t donor-registry:lab3 .; kind load docker-image donor-registry:lab3 --name hpc; kubectl rollout restart deployment/web -n donors
```

**Под web у `CrashLoopBackOff`**
Майже завжди це база або пароль. `kubectl logs -n donors -l app=web --tail 50`:
`connection refused` — база ще не готова (сам поправиться), помилка автентифікації — пароль у
Secret не той, що в базі. Secret створюється з `.env`, а база запамʼятовує пароль **разом з
томом при першому старті**: якщо змінити `.env` після першого підйому, база лишиться зі старим
паролем. Лікується повним видаленням кластера (`scripts\k8s_down.ps1`) і новим підйомом.

**Job `db-migrate` у `BackoffLimitExceeded`**
`kubectl logs -n donors job/db-migrate`. Якщо помилка в SQL — поправити `db/schema.sql`,
видалити Job (`kubectl delete job db-migrate -n donors`) і запустити скрипт розгортання знову.

**`localhost:8080` не відповідає, а поди `Running`**
Перевірити `docker port hpc-control-plane` (перенаправлення є лише в кластері, створеному з
`k8s/kind-cluster.yaml`; старий кластер із тим самим імʼям його не має) і готовність подів:
`kubectl get endpoints web -n donors` має містити адреси.

**`kubectl top` повертає `Metrics API not available`**
metrics-server без `--kubelet-insecure-tls` або ще не піднявся:

```powershell
kubectl get deployment metrics-server -n kube-system -o jsonpath="{.spec.template.spec.containers[0].args}"; kubectl logs -n kube-system deployment/metrics-server --tail 20
```

**У Prometheus немає рядів `http_requests_total`**
Status -> Targets: якщо таргета `podMonitor/donors/web/0` немає, оператор не підхопив PodMonitor.
Перевірити, що в values стоїть `podMonitorSelectorNilUsesHelmValues: false`, і застосувати
PodMonitor знову.

**`kind create cluster` падає з `ports are not available ... bind`**
Хостовий порт зайнятий іншою програмою. Подивитися, ким:

```powershell
Get-NetTCPConnection -LocalPort 8080,9090 -State Listen | Select-Object LocalPort, OwningProcess
```

Якщо це служба NI Application Web Server — зупинити її, як описано в розділі про порти
вище. Порти задані в `k8s/kind-cluster.yaml` (`hostPort`); після зміни кластер треба
створити наново.

**Маніфести поїхали не в той кластер**
Так було до виправлення скрипта: `kubectl` без `--context` працює з поточним контекстом
kubeconfig, а це може бути будь-який сторонній кластер. Тепер `scripts\k8s_up.ps1` звертається
лише до `kind-hpc` і зупиняється, якщо контексту немає. Перевірити, куди дивиться kubectl:

```powershell
kubectl config current-context; kubectl config get-contexts
```

**`kubectl` звертається не до того кластера**

```powershell
kubectl config current-context; kubectl config use-context kind-hpc
```

**Скрипт `.ps1` падає з `Missing closing '}'` або показує кракозябри**
Файл утратив BOM. Обидва скрипти мають бути збережені як UTF-8 **з BOM**, інакше Windows
PowerShell 5.1 читає українські коментарі як cp1252.

