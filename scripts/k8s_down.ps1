# Крок 2: повне видалення кластера разом із томами, образами і моніторингом.
[CmdletBinding()]
param([string]$Cluster = "hpc")

$ErrorActionPreference = "Stop"
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) { throw "у PATH немає kind" }

# kind пише поступ у stderr, а при $ErrorActionPreference = "Stop" кожен такий рядок у
# Windows PowerShell 5.1 стає помилкою і зупиняє скрипт. Та сама причина, що й у
# scripts/k8s_up.ps1: на час виклику зупинку знімаємо, успіх перевіряємо за кодом виходу.
$ErrorActionPreference = "Continue"
& kind delete cluster --name $Cluster 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($code -ne 0) { throw "kind delete cluster завершився з кодом $code" }
Write-Host "кластер $Cluster видалено разом із томами"
