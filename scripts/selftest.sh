#!/usr/bin/env bash
# 네트워크 없이 파싱 → 지표 → 리포트 전 구간 검증 (합성 데이터)
set -euo pipefail
cd "$(dirname "$0")/.."
exec python tests/test_pipeline.py "$@"
