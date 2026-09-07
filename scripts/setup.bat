@echo off
chcp 65001 >nul
REM 최초 1회 설치: 가상환경 + 패키지 + .env 생성
cd /d "%~dp0.."

echo [1/3] 가상환경 만들기
if not exist .venv (python -m venv .venv || goto :err)
call .venv\Scripts\activate || goto :err

echo [2/3] 패키지 설치
python -m pip install --quiet --upgrade pip
python -m pip install -r requirements.txt || goto :err

echo [3/3] .env 준비
if not exist .env (
  copy .env.example .env >nul
  echo   .env 를 만들었습니다. 메모장이 열리면 RIOT_API_KEY= 뒤에 키를 붙여넣고 저장하세요.
  echo   ^(저장할 때 파일 형식을 "모든 파일"로 두어야 .env.txt 가 되지 않습니다^)
  notepad .env
) else (
  echo   .env 가 이미 있습니다. 건너뜁니다.
)

echo.
echo 설치 끝. 이제 scripts\selftest.bat 으로 코드가 도는지 먼저 확인하세요.
pause
exit /b 0

:err
echo.
echo 설치 중 오류가 났습니다. 위 메시지를 그대로 복사해 주세요.
pause
exit /b 1
