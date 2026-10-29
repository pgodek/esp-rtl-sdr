"""In-process mock of the ESP32 rtl_tcp server, so the suite runs without hardware.

It imitates the behaviour the tests rely on: one client at a time, greeting,
stream starts after the client's command burst, a sample-rate change pauses the
stream, and a client that stops reading is dropped after `stall_timeout`.
It does not model the dongle; the data is noise."""
from __future__ import annotations

import random
import socket
import struct
import threading
import time


class MockServer:
    def __init__(self, tuner: int = 6, gain_count: int = 28, start_delay: float = 0.2,
                 rate_pause: float = 0.8, stall_timeout: float = 2.0):
        self.tuner, self.gain_count = tuner, gain_count
        self.start_delay, self.rate_pause, self.stall_timeout = start_delay, rate_pause, stall_timeout
        self.noise = bytes(random.choices(range(118, 138), k=16384))
        self.commands: list = []
        self.clients = 0
        self._ls = socket.socket()
        self._ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ls.bind(("127.0.0.1", 0))
        self._ls.listen(1)
        self._ls.settimeout(0.2)
        self.port = self._ls.getsockname()[1]
        self._run = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._run = False
        self._thread.join(3)
        self._ls.close()

    def _accept_loop(self):
        while self._run:
            try:
                conn, _ = self._ls.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.clients += 1
            self._serve(conn)

    def _serve(self, conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)   # make back-pressure visible
        st = {"rate": 2048000, "last_cmd": time.time(), "pause_until": 0.0, "alive": True}
        try:
            conn.sendall(b"RTL0" + struct.pack(">II", self.tuner, self.gain_count))
        except OSError:
            conn.close()
            return

        def reader():
            buf = b""
            while st["alive"] and self._run:
                try:
                    data = conn.recv(256)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while len(buf) >= 5:
                    c, p = struct.unpack(">BI", buf[:5])
                    buf = buf[5:]
                    st["last_cmd"] = time.time()
                    self.commands.append((c, p))
                    if c == 2 and p != st["rate"]:
                        st["rate"] = p
                        st["pause_until"] = time.time() + self.rate_pause
            st["alive"] = False

        threading.Thread(target=reader, daemon=True).start()
        t_conn = time.time()
        while st["alive"] and time.time() - st["last_cmd"] < 0.3 and time.time() - t_conn < 2.0:
            time.sleep(0.05)
        time.sleep(self.start_delay)                      # "init" of the dongle
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                        struct.pack("ll", int(self.stall_timeout), 0))
        nxt = time.time()
        while st["alive"] and self._run:
            if time.time() < st["pause_until"]:
                time.sleep(0.05)
                nxt = time.time()
                continue
            try:
                conn.sendall(self.noise)
            except OSError:
                break                                     # stalled or reset: drop the client
            nxt += len(self.noise) / (st["rate"] * 2)
            wait = nxt - time.time()
            if wait > 0:
                time.sleep(wait)
            elif wait < -1:
                nxt = time.time()
        st["alive"] = False
        try:
            conn.close()
        except OSError:
            pass
