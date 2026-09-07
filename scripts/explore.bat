@echo off
chcp 65001 >nul
REM 탐색 단계 진행 현황
cd /d "%~dp0.."
if exist .venv\\Scripts\\activate call .venv\\Scripts\\activate
python -m sim_diamond.explore %*
pause
