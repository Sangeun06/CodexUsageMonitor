[CmdletBinding()]
param([string]$Server = "", [string]$TokenFile = "", [switch]$Show)

$ErrorActionPreference = "Stop"
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Machine = Test-Path -LiteralPath (Join-Path $InstallDir "collector_windows_machine.py")
if ($Machine) {
    $Admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $Admin -and -not $Show) {
        if ($Server -or $TokenFile) { throw "Run this command in an administrator PowerShell window." }
        Start-Process powershell.exe -Verb RunAs -ArgumentList ('-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $MyInvocation.MyCommand.Path + '"') -Wait
        exit
    }
    $DataDir = Join-Path $env:ProgramData "CodexUsageCollector"
    $Python = Join-Path $InstallDir "runtime\python.exe"
    $Collector = Join-Path $InstallDir "collector_windows_machine.py"
    $Legacy = Get-ItemProperty "HKLM:\Software\CodexUsageCollector"
    $CurrentServer = [string]$Legacy.Server
    $AccountKey = [string]$Legacy.AccountKey
} else {
    $DataDir = Join-Path $env:APPDATA "CodexUsageCollector"
    $Task = Get-ScheduledTask -TaskName "CodexUsageCollector-$($env:USERNAME)"
    $Python = $Task.Actions[0].Execute
    $LegacyArguments = [string]$Task.Actions[0].Arguments
    $Collector = Join-Path $InstallDir "collector.py"
    $CurrentServer = ""
    $AccountKey = ""
    if ($LegacyArguments -match '"--server"\s+"([^"]+)"') { $CurrentServer = $Matches[1] }
    if ($LegacyArguments -match '"--account-key"\s+"([^"]+)"') { $AccountKey = $Matches[1] }
}
if ((Split-Path -Leaf $Python) -eq "pythonw.exe") { $Python = Join-Path (Split-Path -Parent $Python) "python.exe" }
if ((Split-Path -Leaf $Python) -eq "pyw.exe") { $Python = Join-Path (Split-Path -Parent $Python) "py.exe" }
$ConfigFile = Join-Path $DataDir "collector-config.json"
$CurrentToken = Join-Path $DataDir "collector.token"
if (Test-Path -LiteralPath $ConfigFile) {
    $Configuration = Get-Content -Raw -LiteralPath $ConfigFile | ConvertFrom-Json
    $CurrentServer = [string]$Configuration.server
    if ($Configuration.token_file) { $CurrentToken = [string]$Configuration.token_file }
    if ($Configuration.account_key) { $AccountKey = [string]$Configuration.account_key }
}
if ($Show) {
    Write-Host "Server : $CurrentServer"
    Write-Host "Config : $ConfigFile"
    Write-Host "Token file : $CurrentToken"
    exit
}
if (-not $Server) {
    Add-Type -AssemblyName Microsoft.VisualBasic
    $Server = [Microsoft.VisualBasic.Interaction]::InputBox("Central server URL (http:// or https://)", "Codex Usage Collector Settings", $CurrentServer)
    if (-not $Server) { exit }
}
if (-not $TokenFile) { $TokenFile = $CurrentToken }
$Arguments = @($Collector, "--config-file", $ConfigFile, "--configure", "--server", $Server, "--token-file", $TokenFile)
if ($AccountKey) { $Arguments += @("--account-key", $AccountKey) }
# pyw.exe needs its launcher version flag; native python executables do not.
if ((Split-Path -Leaf $Python) -eq "py.exe") { $Arguments = @("-3") + $Arguments }
& $Python @Arguments
if ($LASTEXITCODE -ne 0) { throw "Collector configuration was rejected." }
if ($Machine) {
    $Uri = [Uri]$Server
    & netsh.exe advfirewall firewall delete rule name="Codex Usage Collector outbound" | Out-Null
    & netsh.exe advfirewall firewall add rule name="Codex Usage Collector outbound" dir=out action=allow program=(Join-Path $InstallDir "runtime\pythonw.exe") enable=yes profile=any protocol=TCP remoteport=$($Uri.Port) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Settings saved, but the outbound firewall rule could not be updated." }
    Set-ItemProperty "HKLM:\Software\CodexUsageCollector" -Name Server -Value $Server
}
Write-Host "Saved. The collector uses the new server on its next scan." -ForegroundColor Green
