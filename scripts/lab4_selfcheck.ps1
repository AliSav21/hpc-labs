# Самоперевірка стенду ЛР4 (кроки 1-2, варіант 19).
# Нічого не змінює, окрім однієї тимчасової задачі в черзі, яку сама ж і прибирає.
#
#   powershell -File scripts\lab4_selfcheck.ps1
[CmdletBinding()]
param(
    [string]$Cluster = "hpc",
    [int]$WebPort = 18080,
    [int]$PromPort = 19090
)

$ctx = "kind-$Cluster"
$script:passed = 0
$script:failed = 0

function Say-Result($n, $title, $ok, $got, $want) {
    if ($ok) {
        $script:passed++
        Write-Host ("[{0,2}] ПРОЙДЕНО  {1}" -f $n, $title) -ForegroundColor Green
    } else {
        $script:failed++
        Write-Host ("[{0,2}] НЕ ПРОЙДЕНО  {1}" -f $n, $title) -ForegroundColor Red
        Write-Host ("      мало бути: {0}" -f $want)
    }
    if ($got) { Write-Host ("      вийшло:   {0}" -f ($got -join " | ")) }
}

function K($arguments) { & kubectl.exe --context $ctx @arguments 2>&1 }
function Q($sql) {
    (K @("exec", "-n", "donors", "db-0", "-c", "postgres", "--", "psql", "-U", "donors",
         "-d", "donors", "-At", "-c", $sql)) -join " "
}

Write-Host "== самоперевірка стенду ЛР4, контекст $ctx" -ForegroundColor Cyan

# 1. таблиці черги і результатів
$tables = Q "select string_agg(table_name, ',' order by table_name) from information_schema.tables where table_schema='public'"
Say-Result 1 "таблиці tasks і task_results створені міграцією" `
    (($tables -match "tasks") -and ($tables -match "task_results")) $tables "tasks, task_results"

# 2. поля tasks за умовою
$cols = Q "select string_agg(column_name, ',' order by column_name) from information_schema.columns where table_name='tasks'"
$need = @("id", "record_id", "status", "seed", "attempts", "max_attempts", "token",
          "lease_until", "worker_id", "created_at", "started_at", "finished_at")
$missing = $need | Where-Object { $cols -notmatch ("\b" + $_ + "\b") }
Say-Result 2 "усі поля tasks за умовою" ($missing.Count -eq 0) $cols ($need -join ", ")

# 3. значення за замовчуванням max_attempts = 3 і token = 0
$defs = Q "select column_name || '=' || column_default from information_schema.columns where table_name='tasks' and column_name in ('max_attempts','token')"
Say-Result 3 "max_attempts = 3 і token = 0 за замовчуванням" `
    (($defs -match "max_attempts=3") -and ($defs -match "token=0")) $defs "max_attempts=3, token=0"

# 4. індекс за (status, created_at)
$idx = Q "select string_agg(indexdef, ' | ') from pg_indexes where tablename='tasks'"
Say-Result 4 "індекс за (status, created_at)" ($idx -match "status, created_at") $idx `
    "btree (status, created_at)"

# 5. первинний ключ task_results і зовнішній ключ на сутність
$pk = Q "select count(*) from pg_constraint where conname='task_results_pkey'"
$fk = Q "select count(*) from pg_constraint where conrelid='tasks'::regclass and contype='f'"
Say-Result 5 "первинний ключ task_results і зовнішній ключ tasks.record_id" `
    (($pk -match "^1") -and ($fk -match "^1")) "pkey=$pk, fkey=$fk" "по одному"

# 6. тригер NOTIFY для вимоги Д5
$trg = Q "select tgname || ':' || (select proname from pg_proc where oid=tgfoid) from pg_trigger where tgrelid='tasks'::regclass and not tgisinternal"
Say-Result 6 "тригер на вставку в tasks надсилає повідомлення (Д5)" `
    ($trg -match "tasks_notify") $trg "tasks_notify_insert:tasks_notify"

# 7. Deployment worker без replicas, з лімітами і 30 с на зупинку
$spec = K @("get", "deployment", "worker", "-n", "donors", "-o",
            "jsonpath={.spec.template.spec.terminationGracePeriodSeconds}|{.spec.template.spec.containers[0].resources.requests.cpu}|{.spec.template.spec.containers[0].resources.limits.cpu}|{.spec.template.spec.containers[0].resources.limits.memory}")
Say-Result 7 "worker: grace 30 с, requests.cpu 250m, limits 500m і 512Mi" `
    ($spec -eq "30|250m|500m|512Mi") $spec "30|250m|500m|512Mi"

$manifest = Get-Content k8s/base/31-worker-deployment.yaml -Raw
Say-Result 8 "у маніфесті Deployment worker немає поля replicas" `
    ($manifest -notmatch "(?m)^\s*replicas:") "" "поля replicas немає"

# 9. WORKER_ID з Downward API
$wid = K @("get", "deployment", "worker", "-n", "donors", "-o",
           "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='WORKER_ID')].valueFrom.fieldRef.fieldPath}")
Say-Result 9 "WORKER_ID заповнюється іменем пода через Downward API" `
    ($wid -eq "metadata.name") $wid "metadata.name"

# 10. параметри варіанта у ConfigMap
$cm = K @("get", "configmap", "worker-config", "-n", "donors", "-o",
          "jsonpath={.data.POLL_MS}|{.data.LEASE_S}|{.data.Q_LO}|{.data.Q_HI}|{.data.SLICE_SIZE}|{.data.FAIL_RATE}")
