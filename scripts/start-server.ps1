param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8876
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$WorkDir = Join-Path $Root "work"
$PidFile = Join-Path $WorkDir "server.pid"
$LogFile = Join-Path $WorkDir "server.log"
$ErrFile = Join-Path $WorkDir "server.err.log"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

if (Test-Path $PidFile) {
  $ExistingPid = (Get-Content -Raw -Encoding UTF8 $PidFile).Trim()
  if ($ExistingPid -and (Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue)) {
    Write-Output "Codex Skills Manager already running: http://${HostName}:$Port"
    exit 0
  }
}

$Python = (Get-Command python -ErrorAction Stop).Source
$App = Join-Path $Root "app.py"
$Args = @($App, "--host", $HostName, "--port", [string]$Port)

$Process = Start-Process `
  -FilePath $Python `
  -ArgumentList $Args `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $LogFile `
  -RedirectStandardError $ErrFile `
  -PassThru

[System.IO.File]::WriteAllText($PidFile, [string]$Process.Id, [System.Text.UTF8Encoding]::new($false))
Write-Output "Codex Skills Manager: http://${HostName}:$Port"
Write-Output "PID: $($Process.Id)"
Write-Output "Log: $LogFile"
