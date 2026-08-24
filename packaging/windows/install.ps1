[CmdletBinding()]
param(
    [string]$Server = "",
    [string]$TokenFile = "",
    [string]$AccountKey = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Server) {
    $ConfigLine = Get-Content (Join-Path $BundleDir "bundle.conf") |
        Where-Object { $_ -like "server=*" } |
        Select-Object -First 1
    if ($ConfigLine) { $Server = $ConfigLine.Substring(7) }
}
if (-not $AccountKey) {
    $AccountLine = Get-Content (Join-Path $BundleDir "bundle.conf") |
        Where-Object { $_ -like "account_key=*" } |
        Select-Object -First 1
    if ($AccountLine) { $AccountKey = $AccountLine.Substring(12) }
}
if (-not $TokenFile) { $TokenFile = Join-Path $BundleDir "collector.token" }

$ParsedUri = $null
if (-not [Uri]::TryCreate($Server, [UriKind]::Absolute, [ref]$ParsedUri) -or
    $ParsedUri.Scheme -notin @("http", "https")) {
    throw "A valid HTTP(S) central server URL is required."
}
if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
    throw "Collector token not found. Use -TokenFile PATH."
}
if ($AccountKey -notmatch '^[a-fA-F0-9]{12}$') {
    throw "A valid registered account key is required."
}

$PythonCommand = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
$PythonPrefix = @()
if (-not $PythonCommand) {
    $PythonCommand = Get-Command "pyw.exe" -ErrorAction SilentlyContinue
    if ($PythonCommand) { $PythonPrefix = @("-3") }
}
if (-not $PythonCommand) {
    $PythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
}
if (-not $PythonCommand) {
    $Winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python 3 was not found and winget is unavailable. Install Python 3, then run install.cmd again."
    }
    Write-Host "Python 3 was not found. Installing it for the current user..." -ForegroundColor Yellow
    & $Winget.Source install --id Python.Python.3.13 --exact --scope user --silent `
        --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Automatic Python installation failed with exit code $LASTEXITCODE." }
    $PythonCommand = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python") `
        -Filter "pythonw.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if (-not $PythonCommand) { throw "Python was installed but pythonw.exe could not be located. Sign out and run install.cmd again." }
}
$PythonExecutable = if ($PythonCommand.PSObject.Properties.Name -contains "Source") {
    $PythonCommand.Source
} else {
    $PythonCommand.FullName
}

$InstallDir = Join-Path $env:LOCALAPPDATA "CodexUsageCollector"
$ConfigDir = Join-Path $env:APPDATA "CodexUsageCollector"
$InstalledCollector = Join-Path $InstallDir "collector.py"
$InstalledToken = Join-Path $ConfigDir "collector.token"
$LogFile = Join-Path $ConfigDir "collector.log"
$TaskName = "CodexUsageCollector-$($env:USERNAME)"
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name

New-Item -ItemType Directory -Force -Path $InstallDir, $ConfigDir | Out-Null
Copy-Item -LiteralPath (Join-Path $BundleDir "collector.py") -Destination $InstalledCollector -Force
Copy-Item -LiteralPath $TokenFile -Destination $InstalledToken -Force

# Limit the token to the current Windows user and SYSTEM.
& icacls.exe $InstalledToken /inheritance:r /grant:r "$($Identity):(R)" "*S-1-5-18:(F)" | Out-Null

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$CollectorArguments = @($PythonPrefix) + @(
    $InstalledCollector,
    "--server", $Server,
    "--token-file", $InstalledToken,
    "--account-key", $AccountKey,
    "--interval", "30",
    "--log-file", $LogFile
)
$ArgumentLine = ($CollectorArguments | ForEach-Object { Quote-TaskArgument $_ }) -join " "
$Action = New-ScheduledTaskAction -Execute $PythonExecutable -Argument $ArgumentLine -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Send sanitized Codex usage metadata to the central dashboard" -Force | Out-Null

if (-not $NoStart) { Start-ScheduledTask -TaskName $TaskName }

Write-Host "Codex Usage Collector installed." -ForegroundColor Green
Write-Host "Server : $Server"
Write-Host "Task   : $TaskName"
Write-Host "Log    : $LogFile"
