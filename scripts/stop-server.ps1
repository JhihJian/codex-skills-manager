$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$WorkDir = Join-Path $Root "work"
$PidFile = Join-Path $WorkDir "server.pid"

if (-not (Test-Path $PidFile)) {
  Write-Output "PID file not found. Server may not be running."
  exit 0
}

$PidText = (Get-Content -Raw -Encoding UTF8 $PidFile).Trim()
if (-not $PidText) {
  Remove-Item -LiteralPath $PidFile -Force
  Write-Output "Empty PID file removed."
  exit 0
}

$Process = Get-Process -Id ([int]$PidText) -ErrorAction SilentlyContinue
if ($Process) {
  Stop-Process -Id $Process.Id -Force
  Write-Output "Stopped Codex Skills Manager, PID: $($Process.Id)"
} else {
  Write-Output "PID $PidText is not running."
}

Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
