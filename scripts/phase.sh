#!/usr/bin/env bash
# 탐색/집중 단계 관리
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.phase "$@"
