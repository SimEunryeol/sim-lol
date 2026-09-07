@echo off
chcp 65001 >nul
REM 리포트 생성
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python -m sim_diamond.report %*
pause
