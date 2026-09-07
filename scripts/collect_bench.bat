@echo off
chcp 65001 >nul
REM 벤치마크 표본 수집
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python -m sim_diamond.collect_bench %*
pause
