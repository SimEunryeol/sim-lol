@echo off
chcp 65001 >nul
REM .env 와 API 키가 제대로 읽히는지, 라이엇이 뭐라고 답하는지 진단한다
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python -m sim_diamond.doctor
pause
