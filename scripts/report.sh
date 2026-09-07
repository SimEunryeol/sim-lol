#!/usr/bin/env bash
# data/report_YYYYMMDD.md 생성
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.report "$@"
