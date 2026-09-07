#!/usr/bin/env bash
# 로컬 대시보드 (오늘/탐색/진단/코치)
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.web "$@"
