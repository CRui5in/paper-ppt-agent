@echo off
REM Paper PPT Agent — Windows entry point.
REM
REM Delegates to the interactive PowerShell launcher, which:
REM   1. Checks required prerequisites (uv, Node.js) and offers to install
REM      any that are missing via an arrow-key menu.
REM   2. Shows optional Agent runtimes (Claude Code / Codex) status.
REM   3. Syncs dependencies and starts the dev stack.

setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "LAUNCHER=%ROOT%\scripts\start-launcher.ps1"

if not exist "%LAUNCHER%" (
  echo Launcher script not found: %LAUNCHER%
  exit /b 1
)

REM Find powershell (powershell.exe on Windows; falls back to pwsh if present).
set "PS_EXE="
where pwsh >nul 2>nul && set "PS_EXE=pwsh"
if not defined PS_EXE where powershell >nul 2>nul && set "PS_EXE=powershell"
if not defined PS_EXE (
  echo Neither PowerShell ^(pwsh^) nor Windows PowerShell ^(powershell^) was found.
  echo Windows PowerShell is included with Windows 10/11. Please open
  echo "Add optional features" or repair your Windows installation.
  exit /b 1
)

REM ExecutionPolicy is scoped to this process only (-Scope Process) so we never
REM change the user's global policy.
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%" %*

set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
