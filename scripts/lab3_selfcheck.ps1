# Самоперевірка стенду ЛР3 (кроки 1-5 і додаткова вимога Д1, варіант 19).
# Нічого не змінює, окрім тимчасового запису в базу і тимчасових реплік web:
# перевірка персистентності видаляє под db-0, перевірка Д1 масштабує web до 4 реплік.
#
#   powershell -File scripts\lab3_selfcheck.ps1            повна перевірка
#   powershell -File scripts\lab3_selfcheck.ps1 -Quick     без видалення пода бази і без масштабування
[CmdletBinding()]
param(
    [string]$Cluster = "hpc",
    [int]$WebPort = 8080,
    [int]$PromPort = 9090,
    [int]$Replicas = 4,
    [switch]$Quick
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

# Після перестворення пода бази сервіс кілька секунд віддає 500: у пулі лишаються обірвані
# зʼєднання (psycopg.errors.AdminShutdown). Readiness виводить под із Service, пул оновлюється,
# і сервіс відповідає знову. Чекаємо на це перед перевіркою CRUD.
function Wait-Service($url, $timeoutSec = 120) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 3
    }
    return $false
}

Write-Host "== самоперевірка стенду ЛР3, контекст $ctx" -ForegroundColor Cyan

# 1. вузли і мітка role=infra
$nodes = K @("get", "nodes", "-o", "jsonpath={range .items[*]}{.metadata.name}={.metadata.labels.role}{' '}{end}")
$ready = (K @("get", "nodes", "--no-headers")) -split "`n" | Where-Object { $_ -match "\sReady\s" }
Say-Result 1 "три вузли Ready, control-plane з міткою role=infra" `
    (($ready.Count -eq 3) -and ($nodes -match "control-plane=infra")) $nodes "3 вузли Ready, hpc-control-plane=infra"

# 2. перенаправлення портів
$ports = (& docker port "$Cluster-control-plane" 2>&1) -join " "
Say-Result 2 "перенаправлення портів NodePort на хост" `
    (($ports -match "30080/tcp -> 127.0.0.1:$WebPort") -and ($ports -match "30090/tcp -> 127.0.0.1:$PromPort")) `
    $ports "30080 -> $WebPort, 30090 -> $PromPort"

# 3. стан усіх подів
$bad = (K @("get", "pods", "-A", "--no-headers")) -split "`n" |
       Where-Object { $_ -and ($_ -notmatch "Running") -and ($_ -notmatch "Completed") }
Say-Result 3 "немає подів поза станами Running і Completed" ($bad.Count -eq 0) $bad "порожній перелік"

# 4. база на вузлі role=infra
$dbNode = K @("get", "pod", "db-0", "-n", "donors", "-o", "jsonpath={.spec.nodeName}")
$infra = K @("get", "nodes", "-l", "role=infra", "-o", "jsonpath={.items[0].metadata.name}")
Say-Result 4 "под бази на вузлі з міткою role=infra" ($dbNode -eq $infra) $dbNode $infra

# 5. max_connections
$maxConn = (K @("exec", "-n", "donors", "db-0", "--", "psql", "-U", "donors", "-d", "donors", "-tAc", "show max_connections")) -join "" -replace "\s", ""
Say-Result 5 "max_connections бази" ($maxConn -eq "100") $maxConn "100"

# 6. персистентність даних після видалення пода
if ($Quick) {
    Write-Host "[ 6] ПРОПУЩЕНО  персистентність бази (-Quick)" -ForegroundColor Yellow
} else {
    $code = "SELFCHECK-" + [int][double]::Parse((Get-Date -UFormat %s))
    K @("exec", "-n", "donors", "db-0", "--", "psql", "-U", "donors", "-d", "donors", "-tAc",
        "insert into donor_registry (donor_code, birth_year, blood_group, hla_typing, registered_on, available) values ('$code', 1990, 'O+', '{A1}', current_date, true)") | Out-Null
    K @("delete", "pod", "db-0", "-n", "donors") | Out-Null
    K @("rollout", "status", "statefulset/db", "-n", "donors", "--timeout=300s") | Out-Null
    $cnt = (K @("exec", "-n", "donors", "db-0", "--", "psql", "-U", "donors", "-d", "donors", "-tAc",
        "select count(*) from donor_registry where donor_code = '$code'")) -join "" -replace "\s", ""
    K @("exec", "-n", "donors", "db-0", "--", "psql", "-U", "donors", "-d", "donors", "-tAc",
        "delete from donor_registry where donor_code = '$code'") | Out-Null
    Say-Result 6 "дані збереглися після видалення пода бази" ($cnt -eq "1") $cnt "1"
}

