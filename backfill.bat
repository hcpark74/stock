@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%scripts\run_backfill.ps1"

if not exist "%PS_SCRIPT%" (
  echo.
  echo   [실패] 실행 스크립트를 찾을 수 없습니다:
  echo     %PS_SCRIPT%
  echo.
  echo   해결 방법:
  echo     backfill.bat은 저장소 폴더 안에서 실행해야 합니다.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "EXITCODE=%errorlevel%"

echo.
pause
exit /b %EXITCODE%
