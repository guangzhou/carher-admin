@echo off
rem cursor_team_setup.cmd - Windows launcher: run the JS on Cursor's own Node runtime (zero deps).
rem Usage: cursor_team_setup.cmd            (dry-run)
rem        cursor_team_setup.cmd --apply    (execute)
rem        cursor_team_setup.cmd --revert   (rollback)
setlocal
chcp 65001 >nul
set "SCRIPT_DIR=%~dp0"

if defined CURSOR_BIN (
  set "EXE=%CURSOR_BIN%"
) else (
  set "EXE=%LOCALAPPDATA%\Programs\cursor\Cursor.exe"
  if not exist "%EXE%" set "EXE=%ProgramFiles%\Cursor\Cursor.exe"
)

if not exist "%EXE%" (
  echo !! Cursor.exe not found. Set CURSOR_BIN=C:\path\to\Cursor.exe and retry.
  exit /b 1
)

set ELECTRON_RUN_AS_NODE=1
"%EXE%" "%SCRIPT_DIR%cursor_team_setup.js" %*
endlocal
