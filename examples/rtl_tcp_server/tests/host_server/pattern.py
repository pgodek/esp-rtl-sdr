"""Reference test signal: "ALL YOUR BASE ARE BELONG TO US" painted into the waterfall.

The stream is a looping CU8 I/Q signal. Each *column* of the text bitmap is one
fixed spectrum held for COL_SAMPLES samples; the waterfall (time runs downwards in SDR++)
therefore shows the text rotated by 90 degrees, reading top to bottom, glyph tops pointing to
the right (highest frequency).

Layout (complex baseband, N = 1024 FFT bins, bin spacing = rate / 1024):

* 7 glyph rows x 64 bins = 448 bins centred on DC, glyph row 0 (top) at the highest bins,
  8 tones per pixel.
* Two "address" combs outside the text so that every column is unique and the receiver can
  find out where it is in the loop: 12 tones on the low side (column // 16), 16 on the high
  side (column % 16). They show up as a small blob moving along each edge of the waterfall.

Every tone sits on an exact bin, so a column's waveform is periodic in N samples. The signal
is a table of COLS x N samples; sample t is  table[(t // COL_SAMPLES) % COLS][t % N].
That makes the host server, the Python checker and any other implementation agree
bit-for-bit without sharing floating-point code: they share the table file.
"""
from __future__ import annotations

import cmath
import math
import struct

N = 1024                    # samples per period of a column waveform
COL_SAMPLES = N * 256       # samples a column lasts (about 128 ms at 2.048 MS/s)
TEXT = "ALL YOUR BASE ARE BELONG TO US"
GLYPH_W, GLYPH_H, GAP = 5, 7, 1
PIXEL_BINS = 64
TONES_PER_PIXEL = 8
TEXT_LOW_BIN = -(GLYPH_H * PIXEL_BINS) // 2          # -224
MAGIC = b"RTLP"
PEAK = 118                                            # max |I-127.5| / |Q-127.5|
ID_GAIN = 3.0                                         # id tones are brighter than text tones
HI_ID_BIN0, HI_ID_STEP = -500, 8                      # low side: column // 16 (0..11)
LO_ID_BIN0, LO_ID_STEP = 260, 16                      # high side: column % 16 (0..15)

FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    " ": ("00000",) * 7,
}


def bitmap_columns() -> list[list[int]]:
    """Text bitmap as a list of columns; each column is 7 ints (row 0 = glyph top)."""
    cols = []
    for ch in TEXT:
        g = FONT[ch]
        for x in range(GLYPH_W):
            cols.append([int(g[y][x]) for y in range(GLYPH_H)])
        cols.append([0] * GLYPH_H)              # gap after every char (also after the last: loop gap)
    return cols


COLS = len(bitmap_columns())                    # 180 text columns + the 6 gap columns inside it


def _tone(table: list[complex], bin_: int, phase: float, gain: float) -> None:
    for s in range(N):
        table[s] += gain * cmath.exp(1j * (2 * math.pi * bin_ * s / N + phase))


def _tone_fast(out: list[complex], bin_: int, phase: float, gain: float, cs: list[complex]) -> None:
    ph = cmath.exp(1j * phase) * gain
    k = bin_ % N
    for s in range(N):
        out[s] += ph * cs[(k * s) % N]


