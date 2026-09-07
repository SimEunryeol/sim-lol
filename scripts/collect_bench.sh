#!/usr/bin/env bash
# 벤치마크 샘플 수집. 예: scripts/collect_bench.sh --tiers SILVER GOLD --per-tier 15
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.collect_bench "$@"
