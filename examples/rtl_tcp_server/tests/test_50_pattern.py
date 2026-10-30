"""T6: data integrity of the real server code against a known test signal, without a dongle.

rtl_tcp_server.cpp is compiled for the host (host_server/) together with a fake esp_rtl_sdr driver
that delivers the reference signal of host_server/pattern.py: a looping I/Q stream whose waterfall
shows "ALL YOUR BASE ARE BELONG TO US" (rotated by 90 degrees, reading downwards). The tests read the
TCP stream and compare it bit for bit with the signal the driver produced, so any byte the server
drops, repeats, reorders or shifts (which would swap I and Q) is caught. Needs a C++ compiler.

To look at the signal in SDR++ (rate 1.024-2.4 MS/s works well):
    python3 host_server/pattern.py write /tmp/pattern.bin
    <build as in the fixture below>  &&  ./rtl_tcp_host --pattern /tmp/pattern.bin --pace
    # SDR++ -> RTL-TCP source -> 127.0.0.1:1234
"""
from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE / "host_server"))
import pattern as P  # noqa: E402

from rtl_tcp_client import CMD_RATE  # noqa: E402

COL_BYTES = 2 * P.COL_SAMPLES                  # bytes per text column (about 4 MB)


@pytest.fixture(scope="module")
def table():
    return P.build_table()


