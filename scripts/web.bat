@echo off
chcp 65001 >nul
REM 로컬 대시보드 (오늘/탐색/진단/코치). 브라우저가 자동으로 열린다.
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
start "" http://127.0.0.1:8765
python -m sim_diamond.web %*
pause
