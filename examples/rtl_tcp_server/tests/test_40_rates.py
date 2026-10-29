"""T7: sample rates other than the default."""
import pytest

from checks import assert_stream, record


@pytest.mark.parametrize("rate", [1024000, 1920000])
def test_lower_rates(rtl, target, rate, metrics):
    c = rtl()
    target.setup(c, rate=rate)
    m = c.read_for(target.short, rate)
    record(metrics, m)
    assert_stream(m, "stream at %.3f MS/s" % (rate / 1e6))


@pytest.mark.slow
def test_t7_2560k(rtl, target, metrics):
    c = rtl()
    target.setup(c, rate=2560000)
    m = c.read_for(target.high_seconds, 2560000)
    record(metrics, m)
    assert_stream(m, "stream at 2.56 MS/s")


@pytest.mark.slow
def test_t7_3200k_informational(rtl, target, metrics):
    """The driver documents 3.2 MS/s as 'drops expected': only the connection must survive."""
    c = rtl()
    target.setup(c, rate=3200000)
    m = c.read_for(target.high_seconds, 3200000)
    record(metrics, m)
    assert m["end"] == "time", "connection lost at 3.2 MS/s (%s)" % m["end"]
    assert m["bytes"] > 0
