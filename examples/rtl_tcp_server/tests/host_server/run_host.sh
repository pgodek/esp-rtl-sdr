#!/usr/bin/env bash
# Build the real rtl_tcp server for the host and serve the "ALL YOUR BASE" test signal in real time.
#   ./run_host.sh [port]       then connect SDR++ / GQRX (RTL-TCP source) to 127.0.0.1:<port> (default 1234)
# Use 1.024 - 2.4 MS/s. In SDR++ the text reads downwards in the waterfall, glyph tops pointing to the right
# (highest frequency); a small blob on each edge counts the text column. If it is mirrored, your client
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
python3 "$here/pattern.py" write "$out/pattern.bin"
exec "$out/rtl_tcp_host" --pattern "$out/pattern.bin" --port "${1:-1234}" --pace
