#!/usr/bin/env bash
# 탐색 단계 진행 현황
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.explore "$@"
