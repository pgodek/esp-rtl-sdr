"""T9: needs a human. Run with --manual -s."""
import pytest

from checks import assert_stream
from rtl_tcp_client import ClientError, connect_retry


@pytest.mark.manual
def test_t9_unplug_and_replug_dongle(rtl, target, metrics):
    c = rtl()
    target.setup(c)
    assert_stream(c.read_for(3, target.rate), "before unplug")
    input("\n>>> UNPLUG the RTL-SDR from the board now, then press Enter ")
    m = c.read_for(10, target.rate, first_data_timeout=10)
    metrics["after_unplug"] = m["end"]
    c.close(abrupt=True)
    input(">>> PLUG the RTL-SDR back in, wait 3 s, then press Enter ")
    try:
        c2, _ = connect_retry(target.host, target.port, total=45)
    except ClientError as e:
        pytest.fail("no service after replug (%s); press RESET on the board" % e)
    target.setup(c2)
    assert_stream(c2.read_for(4, target.rate), "stream after replug")
    c2.close()
