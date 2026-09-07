#!/usr/bin/env bash
# 내 계정 수집. 인자는 그대로 전달된다. 예: scripts/collect_me.sh --limit 20
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.collect_me "$@"
