#!/usr/bin/env bash
# 진단 결과를 모아 GitHub 에 올린다
set -euo pipefail
cd "$(dirname "$0")/.."
python -m sim_diamond.status
git add logs/status.txt
git commit -m "status: $(date '+%F %T')" >/dev/null 2>&1 || true
git push
echo '완료. 클로드에게 "보냈어" 라고 하세요.'
