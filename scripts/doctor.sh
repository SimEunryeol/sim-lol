#!/usr/bin/env bash
# .env 와 API 키 진단
set -euo pipefail
cd "$(dirname "$0")/.."
exec python -m sim_diamond.doctor
