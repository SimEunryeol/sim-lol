@echo off
chcp 65001 >nul
REM 내 계정 수집. 예: scripts\collect_me.bat --limit 20
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python -m sim_diamond.collect_me %*
pause
