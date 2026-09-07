#!/usr/bin/env bash
# 1) 내 데이터 → 2) 벤치 → 3) 리포트. 중간에 끊겨도 다시 실행하면 이어서 진행한다.
set -euo pipefail
cd "$(dirname "$0")/.."
python -m sim_diamond.collect_me "$@"
python -m sim_diamond.collect_bench
python -m sim_diamond.report
