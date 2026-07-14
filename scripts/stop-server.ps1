$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$WorkDir = Join-Path $Root "work"
$PidFile = Join-Path $WorkDir "server.pid"

if (-not (Test-Path $PidFile)) {
  Write-Output "PID file not found. Server may not be running."
  exit 0
}

$PidPayload = (Get-Content -Raw -Encoding UTF8 $PidFile).Trim()
if (-not $PidPayload) {
  Remove-Item -LiteralPath $PidFile -Force
  Write-Output "Empty PID file removed."
  exit 0
}

$ServerPid = $null
$Port = $null
try {
  $Meta = $PidPayload | ConvertFrom-Json
  if ($null -ne $Meta.pid) {
    $ServerPid = [int]$Meta.pid
    $Port = [int]$Meta.port
  } elseif ($PidPayload -match '^\d+$') {
    $ServerPid = [int]$PidPayload
  }
} catch {
  $ServerPid = [int]$PidPayload
}

if (-not $ServerPid -or $ServerPid -lt 1) {
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  Write-Output "Invalid PID file removed."
  exit 0
}

$Process = Get-Process -Id $ServerPid -ErrorAction SilentlyContinue
if ($Process) {
  $CommandLine = ""
  try {
    $Cim = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)"
    $CommandLine = [string]$Cim.CommandLine
  } catch {
  }
  $ExpectedApp = Join-Path $Root "app.py"
  if ($CommandLine -and ($CommandLine -notlike "*$ExpectedApp*")) {
    Write-Output "PID $($Process.Id) is running, but it is not this Codex Skills Manager instance."
    Write-Output "CommandLine: $CommandLine"
    exit 1
  }
  if ($Port -and $CommandLine -and ($CommandLine -notlike "*--port $Port*")) {
    Write-Output "PID $($Process.Id) command line does not match port $Port."
    Write-Output "CommandLine: $CommandLine"
    exit 1
  }
  Stop-Process -Id $Process.Id
  Start-Sleep -Milliseconds 700
  if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) {
    Stop-Process -Id $Process.Id -Force
  }
  Write-Output "Stopped Codex Skills Manager, PID: $($Process.Id)"
} else {
  Write-Output "PID $ServerPid is not running."
}

Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
