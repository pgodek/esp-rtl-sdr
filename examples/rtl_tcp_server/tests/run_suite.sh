#!/usr/bin/env bash
# Run the rtl_tcp test suite.
#   ./run_suite.sh                         # mock server, no hardware (fast, used by CI)
#   ./run_suite.sh 192.168.1.117           # real board, ~10 min
#   ./run_suite.sh 192.168.1.117 --soak=900 --serial=/dev/cu.usbserial-XXXX
# Everything after the host is passed to pytest (e.g. -k soak, -m "not slow").
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
# (re)install when pytest is missing, e.g. after an interrupted first run
if ! .venv/bin/python -c "import pytest" 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi
args=()
if [[ $# -gt 0 && "$1" != -* ]]; then
  args+=("--host=$1"); shift
fi
exec .venv/bin/python -m pytest ${args[@]+"${args[@]}"} "$@"
