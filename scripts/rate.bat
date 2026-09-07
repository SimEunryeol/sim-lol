@echo off
chcp 65001 >nul
REM 최근 경기 재미 점수 기록. 예: scripts\rate.bat 4 "재밌었다"
cd /d "%~dp0.."
if exist .venv\\Scripts\\activate call .venv\\Scripts\\activate
python -m sim_diamond.rate %*
pause
