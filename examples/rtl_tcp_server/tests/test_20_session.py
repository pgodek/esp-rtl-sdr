"""T2/T5: client lifecycle - reconnects, abrupt closes, stalled and competing clients."""
import pytest

from checks import assert_stream, record
from rtl_tcp_client import ClientError, Client, connect_retry


def test_t2_reconnect_graceful(rtl, target, metrics):
    for i in range(target.reconnects):
        c = rtl()
        target.setup(c)
        m = c.read_for(target.short, target.rate)
        assert_stream(m, "cycle %d" % (i + 1))
        c.close()
    metrics["cycles"] = target.reconnects


def test_t2_reconnect_after_rst(rtl, target, metrics):
    c = rtl()
    target.setup(c)
    assert_stream(c.read_for(target.short, target.rate), "first session")
    c.close(abrupt=True)                 # RST instead of FIN
    c2 = rtl()
    target.setup(c2)
    assert_stream(c2.read_for(target.short, target.rate), "session after RST")
    metrics["reconnect_s"] = round(c2.waited, 1)


def test_t5_stalled_client_is_dropped(rtl, target, metrics):
    """A client that stops reading must not block the server for ever."""
    c = rtl(rcvbuf=4096)                 # small buffer: back-pressure appears quickly
    target.setup(c)
    assert_stream(c.read_for(2, target.rate), "session before stall")
    # keep the socket OPEN and silent; a greeting for a new client can only arrive
    # once the server has given up on this one
    c2 = rtl(total=target.stall_wait)
    metrics["dropped_after_s"] = round(c2.waited, 1)
    target.setup(c2)
    assert_stream(c2.read_for(target.short, target.rate), "new client after the drop")


def test_second_client_waits_and_first_is_unaffected(rtl, target, metrics):
    c = rtl()
    target.setup(c)
    assert_stream(c.read_for(2, target.rate), "first client")
    with pytest.raises((ClientError, OSError)):
        Client(target.host, target.port, greeting_timeout=2.0)     # no greeting while busy
    assert_stream(c.read_for(target.short, target.rate), "first client after the 2nd attempt")
