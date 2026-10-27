Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "nsDialogs.nsh"

!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef RUNTIME_DIR
  !error "RUNTIME_DIR is required"
!endif
!ifndef WITHOUT_TOKEN
  !ifndef TOKEN_FILE
    !error "TOKEN_FILE is required"
  !endif
!endif
!ifndef SERVER_URL
  !error "SERVER_URL is required"
!endif
!ifndef SERVER_PORT
  !error "SERVER_PORT is required"
!endif
!ifndef TARGET_ACCOUNT_KEY
  !error "TARGET_ACCOUNT_KEY is required"
!endif
!ifndef OUTPUT_EXE
  !error "OUTPUT_EXE is required"
!endif

Name "Codex Usage Collector"
OutFile "${OUTPUT_EXE}"
InstallDir "$PROGRAMFILES64\Codex Usage Collector"
InstallDirRegKey HKLM "Software\CodexUsageCollector" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show
BrandingText "Codex Usage Monitor"
Var ServerAddress
Var ServerDialog
Var ServerInput
Var LegacyAccountKey
!ifdef WITHOUT_TOKEN
Var TokenInput
Var CollectorToken
!endif

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"
!insertmacro MUI_PAGE_WELCOME
Page custom ServerPageCreate ServerPageLeave
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetRegView 64
  SetShellVarContext all
  StrCpy $ServerAddress "${SERVER_URL}"
  StrCpy $LegacyAccountKey "${TARGET_ACCOUNT_KEY}"
  ReadRegStr $0 HKLM "Software\CodexUsageCollector" "Server"
  ${If} $0 != ""
    StrCpy $ServerAddress $0
  ${EndIf}
  ReadRegStr $0 HKLM "Software\CodexUsageCollector" "AccountKey"
  ${If} $0 != ""
    StrCpy $LegacyAccountKey $0
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Codex Usage Collector requires 64-bit Windows."
    Abort
  ${EndIf}
FunctionEnd

Function ServerPageCreate
  !insertmacro MUI_HEADER_TEXT "Monitoring server" "Choose where this collector sends usage. You can change this later."
  nsDialogs::Create 1018
  Pop $ServerDialog
  ${If} $ServerDialog == error
    Abort
  ${EndIf}
  ${NSD_CreateLabel} 0 0 100% 32u "Central server URL (http:// or https://)."
  Pop $0
  ${NSD_CreateText} 0 40u 100% 14u "$ServerAddress"
  Pop $ServerInput
!ifdef WITHOUT_TOKEN
  ${NSD_CreateLabel} 0 62u 100% 28u "Collector token. Leave empty to preserve the token from an existing installation."
  Pop $0
  ${NSD_CreatePassword} 0 94u 100% 14u ""
  Pop $TokenInput
!endif
  nsDialogs::Show
FunctionEnd

Function ServerPageLeave
  ${NSD_GetText} $ServerInput $ServerAddress
  StrCpy $0 $ServerAddress 7
  StrCpy $1 $ServerAddress 8
  ${If} $0 != "http://"
  ${AndIf} $1 != "https://"
    MessageBox MB_ICONEXCLAMATION "Enter an HTTP(S) server URL."
    Abort
  ${EndIf}
!ifdef WITHOUT_TOKEN
  ${NSD_GetText} $TokenInput $CollectorToken
  ${If} $CollectorToken == ""
    IfFileExists "$APPDATA\CodexUsageCollector\collector.token" keep_existing_token
    MessageBox MB_ICONEXCLAMATION "Enter the collector token supplied by your monitoring server administrator."
    Abort
    keep_existing_token:
  ${EndIf}
!endif
FunctionEnd

Section "Machine-wide collector" SecMain
  SetRegView 64
  SetShellVarContext all

  ; Stop the old collector before replacing its executable runtime or scripts.
  ; The ProgramData log and attribution state are deliberately preserved.
  nsExec::ExecToLog 'schtasks.exe /End /TN "CodexUsageCollectorMachine"'
  nsExec::ExecToLog 'schtasks.exe /Delete /TN "CodexUsageCollectorMachine" /F'
  Sleep 1500

  SetOutPath "$INSTDIR\runtime"
  File /r "${RUNTIME_DIR}\*.*"
  SetOutPath "$INSTDIR"
  File "${SOURCE_DIR}\collector.py"
  File "${SOURCE_DIR}\collector_windows_machine.py"
  File "${SOURCE_DIR}\packaging\windows\register_machine_task.ps1"
  File "${SOURCE_DIR}\packaging\windows\configure.ps1"
  File "${SOURCE_DIR}\packaging\windows\configure.cmd"
  CreateShortCut "$SMPROGRAMS\Codex Usage Collector Settings.lnk" "$INSTDIR\configure.cmd"

  CreateDirectory "$APPDATA\CodexUsageCollector"
  SetOutPath "$APPDATA\CodexUsageCollector"
!ifdef WITHOUT_TOKEN
  ${If} $CollectorToken != ""
    FileOpen $0 "$APPDATA\CodexUsageCollector\collector.token" w
    FileWrite $0 "$CollectorToken$\r$\n"
    FileClose $0
  ${EndIf}
!else
  File /oname=collector.token "${TOKEN_FILE}"
!endif
  nsExec::ExecToLog 'icacls.exe "$APPDATA\CodexUsageCollector\collector.token" /inheritance:r /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)"'

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\CodexUsageCollector" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\CodexUsageCollector" "Server" "$ServerAddress"
  WriteRegStr HKLM "Software\CodexUsageCollector" "AccountKey" "$LegacyAccountKey"
  WriteRegDWORD HKLM "Software\CodexUsageCollector" "LogMaxBytes" 5242880
  WriteRegDWORD HKLM "Software\CodexUsageCollector" "LogBackups" 3
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "DisplayName" "Codex Usage Collector"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "Publisher" "Codex Usage Monitor"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "NoRepair" 1

  nsExec::ExecToLog 'netsh.exe advfirewall firewall delete rule name="Codex Usage Collector outbound"'

  ; NSIS is a 32-bit process. Sysnative launches 64-bit PowerShell so it reads
  ; the same 64-bit HKLM registry view written above.
  nsExec::ExecToStack '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\register_machine_task.ps1" -UpdateConnection'
  Pop $0
  Pop $1
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Could not register the machine collector task.$\r$\n$1"
    Abort
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetRegView 64
  SetShellVarContext all
  nsExec::ExecToLog 'schtasks.exe /End /TN "CodexUsageCollectorMachine"'
  nsExec::ExecToLog 'schtasks.exe /Delete /TN "CodexUsageCollectorMachine" /F'
  nsExec::ExecToLog 'netsh.exe advfirewall firewall delete rule name="Codex Usage Collector outbound"'
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector"
  DeleteRegKey HKLM "Software\CodexUsageCollector"
  Delete "$SMPROGRAMS\Codex Usage Collector Settings.lnk"
  RMDir /r "$INSTDIR"
  RMDir /r "$APPDATA\CodexUsageCollector"
SectionEnd
