@echo off
rem cursor_team_setup.cmd - Windows launcher: run the JS on Cursor's own Node runtime (zero deps).
rem Usage: cursor_team_setup.cmd            (dry-run)
rem        cursor_team_setup.cmd --apply    (execute)
rem        cursor_team_setup.cmd --revert   (rollback)
rem 2026-09-03: a colleague hit "Cursor.exe not found" - the old launcher only knew two fixed paths.
rem Now: CURSOR_BIN > common install dirs (per-user / system / x86 / D: ) > registry App Paths >
rem      uninstall entry > Start-menu shortcut. Still nothing -> ask the user for the path.
setlocal EnableExtensions
rem chcp is safe here only because this file is pure ASCII (see package_team_setup.sh gate).
chcp 65001 >nul
set "SCRIPT_DIR=%~dp0"
set "EXE="

if defined CURSOR_BIN if exist "%CURSOR_BIN%" set "EXE=%CURSOR_BIN%"

if not defined EXE for %%P in (
  "%LOCALAPPDATA%\Programs\cursor\Cursor.exe"
  "%LOCALAPPDATA%\Programs\Cursor\Cursor.exe"
  "%ProgramFiles%\Cursor\Cursor.exe"
  "%ProgramFiles(x86)%\Cursor\Cursor.exe"
  "%LOCALAPPDATA%\cursor\Cursor.exe"
  "%USERPROFILE%\AppData\Local\Programs\cursor\Cursor.exe"
  "D:\Program Files\Cursor\Cursor.exe"
  "D:\Cursor\Cursor.exe"
) do if not defined EXE if exist %%P set "EXE=%%~P"

rem Registry: App Paths (per-user then machine)
if not defined EXE for %%R in (HKCU HKLM) do if not defined EXE (
  for /f "tokens=2,*" %%A in ('reg query "%%R\Software\Microsoft\Windows\CurrentVersion\App Paths\Cursor.exe" /ve 2^>nul ^| find "REG_"') do if exist "%%B" set "EXE=%%B"
)

rem Registry: uninstall entries (DisplayIcon points at Cursor.exe)
if not defined EXE for %%R in (HKCU HKLM) do if not defined EXE (
  for /f "delims=" %%K in ('reg query "%%R\Software\Microsoft\Windows\CurrentVersion\Uninstall" /s /f "Cursor" /d 2^>nul ^| findstr /i "HKEY_"') do if not defined EXE (
    for /f "tokens=2,*" %%A in ('reg query "%%K" /v DisplayIcon 2^>nul ^| find "REG_"') do (
      for /f "tokens=1 delims=," %%X in ("%%B") do if exist "%%~X" set "EXE=%%~X"
    )
  )
)

rem Start-menu shortcut target
if not defined EXE for %%L in (
  "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Cursor.lnk"
  "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Cursor.lnk"
) do if not defined EXE if exist %%L (
  for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).CreateShortcut('%%~L').TargetPath"`) do if exist "%%T" set "EXE=%%T"
)

rem Last resort: ask
if not defined EXE (
  echo.
  echo !! Cursor.exe not found. Right-click the Cursor icon - Properties - copy the full "Target" path and paste it below:
  set /p "EXE=Cursor.exe path: "
  set "EXE=%EXE:"=%"
)
if not exist "%EXE%" (
  echo !! Still not found: "%EXE%"  ^(or set env var CURSOR_BIN and retry^)
  exit /b 1
)

echo Cursor.exe = %EXE%
set ELECTRON_RUN_AS_NODE=1
"%EXE%" "%SCRIPT_DIR%cursor_team_setup.js" %*
endlocal
