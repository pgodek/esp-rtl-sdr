"""T1: steady streaming."""
from checks import assert_stream, assert_steady, record


def test_t1_soak(rtl, target, metrics):
    c = rtl()
    target.setup(c)
    m = c.read_for(target.soak, target.rate)
    record(metrics, m)
    metrics["soak_s"] = target.soak
    if m["per_second"]:
        metrics["min_second_MBps"] = round(min(m["per_second"][s] for s in sorted(m["per_second"])[1:-1] or [0]) / 1e6, 2)
    assert_stream(m, "soak")
    assert_steady(m, target.rate)
    if m["clip"] is not None:
        metrics["clipped_pct"] = round(m["clip"] * 100, 1)
