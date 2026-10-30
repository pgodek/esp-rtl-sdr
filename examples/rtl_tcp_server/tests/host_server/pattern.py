"""Reference test signal: "ALL YOUR BASE ARE BELONG TO US" painted into the waterfall with FM carriers.

The stream is a looping CU8 I/Q signal. Each *column* of the text bitmap is held for COL_SAMPLES
samples (2 s at 2.048 MS/s); a lit pixel is a frequency-modulated carrier that looks like an FM station:
100 kHz apart (at 2.048 MS/s), +-35 kHz deviation, a made-up "audio" signal that differs for every column
and carrier, and a 19 kHz stereo pilot. 100 kHz is narrower than a broadcast channel on purpose: pixels come
out about as wide as they are long on a 20 fps waterfall. Everything sits on a low noise floor that covers the
whole spectrum.

Reading direction. SDR++ draws the newest line at the top and lets older ones scroll down, so text sent in
order reads from the bottom to the top of the waterfall (like the title on a book spine, letters turned 90 degrees
to the left, glyph tops at the lowest frequency). That is the default (READ_UP): the text is transmitted in its
own order, so you can read it while it is being sent. `pattern.py write FILE --down` instead sends the columns in
reverse with the glyph tops at the highest frequency, which reads top to bottom (letters turned to the right) but
sends the end of the text first.

Because the columns differ only in which carriers are on, switching them hard would splash a click across the
whole band. The first N samples of every column are therefore a raised-cosine crossfade from the previous column.

The signal is a table: for each of COLS columns one crossfade block (`trans`) and one steady block (`wave`), each
N samples long and periodic in N. Sample t of the stream is
    trans[c][s]            for s <  N
    wave[c][s % N]         for s >= N          with c = (t // COL_SAMPLES) % COLS, s = t % COL_SAMPLES.
The host server, the Python checker and any other implementation agree bit for bit by sharing the table file, so
no floating-point code has to match between languages. Every column's waveform is unique (its own audio and
noise), which lets a receiver find out where in the loop it is.
"""
from __future__ import annotations

import struct
from typing import NamedTuple

import numpy as np

N = 16384                   # samples per block, and the period of a column's waveform
COL_SAMPLES = N * 250       # 4096000: one column lasts 2 s at 2.048 MS/s (about 40 rows of a 20 fps waterfall)
BLOCKS_PER_COL = COL_SAMPLES // N
TEXT = "ALL YOUR BASE ARE BELONG TO US"
READ_UP = True              # see the module docstring; --down turns it off
GLYPH_W, GLYPH_H, GAP = 5, 7, 1
MAGIC = b"RTLP"
SEED = 20261030

PITCH = 800                 # carrier spacing in bins: 800 * rate / N = 100 kHz at 2.048 MS/s
DEV = 280                   # peak deviation in bins: 35 kHz
AUDIO_K = (4, 120)          # audio components at k * rate / N = 0.5 ... 15 kHz
PILOT_K, PILOT = 152, 0.10  # 19 kHz stereo pilot, 10 % of the deviation
CARRIER_PEAK = 105.0        # peak I or Q of the strongest column, in counts around 127.5
NOISE_SIGMA = 1.2           # noise floor: standard deviation of I and of Q, in counts (about -40 dBFS)
LIT_DB, DARK_DB = 15.0, 6.0  # pixel_levels() bounds: level of a pixel's band above the noise floor

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


class Column(NamedTuple):
    trans: bytes            # 2*N bytes: crossfade from the previous column
    wave: bytes             # 2*N bytes: steady state, repeats until the next column


def bitmap_columns() -> list[list[int]]:
    """Text bitmap as a list of columns in transmission order; each column is 7 ints (row 0 = glyph top)."""
    cols = []
    for ch in TEXT:
        g = FONT[ch]
        for x in range(GLYPH_W):
            cols.append([int(g[y][x]) for y in range(GLYPH_H)])
        cols.append([0] * GLYPH_H)              # gap after every char (also after the last: loop gap)
    return cols if READ_UP else cols[::-1]


