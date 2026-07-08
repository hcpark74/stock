@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%scripts\fix_windows_acl_sandbox.ps1"

if not exist "%PS_SCRIPT%" (
  echo PowerShell script not found:
  echo   %PS_SCRIPT%
  exit /b 1
)

net session >nul 2>&1
if errorlevel 1 (
  echo This script must be run from an elevated Administrator shell.
  echo Open PowerShell as Administrator, then run:
  echo   cd /d "%SCRIPT_DIR%"
  echo   .\fix_windows_acl_sandbox.bat
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -RepoPath "%SCRIPT_DIR%." %*
exit /b %errorlevel%
