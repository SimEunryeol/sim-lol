@echo off
chcp 65001 >nul
REM 코치 메모 생성 (Claude API). ANTHROPIC_API_KEY 필요
cd /d "%~dp0.."
if exist .venv\\Scripts\\activate call .venv\\Scripts\\activate
python -m sim_diamond.coach %*
pause
