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
$TokenFile = Join-Path $Root "data/access-token"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

function Test-Health {
  param([string]$Url)
  try {
    if (-not (Test-Path $TokenFile)) {
      return $false
    }
    $Token = (Get-Content -Raw -Encoding UTF8 $TokenFile).Trim()
    if (-not $Token) {
      return $false
    }
    $Payload = & curl.exe -fsS -H "Authorization: Bearer $Token" "$Url/api/health" 2>$null
    if (-not $Payload) {
      return $false
    }
    $Health = ($Payload -join "`n") | ConvertFrom-Json
    return $Health.ok -eq $true
  } catch {
    return $false
  }
}

if (Test-Path $PidFile) {
  $PidPayload = (Get-Content -Raw -Encoding UTF8 $PidFile).Trim()
  $ExistingPid = $null
  $ExistingPort = $Port
  try {
    $Meta = $PidPayload | ConvertFrom-Json
    if ($null -ne $Meta.pid) {
      $ExistingPid = [int]$Meta.pid
      $ExistingPort = [int]$Meta.port
    } elseif ($PidPayload -match '^\d+$') {
      $ExistingPid = [int]$PidPayload
    }
  } catch {
    if ($PidPayload) {
      $ExistingPid = [int]$PidPayload
    }
  }
  if ($ExistingPid -and $ExistingPid -gt 0 -and (Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue)) {
    $Url = "http://${HostName}:$ExistingPort"
    if (Test-Health $Url) {
      Write-Output "Codex Skills Manager already running: $Url"
      Write-Output "PID: $ExistingPid"
      exit 0
    }
    Write-Output "PID file exists but health check failed for $Url. Remove $PidFile after confirming the process is stale."
    exit 1
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

$Url = "http://${HostName}:$Port"
$PidMeta = [ordered]@{
  pid = $Process.Id
  host = $HostName
  port = $Port
  root = [string]$Root
  startedAt = (Get-Date).ToString("o")
}
[System.IO.File]::WriteAllText($PidFile, ($PidMeta | ConvertTo-Json -Compress), [System.Text.UTF8Encoding]::new($false))

$Healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Milliseconds 500
  if (-not (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
    break
  }
  if (Test-Health $Url) {
    $Healthy = $true
    break
  }
}

if (-not $Healthy) {
  Write-Output "Codex Skills Manager failed to become healthy: $Url"
  if (Test-Path $ErrFile) {
    Write-Output "Error log tail:"
    Get-Content -LiteralPath $ErrFile -Encoding UTF8 -Tail 40
  }
  exit 1
}

Write-Output "Codex Skills Manager: $Url"
Write-Output "PID: $($Process.Id)"
Write-Output "Log: $LogFile"
