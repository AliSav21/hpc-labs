# Крок 2 лабораторної роботи 3: одна команда розгортання стенду в kind.
# Створює кластер, ставить мітку role=infra, збирає і завантажує образ, створює Secret з .env,
# застосовує маніфести і ставить моніторинг.
#
# Усі звернення до кластера йдуть з явним --context kind-<Cluster>: без цього kubectl пішов би
# у поточний контекст kubeconfig, тобто в будь-який сторонній кластер.
[CmdletBinding()]
param(
    [string]$Cluster = "hpc",
    [string]$Image = "donor-registry:lab3",
    # ЛР4: власний образ воркера пула обробки
    [string]$WorkerImage = "donor-worker:lab4",
    [switch]$SkipMonitoring
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$ctx = "kind-$Cluster"

function Need-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "у PATH немає $name"
    }
}
# Зовнішня програма і $ErrorActionPreference = "Stop": у Windows PowerShell 5.1 кожен
# рядок, який програма пише у stderr, стає помилкою і зупиняє скрипт. docker build пише
# туди весь свій поступ, тож розгортання падало на першому ж образі. Тому на час виклику
# зупинку знімаємо, а успіх перевіряємо за кодом виходу, як і раніше.
function Invoke-Native($file, $arguments, $what) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $file @arguments 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($LASTEXITCODE -ne 0) { throw "$what завершилося з кодом $LASTEXITCODE" }
}
function Invoke-Checked($file, $arguments, $what) {
    Invoke-Native $file $arguments $what
}
function Invoke-Kubectl($arguments) {
    # kubectl.exe з розширенням: інакше ім'я функції перекриває саму програму
    Invoke-Native "kubectl.exe" (@("--context", $ctx) + $arguments) "kubectl $($arguments -join ' ')"
}

foreach ($t in @("docker", "kind", "kubectl", "helm")) { Need-Tool $t }

# 0. пароль бази беремо з .env: в образі, ConfigMap і репозиторії його немає
if (-not (Test-Path .env)) {
    throw ".env немає: copy .env.example .env і задати POSTGRES_PASSWORD"
}
$m = Select-String -Path .env -Pattern '^\s*POSTGRES_PASSWORD\s*=\s*(.+?)\s*$' | Select-Object -First 1
if (-not $m) { throw "у .env немає рядка POSTGRES_PASSWORD" }
$pw = $m.Matches[0].Groups[1].Value
if ($pw -eq "change-me") { throw "у .env лишився POSTGRES_PASSWORD=change-me" }

# 1. кластер
# kind пише "No kind clusters found." у stderr; у Windows PowerShell 5.1 такий рядок стає
# помилкою і зупиняв би скрипт, тому на час виклику знімаємо зупинку
$ErrorActionPreference = "Continue"
$clusters = @(& kind get clusters 2>&1 | ForEach-Object { "$_".Trim() })
$ErrorActionPreference = "Stop"
if ($clusters -notcontains $Cluster) {
    Write-Host "== створюю кластер $Cluster"
    Invoke-Checked "kind" @("create", "cluster", "--config", "k8s/kind-cluster.yaml") "kind create cluster"
} else {
    Write-Host "== кластер $Cluster вже існує"
}

# запобіжник: далі працюємо лише з цим кластером
& kubectl config use-context $ctx | Out-Null
$current = & kubectl config current-context
if ($current -ne $ctx) {
    throw "поточний контекст '$current', а не '$ctx': розгортання зупинено"
}

# 2. мітка role=infra на control-plane: на цей вузол ідуть база і моніторинг
$cp = & kubectl --context $ctx get nodes -l node-role.kubernetes.io/control-plane -o jsonpath="{.items[0].metadata.name}"
if (-not $cp) { throw "у кластері $Cluster немає вузла control-plane" }
Invoke-Kubectl @("label", "node", $cp, "role=infra", "--overwrite")

# 3. образ сервісу в кластер (реєстру немає)
Write-Host "== збираю образ $Image"
Invoke-Checked "docker" @("build", "-t", $Image, ".") "docker build"
Invoke-Checked "kind" @("load", "docker-image", $Image, "--name", $Cluster) "kind load docker-image"

