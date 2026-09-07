@echo off
chcp 65001 >nul
REM 수집한 데이터가 말이 되는지 점검
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python -m sim_diamond.check %*
pause
