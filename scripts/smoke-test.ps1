param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 8876
)

$ErrorActionPreference = "Stop"

$BaseUrl = "http://${HostName}:$Port"

function Assert-True {
  param(
    [object]$Condition,
    [string]$Message
  )
  if (-not [bool]$Condition) {
    throw $Message
  }
}

function New-WebClient {
  $Client = New-Object System.Net.WebClient
  $Client.Encoding = [System.Text.Encoding]::UTF8
  return $Client
}

$Client = New-WebClient
try {
  $IndexText = $Client.DownloadString("$BaseUrl/")
  $HealthText = $Client.DownloadString("$BaseUrl/api/health")
  $SettingsText = $Client.DownloadString("$BaseUrl/api/settings")
  $StateText = $Client.DownloadString("$BaseUrl/api/state")
  $TokenUsageText = $Client.DownloadString("$BaseUrl/api/token-usage")
} finally {
  $Client.Dispose()
}

Assert-True -Condition ($IndexText.Contains("Codex Skills")) -Message "首页没有返回预期内容。"
Assert-True -Condition ($IndexText.Contains("https://github.com/LiamGvchi/gc-minimal-zine-poster/blob/main/SKILL.md")) -Message "首页没有显示 SKILL.md 文件安装入口。"
Assert-True -Condition (-not $IndexText.Contains('id="installPath"')) -Message "首页仍显示技能安装路径输入框。"
Assert-True -Condition (-not $IndexText.Contains('id="installRef"')) -Message "首页仍显示技能安装 ref 输入框。"

$Health = ConvertFrom-Json -InputObject $HealthText
Assert-True -Condition ($Health.ok -eq $true) -Message "/api/health 返回非健康状态。"
Assert-True -Condition ($HealthText.Contains('"projectRoot"')) -Message "/api/health 缺少 projectRoot。"
Assert-True -Condition ($HealthText.Contains('"skillsRepo"')) -Message "/api/health 缺少 skillsRepo。"

$Settings = ConvertFrom-Json -InputObject $SettingsText
Assert-True -Condition ($SettingsText.Contains('"repository"')) -Message "/api/settings 缺少 repository。"
Assert-True -Condition ($SettingsText.Contains('"usageStats"')) -Message "/api/settings 缺少 usageStats。"
Assert-True -Condition ($SettingsText.Contains('"paths"')) -Message "/api/settings 缺少 paths。"

$State = ConvertFrom-Json -InputObject $StateText
Assert-True -Condition ($StateText.Contains('"skills"')) -Message "/api/state 缺少 skills。"
Assert-True -Condition ($StateText.Contains('"stats"')) -Message "/api/state 缺少 stats。"
Assert-True -Condition ($StateText.Contains('"paths"')) -Message "/api/state 缺少 paths。"
Assert-True -Condition ($StateText.Contains('"tokenUsage"')) -Message "/api/state 缺少 tokenUsage。"

$TokenUsage = ConvertFrom-Json -InputObject $TokenUsageText
Assert-True -Condition ($TokenUsageText.Contains('"totalTokens"')) -Message "/api/token-usage 缺少 totalTokens。"
Assert-True -Condition ($TokenUsage.scope -eq "enabled-catalog") -Message "/api/token-usage 范围不是 enabled-catalog。"

Write-Output "Smoke test passed: $BaseUrl"
