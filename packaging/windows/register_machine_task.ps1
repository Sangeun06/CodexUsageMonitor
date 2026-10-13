[CmdletBinding()]
param([switch]$UpdateConnection)

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
$StateFile = Join-Path $DataDir "collector-state.json"
$ProfilesDirectoryValue = (Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList").ProfilesDirectory
$ProfilesRoot = [Environment]::ExpandEnvironmentVariables([string]$ProfilesDirectoryValue)
$LogMaxBytes = if ($Configuration.PSObject.Properties.Name -contains "LogMaxBytes") { [int]$Configuration.LogMaxBytes } else { 5242880 }
$LogBackups = if ($Configuration.PSObject.Properties.Name -contains "LogBackups") { [int]$Configuration.LogBackups } else { 3 }

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
$ConfigFile = Join-Path $DataDir "collector-config.json"
if ($UpdateConnection -or -not (Test-Path -LiteralPath $ConfigFile)) {
    if (Test-Path -LiteralPath $ConfigFile) {
        $Saved = Get-Content -Raw -LiteralPath $ConfigFile | ConvertFrom-Json
        if ($Saved.account_key) { $AccountKey = [string]$Saved.account_key }
        if ($Saved.token_file -and (Test-Path -LiteralPath ([string]$Saved.token_file))) { $TokenFile = [string]$Saved.token_file }
    }
    $Uri = $null
    if (-not [Uri]::TryCreate($Server, [UriKind]::Absolute, [ref]$Uri) -or
        $Uri.Scheme -notin @("http", "https") -or -not $Uri.Host -or $Uri.UserInfo -or $Uri.Query -or $Uri.Fragment -or $Server -match '[\x00-\x1f\x7f]') {
        throw "Invalid HTTP(S) server URL."
    }
    @{server=$Server; token_file=$TokenFile; account_key=$AccountKey} |
        ConvertTo-Json | Set-Content -LiteralPath $ConfigFile -Encoding UTF8
} else {
    $Saved = Get-Content -Raw -LiteralPath $ConfigFile | ConvertFrom-Json
    $Server = [string]$Saved.server
}
$Uri = [Uri]$Server
& netsh.exe advfirewall firewall delete rule name="Codex Usage Collector outbound" | Out-Null
& netsh.exe advfirewall firewall add rule name="Codex Usage Collector outbound" dir=out action=allow program=$PythonExecutable enable=yes profile=any protocol=TCP remoteport=$($Uri.Port) | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not configure the outbound firewall rule." }

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$CollectorArguments = @(
    $Collector,
    "--server", $Server,
    "--config-file", (Join-Path $DataDir "collector-config.json"),
    "--token-file", $TokenFile,
    "--account-key", $AccountKey,
    "--profiles-root", $ProfilesRoot,
    "--state-file", $StateFile,
    "--interval", "30",
    "--log-file", $LogFile,
    "--log-max-bytes", ([string]$LogMaxBytes),
    "--log-backups", ([string]$LogBackups)
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
