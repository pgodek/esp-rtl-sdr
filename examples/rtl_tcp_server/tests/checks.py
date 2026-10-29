"""Shared assertions."""
from __future__ import annotations

import warnings

MIN_RATIO = 0.95      # delivered / expected bytes
MAX_GAP_S = 1.5       # longest silence tolerated in a steady stream


def record(metrics: dict, m: dict, prefix: str = "") -> None:
    for k in ("mbps", "ratio", "max_gap", "latency", "std"):
        if m.get(k) is not None:
            metrics[prefix + k] = round(m[k], 3)


def assert_stream(m: dict, what: str = "stream", ratio: float = MIN_RATIO, max_gap: float = MAX_GAP_S):
    """The result of Client.read_for() describes a healthy stream."""
    assert m["end"] not in ("no data", "eof", "reset"), "%s ended: %s" % (what, m["end"])
    assert m["ratio"] >= ratio, "%s delivered only %.0f%% of the expected data (%.2f MB/s)" % (
        what, m["ratio"] * 100, m["mbps"])
    assert m["max_gap"] <= max_gap, "%s had a %.2f s gap" % (what, m["max_gap"])
    # A dead ADC returns one or two values; a weak signal at low gain is not an error.
    assert m.get("distinct") is None or m["distinct"] > 2, (
        "%s data is constant (%d distinct byte values, std %.2f)" % (what, m["distinct"], m["std"]))
    if m["std"] is not None and m["std"] < 1.0:
        warnings.warn("%s: very low signal (std %.2f): antenna or gain?" % (what, m["std"]))
    if m.get("clip") is not None and m["clip"] > 0.2:
        warnings.warn("%s: %.0f%% of the samples are clipped: gain too high?" % (what, m["clip"] * 100))


def assert_steady(m: dict, rate: int, floor: float = 0.7):
    """No single full second of the stream fell far below the expected rate."""
    secs = sorted(m["per_second"])
    full = [s for s in secs[1:-1]]
    for s in full:
        got = m["per_second"][s]
        assert got >= floor * rate * 2, "second %d delivered only %.0f%% of the expected data" % (
            s, 100.0 * got / (rate * 2))


def timeline(per_second: dict, upto: int | None = None) -> list:
    """Delivered MB per second, in order (for the report)."""
    n = (max(per_second) + 1) if per_second else 0
    return [round(per_second.get(i, 0) / 1e6, 2) for i in range(n if upto is None else min(n, upto))]


def recovery_seconds(per_second: dict, rate: int, floor: float = 0.9, ceil: float = 1.25, hold: int = 2):
    """First second (counted from the first received byte) from which the stream delivers
    between floor and ceil x the expected rate for `hold` consecutive seconds; None if it
    never does. The ceiling keeps data of the OLD, faster rate from counting as recovered.
    The last, partial second is ignored."""
    if not per_second:
        return None
    last = max(per_second)
    lo, hi = floor * rate * 2, ceil * rate * 2
    for s in range(0, last - hold + 1):
        if all(lo <= per_second.get(s + i, 0) <= hi for i in range(hold)):
            return s
    return None
