param(
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

function Stop-ExistingPortProcess {
  param([int]$TargetPort)

  $connections = Get-NetTCPConnection -State Listen -LocalPort $TargetPort -ErrorAction SilentlyContinue
  if (-not $connections) {
    return
  }

  $owningProcessIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($procId in $owningProcessIds) {
    try {
      Stop-Process -Id $procId -Force -ErrorAction Stop
      Write-Host "Stopped existing process on port $TargetPort (PID $procId)."
    } catch {
      Write-Host "Could not stop PID $procId on port ${TargetPort}: $($_.Exception.Message)"
    }
  }
}

Write-Host "Installing dependencies..."
python -m pip install -r requirements.txt | Out-Host

Stop-ExistingPortProcess -TargetPort $Port

$args = @("-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "$Port")
$stdoutLog = Join-Path $projectRoot "deploy_stdout.log"
$stderrLog = Join-Path $projectRoot "deploy_stderr.log"
if (Test-Path $stdoutLog) { Remove-Item $stdoutLog -Force }
if (Test-Path $stderrLog) { Remove-Item $stderrLog -Force }

$proc = Start-Process -FilePath "python" -ArgumentList $args -WorkingDirectory $projectRoot -PassThru `
  -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog

try {
  $isHealthy = $false
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    $proc.Refresh()
    if ($proc.HasExited) {
      throw "Uvicorn exited early with code $($proc.ExitCode)."
    }
    try {
      $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2
      if ($resp.StatusCode -eq 200) {
        $isHealthy = $true
        break
      }
    } catch {
    }
  }
  if (-not $isHealthy) {
    throw "Healthcheck failed after waiting for server startup."
  }

  Write-Host "Deploy complete. PID: $($proc.Id)"
  Write-Host "Health endpoint: http://127.0.0.1:$Port/health"
  Write-Host "Deals endpoint : http://127.0.0.1:$Port/deals?origin=CMH&top_destinations=10"
} catch {
  Write-Host "Deployment failed: $($_.Exception.Message)"
  if (Test-Path $stderrLog) {
    Write-Host "----- deploy_stderr.log -----"
    Get-Content $stderrLog | Select-Object -Last 40 | Out-Host
  }
  try {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  } catch {
  }
  exit 1
}
