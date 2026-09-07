#!/usr/bin/env bash
# 코치 메모 생성 (Claude API). ANTHROPIC_API_KEY 필요
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.coach "$@"
