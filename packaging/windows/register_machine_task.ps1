[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$TaskName = "CodexUsageCollectorMachine"
$RegistryPath = "HKLM:\Software\CodexUsageCollector"
$Configuration = Get-ItemProperty -LiteralPath $RegistryPath
$InstallDir = [string]$Configuration.InstallDir
$Server = [string]$Configuration.Server
$AccountKey = [string]$Configuration.AccountKey
$DataDir = Join-Path $env:ProgramData "CodexUsageCollector"
$PythonExecutable = Join-Path $InstallDir "runtime\pythonw.exe"
$Collector = Join-Path $InstallDir "collector_windows_machine.py"
$TokenFile = Join-Path $DataDir "collector.token"
$LogFile = Join-Path $DataDir "collector.log"

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Bundled Python runtime was not found."
}
if (-not (Test-Path -LiteralPath $Collector -PathType Leaf)) {
    throw "Machine collector was not found."
}
if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
    throw "Collector token was not found."
}
if ($AccountKey -notmatch '^[a-fA-F0-9]{12}$') {
    throw "A valid registered account key is required."
}

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$CollectorArguments = @(
    $Collector,
    "--server", $Server,
    "--token-file", $TokenFile,
    "--account-key", $AccountKey,
    "--interval", "30",
    "--log-file", $LogFile
)
$ArgumentLine = ($CollectorArguments | ForEach-Object { Quote-TaskArgument $_ }) -join " "
$Action = New-ScheduledTaskAction -Execute $PythonExecutable -Argument $ArgumentLine -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Principal $Principal -Settings $Settings `
    -Description "Send sanitized Codex usage metadata to the central dashboard" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
