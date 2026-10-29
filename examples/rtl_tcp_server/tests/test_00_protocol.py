"""T0: greeting and protocol robustness."""
import time

from checks import assert_stream, record
from rtl_tcp_client import CMD_FREQ, CMD_RATE, CMD_PPM


def test_t0_greeting(rtl, metrics):
    c = rtl()
    metrics.update(tuner=c.tuner_name, gain_steps=c.gain_count)
    assert c.tuner in (5, 6), "unexpected tuner type %d" % c.tuner
    assert c.gain_count > 0, "no gain steps reported"


def test_stream_starts_without_any_command(rtl, target, metrics):
    """A client that sends nothing (like `nc`) still gets data after a short delay."""
    c = rtl()
    m = c.read_for(target.short, 2048000, first_data_timeout=10)
    record(metrics, m)
    assert m["bytes"] > 0, "no data without commands"
    metrics["first_data_s"] = round(m["latency"], 2)


def test_command_split_across_tcp_segments(rtl, target):
    """A command arriving one byte at a time is still understood (no desync)."""
    c = rtl()
    c.cmd(CMD_RATE, target.rate)
    raw = bytes([CMD_FREQ]) + int(target.freq).to_bytes(4, "big")
    for b in raw:
        c.s.sendall(bytes([b]))
        time.sleep(0.02)
    m = c.read_for(target.short, target.rate)
    assert_stream(m, "stream after split command")


def test_unknown_and_unsupported_commands_are_ignored(rtl, target):
    c = rtl()
    target.setup(c)
    for cid, p in ((0x7F, 1), (0x09, 1), (0x0A, 1), (0x07, 0), (0x06, 0x00010005), (CMD_FREQ, 0)):
        c.cmd(cid, p)       # unknown, direct sampling, offset tuning, test mode, IF gain, freq 0
    m = c.read_for(target.short, target.rate)
    assert_stream(m, "stream after unsupported commands")


def test_ppm_command(rtl, target, metrics):
    """A ppm change retunes the dongle, which pauses the stream for a moment; it must come back."""
    c = rtl()
    target.setup(c)
    assert_stream(c.read_for(2, target.rate), "stream before ppm")
    c.cmd(CMD_PPM, 10)
    c.cmd(CMD_PPM, 0)
    settle = c.read_for(2, target.rate)                    # includes the retune pause
    metrics["settle_max_gap"] = round(settle["max_gap"], 2)
    assert settle["end"] == "time", "connection lost after ppm change (%s)" % settle["end"]
    assert settle["max_gap"] < 3.0, "pause of %.1f s after ppm change" % settle["max_gap"]
    assert_stream(c.read_for(target.short, target.rate), "stream after ppm change")