def build_table() -> list[bytes]:
    """COLS byte strings of 2*N bytes (interleaved I, Q), already quantised."""
    cs = [cmath.exp(2j * math.pi * s / N) for s in range(N)]
    ntones = GLYPH_H * TONES_PER_PIXEL
    pixel_wave = []                                          # one waveform per glyph row
    idx = 0
    for p in range(GLYPH_H):
        w = [0j] * N
        base = TEXT_LOW_BIN + (GLYPH_H - 1 - p) * PIXEL_BINS
        for t in range(TONES_PER_PIXEL):
            b = base + t * (PIXEL_BINS // TONES_PER_PIXEL) + PIXEL_BINS // TONES_PER_PIXEL // 2
            _tone_fast(w, b, math.pi * idx * idx / ntones, 1.0, cs)   # Schroeder phases: low crest factor
            idx += 1
        pixel_wave.append(w)
    hi_wave, lo_wave = [], []
    for i in range(12):
        w = [0j] * N
        _tone_fast(w, HI_ID_BIN0 + i * HI_ID_STEP, 0.0, ID_GAIN, cs)
        hi_wave.append(w)
    for i in range(16):
        w = [0j] * N
        _tone_fast(w, LO_ID_BIN0 + i * LO_ID_STEP, 0.0, ID_GAIN, cs)
        lo_wave.append(w)

    cols = bitmap_columns()
    waves = []
    for c in range(COLS):
        w = [0j] * N
        for p in range(GLYPH_H):
            if cols[c][p]:
                pw = pixel_wave[p]
                for s in range(N):
                    w[s] += pw[s]
        hw, lw = hi_wave[c // 16], lo_wave[c % 16]
        for s in range(N):
            w[s] += hw[s] + lw[s]
        waves.append(w)
    peak = max(max(abs(v.real), abs(v.imag)) for w in waves for v in w)
    scale = PEAK / peak                                       # same amplitude for every column
    out = []
    for w in waves:
        b = bytearray(2 * N)
        for s, v in enumerate(w):
            b[2 * s] = min(255, max(0, int(round(127.5 + scale * v.real))))
            b[2 * s + 1] = min(255, max(0, int(round(127.5 + scale * v.imag))))
        out.append(bytes(b))
    return out


def write_file(path: str) -> list[bytes]:
    table = build_table()
    with open(path, "wb") as f:
        f.write(MAGIC + struct.pack("<III", N, COL_SAMPLES, len(table)))
        for c in table:
            f.write(c)
    return table


def read_file(path: str) -> list[bytes]:
    with open(path, "rb") as f:
        head = f.read(16)
        assert head[:4] == MAGIC, "not a pattern file"
        n, col_samples, cols = struct.unpack("<III", head[4:])
        assert (n, col_samples, cols) == (N, COL_SAMPLES, COLS), "pattern file does not match pattern.py"
        return [f.read(2 * N) for _ in range(cols)]


def expected(table: list[bytes], start_byte: int, count: int) -> bytes:
    """Bytes [start_byte, start_byte+count) of the stream that starts at sample 0 (CU8, 2 B/sample)."""
    out = bytearray()
    pos = start_byte
    end = start_byte + count
    col_bytes = 2 * COL_SAMPLES
    while pos < end:
        col = (pos // col_bytes) % len(table)
        in_col = pos % col_bytes
        n = min(end - pos, col_bytes - in_col)
        w = table[col]
        off = in_col % (2 * N)
        # repeat the period enough times to cover n bytes from offset off
        reps = (off + n + 2 * N - 1) // (2 * N)
        out += (w * reps)[off:off + n]
        pos += n
    return bytes(out)


def locate(table: list[bytes], window: bytes):
    """Find (column, byte offset inside the period) of a window of >= 2N bytes, or None."""
    assert len(window) >= 2 * N
    probe = window[:2 * N]
    for c, w in enumerate(table):
        k = (w + w).find(probe)
        if k >= 0:
            return c, k
    return None


def follow(table: list[bytes], data: bytes):
    """Continuity check of an arbitrarily aligned stream piece.

    Aligns to a period boundary, then every 2N-byte block must be the waveform of the
    current column or of the next one, never going back; a column lasts exactly
    COL_SAMPLES / N blocks. Returns (blocks_checked, column_changes, error_or_None)."""
    loc = locate(table, data)
    if loc is None:
        return 0, 0, "start of the data matches no column"
    col, off = loc
    blocks_per_col = COL_SAMPLES // N
    pos = (2 * N - off) % (2 * N)                    # next period boundary
    # blocks already seen of the current column are unknown; the first column change tells us
    checked = changes = 0
    run = None                                       # blocks seen in the current column since it started
    while pos + 2 * N <= len(data):
        blk = data[pos:pos + 2 * N]
        if blk == table[col]:
            if run is not None:
                run += 1
                if run > blocks_per_col:
                    return checked, changes, f"column {col} lasts longer than {blocks_per_col} blocks at byte {pos}"
        elif blk == table[(col + 1) % len(table)]:
            if run is not None and run != blocks_per_col:
                return checked, changes, (f"column {col} ended after {run} blocks instead of {blocks_per_col} "
                                          f"(byte {pos}): samples lost or repeated")
            col = (col + 1) % len(table)
            run = 1
            changes += 1
        else:
            return checked, changes, f"block at byte {pos} matches neither column {col} nor the next"
        checked += 1
        pos += 2 * N
    return checked, changes, None


def pixel_levels(wave: bytes) -> list[float]:
    """Magnitude of the first tone of each glyph row (index 0 = glyph top = highest frequency)."""
    z = [complex(wave[2 * s] - 127.5, wave[2 * s + 1] - 127.5) for s in range(N)]
    out = []
    for p in range(GLYPH_H):
        b = TEXT_LOW_BIN + (GLYPH_H - 1 - p) * PIXEL_BINS + PIXEL_BINS // TONES_PER_PIXEL // 2
        acc = sum(z[s] * cmath.exp(-2j * math.pi * b * s / N) for s in range(N)) / N
        out.append(abs(acc))
    return out


def render_ascii(table: list[bytes]) -> str:
    """The waterfall as text: one line per column (time runs downwards), frequency increases to the
    right. A lit pixel has a tone about 100x stronger than an unlit one."""
    lines = []
    for w in table:
        lv = pixel_levels(w)
        lines.append("".join("##" if lv[p] > 3.0 else "  " for p in range(GLYPH_H - 1, -1, -1)))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "write":
        write_file(sys.argv[2])
        print("wrote", sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "ascii":
        print(render_ascii(build_table()))
    else:
        print("usage: pattern.py write FILE | ascii")
