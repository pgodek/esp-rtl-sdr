#!/usr/bin/env bash
# Build the real rtl_tcp server for the host and serve the "ALL YOUR BASE" test signal in real time.
#   ./run_host.sh [port]       (FORWARD=1 ./run_host.sh if your client scrolls oldest-on-top) then connect SDR++ / GQRX (RTL-TCP source) to 127.0.0.1:<port> (default 1234)
# Use 1.024 - 2.4 MS/s (2.048 is the one the timing is tuned for). In SDR++ the text sits in the middle 14 % of the
# band and reads downwards in the waterfall, glyph tops pointing to the right (highest frequency); zoom in on
# the centre (the columns are sent in reverse because SDR++ draws the newest line on top) and use a waterfall range of about 40 dB; one text column takes ~1 s, the whole text ~3 minutes.
# A small blob on each edge counts the text column. If it is mirrored, your client
# mirrors the frequency axis or the time direction.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
out="${TMPDIR:-/tmp}/rtl_tcp_host_build"
mkdir -p "$out"
cxx="${CXX:-c++}"
"$cxx" -std=c++17 -O2 -Wall -Wextra -pthread \
    -include "$here/stubs/host_compat.h" -I"$here/stubs" -I"$here/../../main" -I"$repo/include" \
    -I"$repo/tests/host/stubs" "$here/fake_sdr.cpp" "$here/../../main/rtl_tcp_server.cpp" -o "$out/rtl_tcp_host"
python3 "$here/pattern.py" write "$out/pattern.bin" ${FORWARD:+--forward}
exec "$out/rtl_tcp_host" --pattern "$out/pattern.bin" --port "${1:-1234}" --pace
