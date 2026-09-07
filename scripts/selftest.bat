@echo off
chcp 65001 >nul
REM 네트워크/API 키 없이 파싱→지표→리포트 전 구간 검증
cd /d "%~dp0.."
if exist .venv\Scripts\activate call .venv\Scripts\activate
python tests\test_pipeline.py %*
pause
