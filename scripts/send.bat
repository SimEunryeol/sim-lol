@echo off
chcp 65001 >nul
REM 진단 결과를 모아 GitHub 에 올린다. 클로드가 거기서 읽는다.
cd /d "%~dp0.."

echo [1/3] 상태 수집 중...
python -m sim_diamond.status
if errorlevel 1 goto :err

echo.
echo [2/3] GitHub 에 올리는 중...
git add logs/status.txt
git commit -m "status: %DATE% %TIME%" >nul 2>&1
git push
if errorlevel 1 goto :nopush

echo.
echo [3/3] 완료. 클로드에게 "보냈어" 라고만 하시면 됩니다.
pause
exit /b 0

:nopush
echo.
echo push 에 실패했습니다(권한 문제일 수 있습니다).
echo logs\status.txt 를 메모장으로 열어 내용을 붙여넣어 주세요:
notepad logs\status.txt
pause
exit /b 0

:err
echo.
echo 상태 수집에 실패했습니다. 위 메시지를 붙여넣어 주세요.
pause
exit /b 1
