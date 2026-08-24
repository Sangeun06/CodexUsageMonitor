[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "CodexUsageCollector-$($env:USERNAME)"
$InstallDir = Join-Path $env:LOCALAPPDATA "CodexUsageCollector"
$ConfigDir = Join-Path $env:APPDATA "CodexUsageCollector"

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Codex Usage Collector removed." -ForegroundColor Green
