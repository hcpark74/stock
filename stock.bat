@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%scripts\start_main.ps1"

if not exist "%PS_SCRIPT%" (
  echo.
  echo   [실패] 실행 스크립트를 찾을 수 없습니다:
  echo     %PS_SCRIPT%
  echo.
  echo   해결 방법:
  echo     stock.bat은 저장소 폴더 안에서 실행해야 합니다.
  echo     바탕화면 등으로 복사해서 실행하면 이 오류가 납니다.
  echo     바로가기를 만들고 싶다면 파일을 복사하지 말고
  echo     마우스 오른쪽 버튼 - 바로 가기 만들기를 쓰세요.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "EXITCODE=%errorlevel%"

echo.
pause
exit /b %EXITCODE%
