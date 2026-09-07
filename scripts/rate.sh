#!/usr/bin/env bash
# 최근 경기 재미 점수 기록. 예: scripts\rate.bat 4 "재밌었다"
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.rate "$@"