COLS = len(bitmap_columns())


def slot_of_row(p: int) -> int:
    """Frequency slot (0 = lowest) of glyph row p (0 = glyph top)."""
    return p if READ_UP else GLYPH_H - 1 - p


def _fm_carrier(col: int, slot: int) -> np.ndarray:
    """N complex samples of one FM 'station'; its audio depends on (column, slot)."""
    rng = np.random.default_rng([SEED, col, slot])
    n = np.arange(N)
    audio = np.zeros(N)
    for _ in range(6):
        k = int(rng.integers(AUDIO_K[0], AUDIO_K[1] + 1))
        audio += rng.uniform(0.3, 1.0) * np.sin(2 * np.pi * k * n / N + rng.uniform(0, 2 * np.pi))
    audio *= (1.0 - PILOT) / np.max(np.abs(audio))
    audio += PILOT * np.sin(2 * np.pi * PILOT_K * n / N + rng.uniform(0, 2 * np.pi))
    # every component has an integer number of periods in N, so the sum is zero-mean and the phase returns to 0
    phase = 2 * np.pi * DEV / N * (np.cumsum(audio) - audio)        # exclusive cumulative sum
    centre = (slot - GLYPH_H // 2) * PITCH
    return np.exp(1j * (2 * np.pi * centre * n / N + phase))


def _quantise(x: np.ndarray) -> bytes:
    out = np.empty(2 * N, np.uint8)
    out[0::2] = np.clip(np.rint(127.5 + x.real), 0, 255)
    out[1::2] = np.clip(np.rint(127.5 + x.imag), 0, 255)
    return out.tobytes()


def build_table() -> list[Column]:
    bm = bitmap_columns()
    carriers = []
    for c in range(COLS):
        w = np.zeros(N, complex)
        for p in range(GLYPH_H):
            if bm[c][p]:
                w += _fm_carrier(c, slot_of_row(p))
        carriers.append(w)
    scale = CARRIER_PEAK / max(max(np.abs(w.real).max(), np.abs(w.imag).max()) for w in carriers)
    waves = []
    for c in range(COLS):
        rng = np.random.default_rng([SEED, c, 999])
        noise = NOISE_SIGMA * (rng.standard_normal(N) + 1j * rng.standard_normal(N))
        waves.append(scale * carriers[c] + noise)
    g = 0.5 - 0.5 * np.cos(np.pi * (np.arange(N) + 0.5) / N)        # 0 -> 1 raised cosine
    table = []
    for c in range(COLS):
        prev = waves[(c - 1) % COLS]
        table.append(Column(_quantise((1 - g) * prev + g * waves[c]), _quantise(waves[c])))
    return table


def write_file(path: str) -> list[Column]:
    table = build_table()
    with open(path, "wb") as f:
        f.write(MAGIC + struct.pack("<III", N, COL_SAMPLES, len(table)))
        for c in table:
            f.write(c.trans)
            f.write(c.wave)
    return table


def read_file(path: str) -> list[Column]:
    with open(path, "rb") as f:
        head = f.read(16)
        assert head[:4] == MAGIC, "not a pattern file"
        n, col_samples, cols = struct.unpack("<III", head[4:])
        assert (n, col_samples, cols) == (N, COL_SAMPLES, COLS), "pattern file does not match pattern.py"
        return [Column(f.read(2 * N), f.read(2 * N)) for _ in range(cols)]


def expected(table: list[Column], start_byte: int, count: int) -> bytes:
    """Bytes [start_byte, start_byte+count) of the stream that starts at sample 0 (CU8, 2 B/sample)."""
    out = bytearray()
    pos = start_byte
    end = start_byte + count
    col_bytes = 2 * COL_SAMPLES
    while pos < end:
        col = table[(pos // col_bytes) % len(table)]
        in_col = pos % col_bytes
        n = min(end - pos, col_bytes - in_col)
        if in_col < 2 * N:
            n = min(n, 2 * N - in_col)
            out += col.trans[in_col:in_col + n]
        else:
            off = in_col % (2 * N)
            reps = (off + n + 2 * N - 1) // (2 * N)
            out += (col.wave * reps)[off:off + n]
        pos += n
    return bytes(out)


def locate(table: list[Column], window: bytes):
    """Find (column, byte offset inside the period) of a window of 2N bytes lying in a steady block, or None."""
    assert len(window) >= 2 * N
    probe = window[:2 * N]
    for c, col in enumerate(table):
        k = (col.wave + col.wave).find(probe)
        if k >= 0:
            return c, k
    return None


def follow(table: list[Column], data: bytes):
    """Continuity check of an arbitrarily aligned piece of the stream.

    Finds where it is, aligns to a block boundary, then every 2N-byte block must be the steady block of the
    current column or the crossfade block of the next one (which starts that column), never going back. A
    column is one crossfade block plus BLOCKS_PER_COL - 1 steady blocks. Returns
    (blocks_checked, column_changes, error_or_None)."""
    n2 = 2 * N
    loc = None
    for skip in (0, n2, 2 * n2):
        if len(data) < skip + n2:
            break
        loc = locate(table, data[skip:skip + n2])
        if loc:
            base = skip
            break
    if loc is None:
        return 0, 0, "start of the data matches no column"
    col, off = loc
    pos = base + ((n2 - off) % n2)                   # next block boundary
    checked = changes = 0
    run = None                                       # blocks of the current column seen since its crossfade
    while pos + n2 <= len(data):
        blk = data[pos:pos + n2]
        nxt = (col + 1) % len(table)
        if blk == table[col].wave:
            if run is not None:
                run += 1
                if run > BLOCKS_PER_COL:
                    return checked, changes, f"column {col} lasts longer than {BLOCKS_PER_COL} blocks at byte {pos}"
        elif blk == table[nxt].trans:
            if run is not None and run != BLOCKS_PER_COL:
                return checked, changes, (f"column {col} ended after {run} blocks instead of {BLOCKS_PER_COL} "
                                          f"(byte {pos}): samples lost or repeated")
            col = nxt
            run = 1
            changes += 1
        else:
            return checked, changes, f"block at byte {pos} matches neither column {col} nor the start of the next"
        checked += 1
        pos += n2
    return checked, changes, None


def pixel_levels(col: Column) -> list[float]:
    """Level in dB above the noise floor of each glyph row's frequency slot (index 0 = glyph top)."""
    z = np.frombuffer(col.wave, np.uint8).astype(float) - 127.5
    spec = np.abs(np.fft.fft(z[0::2] + 1j * z[1::2])) ** 2
    idx = lambda b: np.arange(b - 200, b + 200) % N                # +-25 kHz around a slot centre
    floor = spec[np.arange(3600, 7900)].mean()                     # beyond +-3.5 slots: nothing but noise
    return [float(10 * np.log10(spec[idx((slot_of_row(p) - GLYPH_H // 2) * PITCH)].mean() / floor))
            for p in range(GLYPH_H)]


def render_ascii(table: list[Column]) -> str:
    """The waterfall as text, as SDR++ shows it: the newest line (last column sent) on top, frequency
    increasing to the right."""
    lines = []
    for col in table:
        lv = pixel_levels(col)
        row_of_slot = {slot_of_row(p): p for p in range(GLYPH_H)}
        lines.append("".join("##" if lv[row_of_slot[s]] > LIT_DB else "  " for s in range(GLYPH_H)))
    return "\n".join(lines[::-1])


if __name__ == "__main__":
    import sys
    if len(sys.argv) in (3, 4) and sys.argv[1] == "write":
        if "--down" in sys.argv[3:]:
            READ_UP = False
        write_file(sys.argv[2])
        print("wrote", sys.argv[2])
    elif len(sys.argv) in (2, 3) and sys.argv[1] == "ascii":
        if "--down" in sys.argv[2:]:
            READ_UP = False
        print(render_ascii(build_table()))
    else:
        print("usage: pattern.py write FILE [--down] | ascii [--down]")
