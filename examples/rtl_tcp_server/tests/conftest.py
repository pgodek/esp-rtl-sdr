"""pytest configuration for the rtl_tcp_server test suite.

Without --host the suite runs against an in-process mock server (no hardware,
suitable for CI). With --host it tests a real board."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

import pytest

from mock_server import MockServer
from rtl_tcp_client import (CMD_FREQ, CMD_GAIN_INDEX, CMD_GAIN_MODE, CMD_RATE, Client, ClientError,
                            connect_retry)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
STATUS = re.compile(r"tcp ([\d.]+) MB/s, usb (\d+) sps, ring overrun (\d+) blk, "
                    r"slot starve (\d+) blk, usb err (\d+)")
# lines that mean the driver / USB went wrong (the benign init STALLs are not listed)
BAD_BOARD = re.compile(r"ctrl record rejected|ERR_FAULT|\bFAULT\b|stream ended|abort\(\)|"
                       r"Guru Meditation|rst:0x|Rebooting|hot retune EP0 failed|"
                       r"start_hz\(|out of memory|assert failed")

REPORT: list = []


def pytest_addoption(parser):
    g = parser.getgroup("rtl_tcp")
    g.addoption("--host", default=None, help="board IP; omit to test an in-process mock server")
    g.addoption("--port", type=int, default=1234)
    g.addoption("--rate", type=int, default=2400000, help="base sample rate")
    g.addoption("--freq", type=int, default=100_000_000, help="base frequency in Hz")
    g.addoption("--gain-index", type=int, default=None,
                help="use manual gain step N (0..27) instead of auto gain; auto may saturate with strong signals")
    g.addoption("--soak", type=int, default=None, help="soak seconds (default 120 on hardware, 4 on mock)")
    g.addoption("--serial", default=None, help="serial port of the board (needs pyserial)")
    g.addoption("--board-strict", action="store_true",
                help="fail a test when the board logged an error line during it")
    g.addoption("--manual", action="store_true", help="run tests that need you to unplug the dongle (use -s)")
    g.addoption("--report-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports"),
                help="where the .md/.json report is written (default: tests/reports)")
    g.addoption("--stall-wait", type=int, default=None,
                help="T5: seconds the server may take to drop a stalled client (default 60 on hardware)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes more than ~20 s on hardware")
    config.addinivalue_line("markers", "manual: needs a human (unplug the dongle); requires --manual and -s")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--manual"):
        skip = pytest.mark.skip(reason="needs --manual (and -s)")
        for item in items:
            if "manual" in item.keywords:
                item.add_marker(skip)


# ---------------------------------------------------------------- target ----
class Target:
    def __init__(self, host, port, mock, rate, freq, soak, gain_index=None):
        self.host, self.port, self.mock = host, port, mock
        self.rate, self.freq, self.soak = rate, freq, soak
        self.gain_index = gain_index
        # durations: short on the mock, realistic on hardware
        self.short = 2 if mock else 5
        self.tune_seconds = 4 if mock else 30
        self.high_seconds = 3 if mock else 20
        self.stall_wait = 20 if mock else 60     # how long the server may take to drop a stalled client
        self.reconnects = 2 if mock else 3

    def setup(self, client, rate=None, freq=None):
        """Send the usual client start-up commands: rate, frequency and gain."""
        client.cmd(CMD_RATE, rate or self.rate)
        client.cmd(CMD_FREQ, freq or self.freq)
        if self.gain_index is None:
            client.cmd(CMD_GAIN_MODE, 0)                  # auto
        else:
            client.cmd(CMD_GAIN_MODE, 1)                  # manual
            client.cmd(CMD_GAIN_INDEX, self.gain_index)


@pytest.fixture(scope="session")
def target(request):
    o = request.config.getoption
    mock = None
    if o("--host"):
        host, port, is_mock = o("--host"), o("--port"), False
    else:
        mock = MockServer().start()
        host, port, is_mock = "127.0.0.1", mock.port, True
    soak = o("--soak") if o("--soak") is not None else (4 if is_mock else 120)
    t = Target(host, port, is_mock, o("--rate"), o("--freq"), soak, o("--gain-index"))
    if o("--stall-wait") is not None:
        t.stall_wait = o("--stall-wait")
    t.mock_server = mock
    yield t
    if mock:
        mock.stop()


@pytest.fixture
def rtl(target):
    """Factory for connected clients; everything is closed at teardown."""
    made: list = []

    def make(rcvbuf=None, total=40.0):
        c, waited = connect_retry(target.host, target.port, total=total, rcvbuf=rcvbuf)
        c.waited = waited
        made.append(c)
        return c

    yield make
    for c in made:
        c.close(abrupt=True)
    time.sleep(0.3 if target.mock else 1.0)   # one client at a time: let the server return to accept()


@pytest.fixture
def metrics(request):
    """Test-local dict of numbers that end up in the report."""
    d: dict = {}
    request.node._metrics = d
    return d


# ------------------------------------------------------------ serial log ----
class SerialLog:
    def __init__(self, port, baud=115200):
        import serial  # type: ignore
        self.lines: list = []
        self.status = None
        self._run = True
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, baud, 0.2
        self.ser.dtr = False      # try not to reset the board when opening
        self.ser.rts = False
        self.ser.open()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        buf = b""
        while self._run:
            try:
                data = self.ser.read(1024)
            except Exception:    # noqa: BLE001
                return
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = ANSI.sub("", raw.decode("utf-8", "replace")).strip()
                if not text:
                    continue
                self.lines.append((time.time(), text))
                m = STATUS.search(text)
                if m:
                    self.status = {"ring_overrun": int(m.group(3)), "slot_starve": int(m.group(4)),
                                   "usb_err": int(m.group(5))}

    def mark(self):
        return len(self.lines), dict(self.status) if self.status else None

    def since(self, mark):
        idx, st0 = mark
        bad = [t for _, t in self.lines[idx:] if BAD_BOARD.search(t)]
        st1 = dict(self.status) if self.status else None
        delta = {k: st1[k] - st0[k] for k in st1} if (st0 and st1) else None
        return bad, delta

    def close(self):
        self._run = False
        try:
            self.ser.close()
        except Exception:    # noqa: BLE001
            pass


@pytest.fixture(scope="session")
def board(request):
    port = request.config.getoption("--serial")
    if not port:
        yield None
        return
    log = SerialLog(port)
    yield log
    out = os.path.join(request.config.getoption("--report-dir"), "board.log")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        for t, line in log.lines:
            f.write("%.2f %s\n" % (t, line))
    log.close()


@pytest.fixture(autouse=True)
def board_watch(request, board):
    """Record counter deltas and error lines the board logged during each test."""
    if board is None:
        yield
        return
    mark = board.mark()
    yield
    bad, delta = board.since(mark)
    request.node._board = {"errors": bad[:5], "error_count": len(bad), "counters": delta}
    if bad and request.config.getoption("--board-strict"):
        pytest.fail("board logged %d error line(s), first: %s" % (len(bad), bad[0]), pytrace=False)


# ---------------------------------------------------------------- report ----
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.skipped):
        entry = {"test": item.nodeid.split("::")[-1], "outcome": rep.outcome,
                 "seconds": round(rep.duration, 1),
                 "metrics": getattr(item, "_metrics", {}),
                 "board": getattr(item, "_board", None)}
        if rep.failed:
            entry["error"] = str(rep.longrepr).strip().splitlines()[-1][:300]
        if rep.skipped:
            entry["error"] = str(rep.longrepr[2]) if isinstance(rep.longrepr, tuple) else ""
        REPORT.append(entry)


def pytest_sessionfinish(session, exitstatus):
    if not REPORT:
        return
    cfg = session.config
    d = cfg.getoption("--report-dir")
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    host = cfg.getoption("--host") or "mock"
    lines = ["# rtl_tcp test report %s" % datetime.now().strftime("%Y-%m-%d %H:%M"), "",
             "Target: %s:%s, rate %.3f MS/s, freq %.3f MHz, gain %s" % (
                 host, cfg.getoption("--port") if cfg.getoption("--host") else "(mock)",
                 cfg.getoption("--rate") / 1e6, cfg.getoption("--freq") / 1e6,
                 "auto" if cfg.getoption("--gain-index") is None else "step %d" % cfg.getoption("--gain-index")),
             "", "| Test | Result | s | Metrics / notes |", "|---|---|---|---|"]
    for e in REPORT:
        notes = ", ".join("%s=%s" % (k, ("%.3g" % v) if isinstance(v, float) else v)
                          for k, v in e["metrics"].items())
        b = e.get("board")
        if b and b.get("counters"):
            notes += " | board counters delta %s" % json.dumps(b["counters"])
        if b and b.get("error_count"):
            notes += " | board errors: %s" % " / ".join(b["errors"][:2])
        if e.get("error"):
            notes += " | %s" % e["error"]
        lines.append("| %s | %s | %s | %s |" % (e["test"], e["outcome"].upper(), e["seconds"],
                                                notes.replace("|", "/")))
    counts = {k: sum(1 for e in REPORT if e["outcome"] == k) for k in ("passed", "failed", "skipped")}
    lines += ["", "passed %(passed)d, failed %(failed)d, skipped %(skipped)d" % counts]
    with open(os.path.join(d, "report_%s.md" % stamp), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(d, "report_%s.json" % stamp), "w") as f:
        json.dump(REPORT, f, indent=2, default=str)
    print("\nreport written to %s/report_%s.md" % (d, stamp))