Say-Result 10 "параметри варіанта 19: T_poll 50 мс, L 5 с, 0,02/0,98, S 256, FAIL_RATE 0" `
    ($cm -eq "50|5|0.02|0.98|256|0") $cm "50|5|0.02|0.98|256|0"

# 11. ендпоінт створення задачі: 202 і тіло задачі
$rid = (Invoke-WebRequest "http://127.0.0.1:$WebPort/donor-registry?limit=1" -UseBasicParsing).Content |
    ConvertFrom-Json | ForEach-Object { $_.items[0].id }
$created = $null
try {
    $r = Invoke-WebRequest "http://127.0.0.1:$WebPort/donor-registry/$rid/tasks" -Method Post `
        -UseBasicParsing -TimeoutSec 10
    $created = $r.Content | ConvertFrom-Json
    $code = $r.StatusCode
} catch { $code = $_.Exception.Response.StatusCode.value__ }
Say-Result 11 "POST /donor-registry/{id}/tasks віддає 202 і тіло задачі" `
    (($code -eq 202) -and ($created.status -eq "queued") -and ($created.seed -ge 1)) `
    "код $code, статус $($created.status), seed $($created.seed)" "202, queued, seed >= 1"

# 12. 404 на неіснуючий запис
try {
    Invoke-WebRequest "http://127.0.0.1:$WebPort/donor-registry/999999999/tasks" -Method Post `
        -UseBasicParsing -TimeoutSec 10 | Out-Null
    $code404 = 200
} catch { $code404 = $_.Exception.Response.StatusCode.value__ }
Say-Result 12 "POST задачі для неіснуючого запису віддає 404" ($code404 -eq 404) $code404 "404"

# 13. GET /tasks/{id} і GET /tasks з фільтром
Start-Sleep -Seconds 3
$one = (Invoke-WebRequest "http://127.0.0.1:$WebPort/tasks/$($created.id)" -UseBasicParsing).Content |
    ConvertFrom-Json
$list = (Invoke-WebRequest "http://127.0.0.1:$WebPort/tasks?status=done&limit=5" -UseBasicParsing).Content |
    ConvertFrom-Json
Say-Result 13 "GET /tasks/{id} віддає стан із результатом, GET /tasks фільтрує за status" `
    (($one.id -eq $created.id) -and ($one.result -ne $null) -and ($list.items.Count -ge 1)) `
    "статус $($one.status), результат $(if ($one.result) {'є'} else {'немає'}), у переліку done $($list.total)" `
    "задача done з результатом, перелік не порожній"

# 14. вимірювачі черги в /metrics сервісу
$m = (Invoke-WebRequest "http://127.0.0.1:$WebPort/metrics" -UseBasicParsing).Content
Say-Result 14 "у /metrics сервісу є tasks_queued і tasks_running" `
    (($m -match "(?m)^tasks_queued ") -and ($m -match "(?m)^tasks_running ")) `
    (($m -split "`n" | Where-Object { $_ -match "^tasks_(queued|running) " }) -join " ; ") `
    "tasks_queued і tasks_running"

# 15. метрики воркера
$pod = K @("get", "pods", "-n", "donors", "-l", "app=worker", "-o", "jsonpath={.items[0].metadata.name}")
$wm = K @("exec", "-n", "donors", $pod, "-c", "worker", "--", "python", "-c",
          "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9100/metrics').read().decode())")
$wmText = $wm -join "`n"
$needM = @("worker_tasks_total", "worker_claims_total", "worker_task_duration_seconds",
           "worker_tasks_in_flight")
$missM = $needM | Where-Object { $wmText -notmatch $_ }
Say-Result 15 "у /metrics воркера є всі чотири метрики кроку 2" ($missM.Count -eq 0) `
    (($needM | Where-Object { $wmText -match $_ }) -join ", ") ($needM -join ", ")

# 16. мітки кроків у гістограмі
$steps = @("claim", "fetch", "process", "store") | Where-Object {
    $wmText -match "worker_task_duration_seconds_bucket\{.*step=`"$_`"" }
Say-Result 16 "гістограма має мітки кроків claim, fetch, process, store" `
    ($steps.Count -eq 4) ($steps -join ", ") "claim, fetch, process, store"

# 17. поди worker у збору Prometheus
$targets = (Invoke-WebRequest "http://127.0.0.1:$PromPort/api/v1/targets?state=active" -UseBasicParsing).Content |
    ConvertFrom-Json
# PodMonitor у kube-prometheus-stack дає мітку job виду "<namespace>/<імʼя PodMonitor>"
# @() обовʼязково: Windows PowerShell 5.1 на одному елементі повертає не масив, і .Count порожній
$wt = @($targets.data.activeTargets | Where-Object { $_.labels.job -eq "donors/worker" })
$wtBad = @($wt | Where-Object { $_.health -ne "up" })
Say-Result 17 "поди worker у збору Prometheus у стані up" `
    (($wt.Count -ge 1) -and ($wtBad.Count -eq 0)) `
    "цілей $($wt.Count), станів: $(($wt.health | Sort-Object -Unique) -join ',')" `
    "щонайменше одна ціль, усі up"

# прибираємо тимчасову задачу
Q "delete from task_results where task_id = $($created.id); delete from tasks where id = $($created.id)" | Out-Null

Write-Host ""
# Windows PowerShell 5.1 не приймає if як вираз у аргументі
$color = "Red"; if ($script:failed -eq 0) { $color = "Green" }
Write-Host ("ПРОЙДЕНО {0}, НЕ ПРОЙДЕНО {1}" -f $script:passed, $script:failed) `
    -ForegroundColor $color
if ($script:failed -ne 0) { exit 1 }