# 7. поля replicas немає в маніфесті
$hasReplicas = Select-String -Path "k8s\base\21-web-deployment.yaml" -Pattern "^\s*replicas:"
Say-Result 7 "у маніфесті web немає поля replicas" ($null -eq $hasReplicas) $hasReplicas "нічого не знайдено"

# 8. ресурси контейнера web
$res = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].resources}")
Say-Result 8 "ресурси web: requests 500m, limits 1 CPU і 512Mi" `
    (($res -match '"cpu":"500m"') -and ($res -match '"cpu":"1"') -and ($res -match '"memory":"512Mi"')) `
    $res 'requests.cpu 500m, limits.cpu 1, limits.memory 512Mi'

# 9. проби і коректна зупинка
$rp = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].readinessProbe}")
$lp = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].livenessProbe}")
$lc = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].lifecycle}")
$gr = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.terminationGracePeriodSeconds}")
$probesOk = ($rp -match '"path":"/healthz"') -and ($rp -match '"timeoutSeconds":3') -and
            ($rp -match '"periodSeconds":5') -and ($rp -match '"failureThreshold":3') -and
            ($lp -match '"path":"/livez"') -and ($lc -match '"seconds":5') -and ($gr -eq "15")
Say-Result 9 "readiness /healthz 3-5-3, liveness /livez, preStop 5 с, grace 15 с" $probesOk @($rp, $lc, $gr) `
    "/healthz з timeout 3, period 5, failureThreshold 3; /livez; preStop sleep 5; grace 15"

# 10. пароль лише в Secret
$cmHasPw = (K @("get", "configmap", "web-config", "-n", "donors", "-o", "yaml")) -match "(?i)password"
$envRef = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='POSTGRES_PASSWORD')]}")
$podName = K @("get", "deployment", "web", "-n", "donors", "-o", "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='POD_NAME')]}")
$gitIgnored = (& git check-ignore -v .env 2>&1) -match "\.env"
Say-Result 10 "пароль лише в Secret, POD_NAME через Downward API" `
    ((-not $cmHasPw) -and ($envRef -match "secretKeyRef") -and ($podName -match "metadata.name") -and $gitIgnored) `
    @($envRef, $podName) "у ConfigMap пароля немає, secretKeyRef і fieldRef на місці, .env у .gitignore"

# 11. повний цикл CRUD через NodePort із заголовком X-Pod
$base = "http://127.0.0.1:$WebPort/donor-registry"
if (-not (Wait-Service "http://127.0.0.1:$WebPort/healthz")) {
    Write-Host "      сервіс не відповів 200 за 120 с — CRUD нижче, найімовірніше, не пройде" -ForegroundColor Yellow
}
$code = "SELFCHECK-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$body = "{""donor_code"":""$code"",""birth_year"":1990,""blood_group"":""O+"",""hla_typing"":[""A1""],""registered_on"":""2026-01-01"",""available"":true}"
$steps = @()
try {
    $r = Invoke-WebRequest $base -Method Post -ContentType "application/json" -Body $body -UseBasicParsing
    $steps += "POST $($r.StatusCode) $($r.Headers['X-Pod'])"
    $id = ($r.Content | ConvertFrom-Json).id
    $r = Invoke-WebRequest "$base/$id" -UseBasicParsing
    $steps += "GET $($r.StatusCode) $($r.Headers['X-Pod'])"
    $r = Invoke-WebRequest "$base/$id" -Method Put -ContentType "application/json" -Body ($body -replace '"available":true', '"available":false') -UseBasicParsing
    $steps += "PUT $($r.StatusCode) $($r.Headers['X-Pod'])"
    $r = Invoke-WebRequest "$base/$id" -Method Delete -UseBasicParsing
    $steps += "DELETE $($r.StatusCode) $($r.Headers['X-Pod'])"
    $crudOk = ($steps -join " ") -match "POST 201" -and ($steps -join " ") -match "GET 200" -and
              ($steps -join " ") -match "PUT 200" -and ($steps -join " ") -match "DELETE 204" -and
              ($steps | Where-Object { $_ -notmatch "web-" }).Count -eq 0
} catch {
    $steps += $_.Exception.Message
    $crudOk = $false
}
Say-Result 11 "CRUD через NodePort повертає 201, 200, 200, 204 із заголовком X-Pod" $crudOk $steps "201, 200, 200, 204 і непорожній X-Pod"

# 12. Д1: рівномірний розподіл по робочих вузлах
if ($Quick) {
    Write-Host "[12] ПРОПУЩЕНО  розподіл подів по вузлах (-Quick)" -ForegroundColor Yellow
} else {
    K @("scale", "deployment/web", "-n", "donors", "--replicas=$Replicas") | Out-Null
    K @("rollout", "status", "deployment/web", "-n", "donors", "--timeout=300s") | Out-Null
    $byNode = @{}
    foreach ($line in (K @("get", "pods", "-n", "donors", "-l", "app=web", "-o", "jsonpath={range .items[*]}{.spec.nodeName}{'\n'}{end}")) -split "`n") {
        $n = "$line".Trim()
        if ($n) { $byNode[$n] = 1 + $byNode[$n] }
    }
    $counts = $byNode.Values | Sort-Object
    $skew = if ($counts.Count) { $counts[-1] - $counts[0] } else { 99 }
    Say-Result 12 "репліки web рівномірно по робочих вузлах (Д1)" `
        (($byNode.Count -eq 2) -and ($skew -le 1)) `
        (($byNode.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ", ") `
        "два вузли, різниця не більша за 1"
}

# 13. metrics-server
$top = K @("top", "pods", "-n", "donors", "--no-headers")
Say-Result 13 "kubectl top pods повертає дані" `
    (($top -join " ") -notmatch "error" -and ($top -join " ") -match "\d+m") ($top | Select-Object -First 2) "рядки з CPU і памʼяттю"

# 14. ряди в Prometheus і інтервал збору
$queries = [ordered]@{
    "запити за секунду на под web"      = 'count(sum by (pod) (rate(http_requests_total{namespace="donors"}[1m])))'
    "лічильник періодів CFS"            = 'count(container_cpu_cfs_periods_total{namespace="donors",container="web"})'
    "лічильник дроселювання CFS"        = 'count(container_cpu_cfs_throttled_periods_total{namespace="donors",container="web"})'
    "готові репліки Deployment"         = 'kube_deployment_status_replicas_ready{namespace="donors",deployment="web"}'
    "postgres-exporter"                 = 'pg_up'
}
$missing = @()
foreach ($q in $queries.GetEnumerator()) {
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$PromPort/api/v1/query" -Body @{ query = $q.Value } -UseBasicParsing
        if (-not $resp.data.result -or $resp.data.result.Count -eq 0) { $missing += $q.Key }
    } catch { $missing += "$($q.Key) (Prometheus недоступний)" }
}
$interval = K @("get", "prometheus", "-n", "monitoring", "-o", "jsonpath={.items[0].spec.scrapeInterval}")
Say-Result 14 "у Prometheus є всі потрібні ряди, інтервал збору 5 с" `
    (($missing.Count -eq 0) -and ($interval -eq "5s")) `
    (@("інтервал $interval") + $missing) "усі ряди присутні, інтервал 5s"

# 15. HPA бачить метрики (створюється і одразу видаляється)
K @("apply", "-f", "k8s/hpa/web-hpa.yaml") | Out-Null
Start-Sleep -Seconds 30
$hpa = (K @("get", "hpa", "web", "-n", "donors", "--no-headers")) -join " "
K @("delete", "hpa", "web", "-n", "donors", "--ignore-not-found") | Out-Null
Say-Result 15 "HPA бачить використання CPU (не <unknown>)" `
    (($hpa -match "cpu:\s*\d+%/50%") -and ($hpa -notmatch "unknown")) $hpa "cpu: N%/50%, межі 1 і $Replicas"

Write-Host ""
Write-Host ("ПРОЙДЕНО {0}, НЕ ПРОЙДЕНО {1}" -f $script:passed, $script:failed) -ForegroundColor Cyan
if ($script:failed -gt 0) { exit 1 }
