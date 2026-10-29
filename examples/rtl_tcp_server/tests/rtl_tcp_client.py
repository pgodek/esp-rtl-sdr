"""Minimal rtl_tcp client used by the test suite (standard library only)."""
from __future__ import annotations

import socket
import struct
import threading
import time

CMD_FREQ, CMD_RATE, CMD_GAIN_MODE, CMD_GAIN, CMD_PPM = 0x01, 0x02, 0x03, 0x04, 0x05
CMD_AGC, CMD_GAIN_INDEX = 0x08, 0x0D

TUNER_NAMES = {5: "R820T", 6: "R828D"}


class ClientError(Exception):
    pass


def sample_stats(sample: bytes) -> dict:
    """Mean / standard deviation / clipped fraction of a CU8 sample."""
    if len(sample) < 1024:
        return {"mean": None, "std": None, "clip": None, "distinct": None}
    n = len(sample)
    mean = sum(sample) / n
    var = sum((b - mean) ** 2 for b in sample) / n
    clip = (sample.count(0) + sample.count(255)) / n
    return {"mean": mean, "std": var ** 0.5, "clip": clip, "distinct": len(set(sample))}


class Client:
    def __init__(self, host: str, port: int, greeting_timeout: float = 5.0, rcvbuf: int | None = None):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if rcvbuf:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)   # before connect
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(greeting_timeout)
        try:
            s.connect((host, port))
        except OSError:
            s.close()
            raise
        self.s = s
        try:
            hdr = self._recvn(12, greeting_timeout)
        except Exception:
            s.close()
            raise
        if hdr[:4] != b"RTL0":
            s.close()
            raise ClientError("bad greeting %r" % hdr[:4])
        self.tuner, self.gain_count = struct.unpack(">II", hdr[4:])

    @property
    def tuner_name(self) -> str:
        return TUNER_NAMES.get(self.tuner, "unknown(%d)" % self.tuner)

    def _recvn(self, n: int, timeout: float) -> bytes:
        deadline = time.time() + timeout
        buf = b""
        while len(buf) < n:
            left = deadline - time.time()
            if left <= 0:
                raise ClientError("timeout waiting for greeting")
            self.s.settimeout(left)
            try:
                chunk = self.s.recv(n - len(buf))
            except socket.timeout:
                raise ClientError("timeout waiting for greeting")
            if not chunk:
                raise ClientError("connection closed during greeting")
            buf += chunk
        return buf

    def cmd(self, cmd_id: int, param: int) -> None:
        self.s.sendall(struct.pack(">BI", cmd_id, param & 0xFFFFFFFF))

    def configure(self, rate: int, freq: int, auto_gain: bool = True) -> None:
        self.cmd(CMD_RATE, rate)
        self.cmd(CMD_FREQ, freq)
        self.cmd(CMD_GAIN_MODE, 0 if auto_gain else 1)

    def read_for(self, seconds: float, rate: int, first_data_timeout: float = 15.0) -> dict:
        """Read for `seconds` counted from the first received byte."""
        self.s.settimeout(0.5)
        t0 = time.time()
        first = last = None
        total = 0
        max_gap = 0.0
        sample = b""
        sample_after = min(2.0, seconds / 2)     # skip the first moments (gain / AGC settling)
        per_second: dict = {}
        end = "time"
        while True:
            now = time.time()
            if first is None and now - t0 > first_data_timeout:
                end = "no data"
                break
            if first is not None and now - first >= seconds:
                break
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                continue
            except (ConnectionResetError, BrokenPipeError):
                end = "reset"
                break
            if not chunk:
                end = "eof"
                break
            now = time.time()
            if first is None:
                first = last = now
            max_gap = max(max_gap, now - last)
            last = now
            total += len(chunk)
            sec = int(now - first)
            per_second[sec] = per_second.get(sec, 0) + len(chunk)
            if len(sample) < 16384 and now - first > sample_after:
                sample += chunk[: 16384 - len(sample)]
        elapsed = (last - first) if first and last and last > first else 0.0
        expected = rate * 2 * elapsed
        return {
            "bytes": total, "elapsed": elapsed,
            "latency": (first - t0) if first else None,
            "ratio": (total / expected) if expected > 0 else 0.0,
            "mbps": (total / elapsed / 1e6) if elapsed > 0 else 0.0,
            "max_gap": max_gap, "end": end, "per_second": per_second,
            **sample_stats(sample),
        }

    def read_while(self, fn, timeout_after: float = 5.0) -> dict:
        """Run fn() in a thread (it sends commands) while draining the stream.

        The server drops clients that stop reading, so never sleep without reading."""
        done = threading.Event()
        errors: list = []

        def runner():
            try:
                fn()
            except Exception as e:   # noqa: BLE001
                errors.append(e)
            finally:
                done.set()

        threading.Thread(target=runner, daemon=True).start()
        total, max_gap, last, end = 0, 0.0, time.time(), "time"
        self.s.settimeout(0.5)
        while not done.is_set():
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                max_gap = max(max_gap, time.time() - last)
                continue
            except (ConnectionResetError, BrokenPipeError):
                end = "reset"
                break
            if not chunk:
                end = "eof"
                break
            now = time.time()
            max_gap = max(max_gap, now - last)
            last = now
            total += len(chunk)
        done.wait(timeout_after)
        if errors:
            raise errors[0]
        return {"bytes": total, "max_gap": max_gap, "end": end}

    def close(self, abrupt: bool = False) -> None:
        try:
            if abrupt:   # RST instead of FIN
                self.s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.s.close()
        except OSError:
            pass


def connect_retry(host: str, port: int, total: float = 40.0, rcvbuf: int | None = None):
    """Connect until a greeting arrives; the server serves one client at a time.

    Returns (client, seconds_waited)."""
    t0 = time.time()
    last = None
    while time.time() - t0 < total:
        try:
            return Client(host, port, greeting_timeout=3.0, rcvbuf=rcvbuf), time.time() - t0
        except (OSError, ClientError) as e:
            last = e
            time.sleep(0.5)
    raise ClientError("no greeting within %.0f s (%s)" % (total, last))
