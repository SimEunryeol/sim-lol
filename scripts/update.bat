@echo off
chcp 65001 >nul
REM 최신 코드 받기. 템플릿(.env.example)을 건드렸어도 알아서 처리한다.
cd /d "%~dp0.."

echo [1/3] 로컬 수정 백업 후 되돌리기
if exist .env.example copy /Y .env.example .env.example.bak >nul
git checkout -- .env.example 2>nul
git checkout -- requirements.txt 2>nul

echo [2/3] 최신 코드 받기
git pull
if errorlevel 1 goto :err

echo [3/3] 설정 확인
python -m sim_diamond.doctor

echo.
echo 완료. .env 원본이 필요하면 .env.example.bak 에 남아 있습니다.
pause
exit /b 0

:err
echo.
echo git pull 이 실패했습니다. 위 메시지를 그대로 복사해 주세요.
echo 로컬 수정이 남아 있다면:  git status
pause
exit /b 1