# ЛР4: образ воркера. Тег web лишається з ЛР3 — це той самий сервіс, якому ЛР4 додала
# ендпоінти черги задач, тому перескладання образу не змінює імені стенду.
Write-Host "== збираю образ $WorkerImage"
Invoke-Checked "docker" @("build", "-f", "Dockerfile.worker", "-t", $WorkerImage, ".") "docker build worker"
Invoke-Checked "kind" @("load", "docker-image", $WorkerImage, "--name", $Cluster) "kind load worker image"

# 4. namespace, Secret, схема і маніфести
Invoke-Kubectl @("apply", "-f", "k8s/base/00-namespace.yaml")
$ErrorActionPreference = "Continue"
& kubectl --context $ctx create secret generic db-secret -n donors --from-literal=POSTGRES_PASSWORD=$pw `
    --dry-run=client -o yaml | kubectl --context $ctx apply -f -
if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = "Stop"; throw "не вдалось створити Secret db-secret" }
& kubectl --context $ctx create configmap db-schema -n donors --from-file=schema.sql=db/schema.sql `
    --dry-run=client -o yaml | kubectl --context $ctx apply -f -
& kubectl --context $ctx create configmap db-schema-lab4 -n donors --from-file=schema-lab4.sql=db/schema-lab4.sql `
    --dry-run=client -o yaml | kubectl --context $ctx apply -f -
if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = "Stop"; throw "не вдалось створити ConfigMap db-schema-lab4" }
$ErrorActionPreference = "Stop"
# Job незмінний після створення, тому перед повторним застосуванням його видаляємо
Invoke-Kubectl @("delete", "job", "db-migrate", "-n", "donors", "--ignore-not-found")
Invoke-Kubectl @("delete", "job", "db-migrate-lab4", "-n", "donors", "--ignore-not-found")
Invoke-Kubectl @("apply", "-f", "k8s/base")

Write-Host "== чекаю базу і міграцію"
Invoke-Kubectl @("rollout", "status", "statefulset/db", "-n", "donors", "--timeout=300s")
Invoke-Kubectl @("wait", "--for=condition=complete", "job/db-migrate", "-n", "donors", "--timeout=300s")
Invoke-Kubectl @("wait", "--for=condition=complete", "job/db-migrate-lab4", "-n", "donors", "--timeout=300s")
Invoke-Kubectl @("rollout", "status", "deployment/web", "-n", "donors", "--timeout=300s")

# 5. моніторинг
if (-not $SkipMonitoring) {
    Write-Host "== metrics-server"
    Invoke-Kubectl @("apply", "-f", "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml")
    # у kind kubelet віддає метрики з самопідписаним сертифікатом
    $argsNow = & kubectl --context $ctx get deployment metrics-server -n kube-system -o jsonpath="{.spec.template.spec.containers[0].args}"
    if ($argsNow -notlike "*kubelet-insecure-tls*") {
        Invoke-Kubectl @("patch", "deployment", "metrics-server", "-n", "kube-system", "--type=json",
                             "--patch-file", "k8s/monitoring/metrics-server-insecure-tls.patch.json")
    }
    Invoke-Kubectl @("rollout", "status", "deployment/metrics-server", "-n", "kube-system", "--timeout=300s")

    Write-Host "== kube-prometheus-stack і postgres-exporter"
    $ErrorActionPreference = "Continue"
    & helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>&1 | Out-Null
    & helm repo update prometheus-community 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Invoke-Checked "helm" @("upgrade", "--install", "monitoring", "prometheus-community/kube-prometheus-stack",
        "--kube-context", $ctx, "-n", "monitoring", "--create-namespace",
        "-f", "k8s/monitoring/kube-prometheus-stack.values.yaml", "--wait", "--timeout", "15m") "helm kube-prometheus-stack"
    Invoke-Checked "helm" @("upgrade", "--install", "pg-exporter", "prometheus-community/prometheus-postgres-exporter",
        "--kube-context", $ctx, "-n", "donors",
        "-f", "k8s/monitoring/postgres-exporter.values.yaml", "--wait", "--timeout", "5m") "helm postgres-exporter"
    Invoke-Kubectl @("apply", "-f", "k8s/monitoring/web-podmonitor.yaml")
    Invoke-Kubectl @("apply", "-f", "k8s/monitoring/worker-podmonitor.yaml")
}

Write-Host ""
Write-Host "стенд готовий:"
Write-Host "  сервіс     http://127.0.0.1:18080/donor-registry"
Write-Host "  Prometheus http://127.0.0.1:19090"
Invoke-Kubectl @("get", "pods", "-n", "donors", "-o", "wide")
