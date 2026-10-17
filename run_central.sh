#!/usr/bin/env bash
# 중앙 서버 실행 (리눅스 노트북). 처음 실행하면 .venv 를 만든다.
#   ./run_central.sh
# 대시보드: http://<이 노트북 Tailscale IP>:8100/ui
#
# 인증 없음 (POC). Tailscale·LAN 안에서만 쓴다.
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-central.txt
fi

# /rail/stop 을 전달할 파이 (lab-pi 의 Tailscale IP)
export PI_URL=${PI_URL:-http://100.112.77.123:8000}

exec .venv/bin/python -m uvicorn central.main:app --host 0.0.0.0 --port 8100