@pytest.fixture(scope="module")
def host_bin(tmp_path_factory):
    cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        pytest.skip("no C++ compiler")
    d = tmp_path_factory.mktemp("host_server")
    exe = d / "rtl_tcp_host"
    hs = HERE / "host_server"
    cmd = [cxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-pthread",
           "-include", str(hs / "stubs" / "host_compat.h"),
           f"-I{hs / 'stubs'}", f"-I{HERE.parent / 'main'}", f"-I{REPO / 'include'}",
           f"-I{REPO / 'tests' / 'host' / 'stubs'}",
           str(hs / "fake_sdr.cpp"), str(HERE.parent / "main" / "rtl_tcp_server.cpp"), "-o", str(exe)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    P.write_file(str(d / "pattern.bin"))
    return exe, d / "pattern.bin"


class HostServer:
    def __init__(self, exe, pat, pace):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        args = [str(exe), "--pattern", str(pat), "--port", str(self.port)] + (["--pace"] if pace else [])
        self.p = subprocess.Popen(args, stderr=subprocess.PIPE, text=True)
        self.log: list[str] = []
        ready = threading.Event()

        def drain():
            for line in self.p.stderr:
                self.log.append(line.rstrip())
                if "listening on port" in line:
                    ready.set()

        threading.Thread(target=drain, daemon=True).start()
        assert ready.wait(10), "server did not start: " + "\n".join(self.log)

    def connect(self):
        assert self.p.poll() is None, f"server died (exit {self.p.returncode}):\n" + "\n".join(self.log[-8:])
        c = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        greeting = b""
        while len(greeting) < 12:
            greeting += c.recv(12 - len(greeting))
        assert greeting[:4] == b"RTL0"
        return c

    def close(self):
        self.p.kill()
        self.p.wait()


@pytest.fixture
def server(host_bin):
    made = []

    def make(pace=False):
        s = HostServer(*host_bin, pace)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def read_exact(c, n, timeout=20):
    buf = bytearray()
    c.settimeout(timeout)
    while len(buf) < n:
        chunk = c.recv(min(1 << 20, n - len(buf)))
        assert chunk, f"stream ended after {len(buf)} of {n} bytes"
        buf += chunk
    return bytes(buf)


def first_diff(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(0, n, 4096):
        if a[i:i + 4096] != b[i:i + 4096]:
            for j in range(i, min(i + 4096, n)):
                if a[j] != b[j]:
                    return j
    return -1 if len(a) == len(b) else n


# --- the signal itself -------------------------------------------------------------------------

def test_signal_paints_the_text(table):
    """Every lit pixel of the bitmap is a strong tone, every unlit one is silent."""
    bm = P.bitmap_columns()
    assert len(table) == len(bm)
    for c, w in enumerate(table):
        lv = P.pixel_levels(w)
        for p in range(P.GLYPH_H):
            if bm[c][p]:
                assert lv[p] > P.LIT_MIN, f"column {c} pixel {p} should be lit ({lv[p]:.2f})"
            else:
                assert lv[p] < P.DARK_MAX, f"column {c} pixel {p} should be dark ({lv[p]:.2f})"


def test_checker_catches_damage(table):
    """The tester must itself be able to fail: a lost byte, a swapped I/Q pair, a duplicated block."""
    start = 4 * COL_BYTES + 6 * 2 * P.N + 2 * 7          # arbitrary even offset inside column 4
    good = P.expected(table, start, 3 * COL_BYTES)
    blocks, changes, err = P.follow(table, good)
    assert err is None and changes >= 2 and blocks > 500, (blocks, changes, err)

    cut = len(good) // 2
    assert P.follow(table, good[:cut] + good[cut + 1:])[2] is not None                   # one byte lost
    assert P.follow(table, good[:cut] + good[cut:cut + 2 * P.N] + good[cut:])[2] is not None   # block repeated
    swapped = bytearray(good)
    swapped[cut:cut + 2], swapped[cut + 2:cut + 4] = good[cut + 1:cut + 2] + good[cut:cut + 1], \
        good[cut + 3:cut + 4] + good[cut + 2:cut + 3]
    assert P.follow(table, bytes(swapped))[2] is not None                                 # I/Q swapped


# --- the server --------------------------------------------------------------------------------

def test_stream_is_bit_exact(server, table):
    """First 14 MB (three text columns, three column changes) arrive identical to what the driver produced."""
    srv = server()
    c = srv.connect()
    n = 14 * 1024 * 1024
    got = read_exact(c, n)
    c.close()
    exp = P.expected(table, 0, n)
    assert got == exp, f"first difference at byte {first_diff(got, exp)}"


def test_each_connection_starts_the_signal_again(server, table):
    srv = server()
    for _ in range(3):
        c = srv.connect()
        got = read_exact(c, 1 << 20)
        c.close()
        assert got == P.expected(table, 0, len(got))
        time.sleep(0.3)


def test_slow_client_loses_nothing(server, table):
    """A client that stops reading for 1.5 s (TCP backpressure, no drop) gets a gapless stream."""
    srv = server()
    c = srv.connect()
    head = read_exact(c, 2 << 20)
    time.sleep(1.5)
    tail = read_exact(c, 6 << 20)
    c.close()
    got = head + tail
    assert got == P.expected(table, 0, len(got)), f"first difference at byte {first_diff(got, P.expected(table, 0, len(got)))}"


def test_rate_change_cuts_cleanly(server, table):
    """A sample-rate change stops and restarts the stream. The cut must fall on a sample boundary
    (an odd byte count would swap I and Q for the rest of the session); what comes before the cut and
    what comes after it must both be undamaged signal."""
    srv = server(pace=True)
    c = srv.connect()
    head = read_exact(c, COL_BYTES + (1 << 19))        # past column 0, so column 0 is a clean restart marker
    c.sendall(struct.pack(">BI", CMD_RATE, 1_024_000))
    data = bytearray(head)
    marker = table[0] * 2
    restart = -1
    deadline = time.time() + 30
    while time.time() < deadline and restart < 0:
        c.settimeout(5)
        chunk = c.recv(1 << 20)
        assert chunk, "stream ended"
        data += chunk
        restart = data.find(marker, len(head) - 4 * P.N)   # column 0 again = stream rewound
    assert restart >= 0, "no restart seen after the rate change"
    data += read_exact(c, 2 << 20)
    c.close()
    assert restart % 2 == 0, f"restart at odd byte {restart}: I and Q would be swapped"
    assert bytes(data[:restart]) == P.expected(table, 0, restart), "damaged data before the restart"
    after = bytes(data[restart:])
    assert after == P.expected(table, 0, len(after)), \
        f"damaged data after the restart, first difference {first_diff(after, P.expected(table, 0, len(after)))}"
