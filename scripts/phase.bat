@echo off
chcp 65001 >nul
REM 탐색/집중 단계 관리
cd /d "%~dp0.."
if exist .venv\\Scripts\\activate call .venv\\Scripts\\activate
python -m sim_diamond.phase %*
pause
