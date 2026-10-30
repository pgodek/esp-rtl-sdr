#!/usr/bin/env bash
# Build the real rtl_tcp server for the host and serve the "ALL YOUR BASE" test signal in real time.
#   ./run_host.sh [port]       then connect SDR++ / GQRX (RTL-TCP source) to 127.0.0.1:<port> (default 1234)
#   DOWN=1 ./run_host.sh       send the text in reverse so it reads top to bottom (default: sent in order, which
#                              reads from the bottom to the top of the waterfall, letters turned to the left)
# Use 2.048 MS/s (the timing and the 100 kHz carrier spacing scale with the rate). The text sits in the middle
# +-350 kHz of the band; set a waterfall range of about 50 dB. One text column takes 2 s, the whole text 6 minutes.
# Needs numpy. The text is made of FM carriers on a low noise floor.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
out="${TMPDIR:-/tmp}/rtl_tcp_host_build"
mkdir -p "$out"
cxx="${CXX:-c++}"
"$cxx" -std=c++17 -O2 -Wall -Wextra -pthread \
    -include "$here/stubs/host_compat.h" -I"$here/stubs" -I"$here/../../main" -I"$repo/include" \
    -I"$repo/tests/host/stubs" "$here/fake_sdr.cpp" "$here/../../main/rtl_tcp_server.cpp" -o "$out/rtl_tcp_host"
python3 "$here/pattern.py" write "$out/pattern.bin" ${DOWN:+--down}
exec "$out/rtl_tcp_host" --pattern "$out/pattern.bin" --port "${1:-1234}" --pace
