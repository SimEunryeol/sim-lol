#!/usr/bin/env bash
# 최신 코드 받기 (템플릿 로컬 수정 자동 처리)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env.example ] && cp .env.example .env.example.bak
git checkout -- .env.example 2>/dev/null || true
git pull
python -m sim_diamond.doctor
