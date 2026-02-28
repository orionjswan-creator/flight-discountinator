param(
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$connections = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $connections) {
  Write-Host "No process is listening on port $Port."
  exit 0
}

$owningProcessIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $owningProcessIds) {
  try {
    Stop-Process -Id $procId -Force -ErrorAction Stop
    Write-Host "Stopped process on port $Port (PID $procId)."
  } catch {
    Write-Host "Could not stop PID ${procId}: $($_.Exception.Message)"
  }
}
