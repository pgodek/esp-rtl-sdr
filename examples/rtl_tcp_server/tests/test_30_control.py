"""T3/T4/T6/T8: commands while streaming."""
import time

import pytest

from checks import MIN_RATIO, assert_stream, recovery_seconds, record, timeline
from rtl_tcp_client import CMD_AGC, CMD_FREQ, CMD_GAIN_INDEX, CMD_GAIN_MODE, CMD_RATE


def test_t3_sample_rate_changes(rtl, target, metrics):
    """Each change restarts the dongle (stop/start, about 1.5-3 s). The stream must be back at the
    new rate within 6 s of the command and then run at >= 95 % of it. The numbers per change are recorded
    before anything is asserted, so a failing run still shows where the time went."""
    c = rtl()
    target.setup(c, rate=2048000)
    assert_stream(c.read_for(target.short, 2048000), "start at 2.048 MS/s")
    problems = []
    for step, rate in enumerate((2400000, 1024000, 2400000), start=1):
        tag = "%d_%.3f" % (step, rate / 1e6)          # step number: the same rate occurs twice
        c.cmd(CMD_RATE, rate)
        m = c.read_for(6 if target.mock else 10, rate, first_data_timeout=15)
        rec = recovery_seconds(m["per_second"], rate)
        # seconds from the command to a stream at the new rate: silence before the first byte
        # (nothing buffered) + position inside the window
        total = None if rec is None else round((m["latency"] or 0.0) + rec, 1)
        metrics["timeline_MBps_" + tag] = timeline(m["per_second"])
        metrics["first_byte_after_s_" + tag] = round(m["latency"] or 0.0, 1)
        metrics["recovery_s_" + tag] = total
        if m["end"] != "time":
            problems.append("change %s: connection lost (%s)" % (tag, m["end"]))
            break
        if rec is None:
            problems.append("change %s: stream never settled at the new rate within the window" % tag)
            continue
        if total > 6:
            problems.append("change %s: back at the new rate only after %.1f s" % (tag, total))
        steady = [m["per_second"].get(i, 0) for i in range(rec, max(m["per_second"]))]
        if steady and sum(steady) / len(steady) < MIN_RATIO * rate * 2:
            problems.append("change %s: only %.0f%% of the rate after settling" % (
                tag, 100.0 * sum(steady) / len(steady) / (rate * 2)))
    assert not problems, "; ".join(problems)


def test_t4_fast_tuning_sweep(rtl, target, metrics):
    c = rtl()
    target.setup(c, freq=88_000_000)
    assert_stream(c.read_for(2, target.rate), "before sweep")
    sent = {"n": 0}

    def sweep():
        f, end = 88_000_000, time.time() + target.tune_seconds
        while time.time() < end:
            c.cmd(CMD_FREQ, f)
            sent["n"] += 1
            f = f + 25_000 if f < 108_000_000 else 88_000_000
            time.sleep(0.05)

    r = c.read_while(sweep)
    metrics.update(freq_commands=sent["n"], sweep_max_gap=round(r["max_gap"], 2))
    assert r["end"] == "time", "connection lost during the sweep (%s)" % r["end"]
    assert r["max_gap"] < 3.0, "gap of %.1f s during the sweep" % r["max_gap"]
    time.sleep(0.5)
    assert_stream(c.read_for(target.short, target.rate), "after sweep")


def test_t8_gain_and_agc_sweep(rtl, target, metrics):
    c = rtl()
    c.configure(target.rate, target.freq, auto_gain=False)
    assert_stream(c.read_for(2, target.rate), "before gain sweep")
    steps = c.gain_count or 28

    def sweep():
        for idx in range(steps):
            c.cmd(CMD_GAIN_INDEX, idx)
            time.sleep(0.35)
        c.cmd(CMD_GAIN_MODE, 0)
        time.sleep(0.5)
        c.cmd(CMD_AGC, 1)
        time.sleep(0.5)
        c.cmd(CMD_AGC, 0)
        time.sleep(0.5)
        if target.gain_index is not None:            # leave the dongle at the configured gain
            c.cmd(CMD_GAIN_MODE, 1)
            time.sleep(0.4)
            c.cmd(CMD_GAIN_INDEX, target.gain_index)
            time.sleep(0.6)

    r = c.read_while(sweep)
    metrics["gain_steps"] = steps
    assert r["end"] == "time", "connection lost during the gain sweep (%s)" % r["end"]
    assert r["max_gap"] < 3.0, "gap of %.1f s during the gain sweep" % r["max_gap"]
    assert_stream(c.read_for(target.short, target.rate), "after gain sweep")


def test_t6_hf_10mhz(rtl, target, metrics):
    """HF goes through the V4 upconverter; this checks the server accepts it and data flows
    (it does not judge reception quality)."""
    c = rtl()
    target.setup(c, freq=10_000_000)
    m = c.read_for(target.short + 2, target.rate)
    record(metrics, m)
    assert_stream(m, "HF stream")


@pytest.mark.parametrize("freq", [24_000_000, 433_920_000, 1_090_000_000])
def test_frequency_range(rtl, target, freq, metrics):
    c = rtl()
    target.setup(c, freq=freq)
    m = c.read_for(target.short, target.rate)
    record(metrics, m)
    assert_stream(m, "stream at %.3f MHz" % (freq / 1e6))
