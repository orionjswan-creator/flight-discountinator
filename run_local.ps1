param(
  [int]$Port = 8000,
  [switch]$Reload
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Installing dependencies..."
python -m pip install -r requirements.txt | Out-Host

if (-not $env:AMADEUS_CLIENT_ID -or -not $env:AMADEUS_CLIENT_SECRET) {
  Write-Host "Note: AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not set in current shell."
  Write-Host "The server will start, but /deals will return 500 until credentials are set."
}

$args = @("-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) {
  $args += "--reload"
}

Write-Host "Starting local API on http://127.0.0.1:$Port ..."
Write-Host "Press Ctrl+C to stop."
& python @args
