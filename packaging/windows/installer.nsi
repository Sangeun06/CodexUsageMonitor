Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef RUNTIME_DIR
  !error "RUNTIME_DIR is required"
!endif
!ifndef TOKEN_FILE
  !error "TOKEN_FILE is required"
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

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Codex Usage Collector requires 64-bit Windows."
    Abort
  ${EndIf}
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

  CreateDirectory "$APPDATA\CodexUsageCollector"
  SetOutPath "$APPDATA\CodexUsageCollector"
  File /oname=collector.token "${TOKEN_FILE}"
  nsExec::ExecToLog 'icacls.exe "$APPDATA\CodexUsageCollector\collector.token" /inheritance:r /grant:r "*S-1-5-18:(F)" "*S-1-5-32-544:(F)"'

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\CodexUsageCollector" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\CodexUsageCollector" "Server" "${SERVER_URL}"
  WriteRegStr HKLM "Software\CodexUsageCollector" "AccountKey" "${TARGET_ACCOUNT_KEY}"
  WriteRegDWORD HKLM "Software\CodexUsageCollector" "LogMaxBytes" 5242880
  WriteRegDWORD HKLM "Software\CodexUsageCollector" "LogBackups" 3
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "DisplayName" "Codex Usage Collector"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "Publisher" "Codex Usage Monitor"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexUsageCollector" "NoRepair" 1

  nsExec::ExecToLog 'netsh.exe advfirewall firewall delete rule name="Codex Usage Collector outbound"'
  nsExec::ExecToLog 'netsh.exe advfirewall firewall add rule name="Codex Usage Collector outbound" dir=out action=allow program="$INSTDIR\runtime\pythonw.exe" enable=yes profile=any protocol=TCP remoteport=${SERVER_PORT}'

  ; NSIS is a 32-bit process. Sysnative launches 64-bit PowerShell so it reads
  ; the same 64-bit HKLM registry view written above.
  nsExec::ExecToStack '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\register_machine_task.ps1"'
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
  RMDir /r "$INSTDIR"
  RMDir /r "$APPDATA\CodexUsageCollector"
SectionEnd
