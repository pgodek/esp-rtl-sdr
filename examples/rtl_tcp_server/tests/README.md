# rtl_tcp_server test suite

A black-box pytest suite that talks to the server like an SDR client would:
it reads the `RTL0` greeting, sends rtl_tcp commands and checks that the raw CU8 I/Q
stream keeps flowing at the expected rate. Standard library plus `pytest`.

Two modes:

| Mode | Command | Use |
|---|---|---|
| **Mock** (no hardware) | `./run_suite.sh` | in-process mock server; checks the suite itself and the protocol behaviour the tests rely on. Runs in CI (about 1.5 min). |
| **Board** | `./run_suite.sh 192.168.1.117` | real ESP32-P4 + dongle, about 10 minutes. Close SDR++ first: the server takes one client at a time. |

`run_suite.sh` creates `.venv` and installs `requirements.txt` on first use; everything after
the host is passed to pytest.

```bash
./run_suite.sh 192.168.1.117 --soak 900                 # 15 min soak
./run_suite.sh 192.168.1.117 -k "soak or rate"          # selection
./run_suite.sh 192.168.1.117 -m "not slow"              # skip the 20 s high-rate tests
./run_suite.sh 192.168.1.117 --serial=/dev/cu.usbserial-XXXX   # also read the board log
./run_suite.sh 192.168.1.117 --manual -s                # also unplug/replug the dongle
```

Options: `--host --port --rate --freq --gain-index --soak --serial --board-strict --manual --report-dir --stall-wait`.
`--gain-index=N` uses manual gain step N instead of auto gain (auto can saturate with strong signals).

> Give options that take a path or address in the `--option=value` form (`--serial=/dev/cu...`,
> `--report-dir=out`). With a separate value pytest may take it for a test path and fail with
> "unrecognized arguments".

## What is tested

| ID | Test | Checks |
|---|---|---|
| T0 | `test_00_protocol.py` | greeting (`RTL0`, tuner, gain steps); data without any command; a command split into single bytes; unknown/unsupported commands and `freq 0` are ignored; ppm |
| T1 | `test_10_stream.py` | soak: at least 95 % of the expected bytes, no gap over 1.5 s, no second below 70 %, data not constant |
| T2 | `test_20_session.py` | repeated graceful reconnects; reconnect after a TCP reset |
| T5 | `test_20_session.py` | a client that stops reading (socket left open) is dropped by the server, a new client is served; a second client gets no greeting while the first is streaming |
| T3 | `test_30_control.py` | sample-rate changes: stream resumes within 8 s and delivers the new rate |
| T4 | `test_30_control.py` | fast tuning sweep: connection and stream survive |
| T8 | `test_30_control.py` | all gain steps, auto gain, RTL AGC on/off while streaming |
| T6 | `test_30_control.py` | HF (10 MHz, upconverter path) and a few other frequencies; this does not judge reception quality |
| T7 | `test_40_rates.py` | 1.024, 1.92, 2.56 MS/s; 3.2 MS/s only has to keep the connection (drops are documented) |
| T9 | `test_90_manual.py` | (`--manual -s`) unplug and replug the dongle |

## Board log (`--serial`)

With `pip install pyserial` and `--serial PORT` the suite reads the board's log, adds the change of
`ring overrun` / `slot starve` / `usb err` and any driver error line (`ctrl record rejected`,
`FAULT`, `stream ended`, reboot, ...) to the report for each test, and with `--board-strict`
fails the test. Do not run `idf.py monitor` on the same port. The benign `EP 0 STALL` lines that
appear while the dongle is initialised are not counted as errors.

## Report

`reports/report_<time>.md` and `.json` (plus `board.log` with `--serial`): one row per test with
throughput, first-data latency, gaps, pause lengths and counter deltas. Paste the Markdown into an
issue or PR to document a run.

## Limits

- The mock imitates the server's behaviour, not the dongle; its pass says nothing about hardware.
- T5 simulates a client that stops reading. A cable pulled without a FIN is covered by the same
  server code path (send stall) plus TCP keepalive, which this suite cannot trigger on its own.
- The suite never enables the bias tee.

## Signal-integrity test without a dongle (T6)

`test_50_pattern.py` compiles the real `main/rtl_tcp_server.cpp` for the host (`host_server/`:
a thin FreeRTOS/`esp_log` shim and a fake `esp_rtl_sdr` driver) and feeds it a known signal instead of a
dongle. It reads the TCP stream and compares it **bit for bit** with what the driver produced, so a byte
the server drops, repeats or shifts is caught (a shift by one byte would swap I and Q). It checks the
first 12 MB, a new connection, a client that stops reading for 1.5 s, and the clean cut of a sample-rate
restart. It needs a C++ compiler (skipped without one) and runs in the normal CI step. It does not test
USB, the dongle, lwIP or real throughput.

The reference signal (`host_server/pattern.py`) paints **ALL YOUR BASE ARE BELONG TO US** into the waterfall
(rotated by 90 degrees, reading downwards; the waveform is a table of 180 columns, one spectrum each, sent in
reverse order because SDR++ draws the newest line on top; `pattern.py write FILE --forward` for a client that
scrolls the other way). A text pixel is
a 40 kHz wide block of noise that lasts 1 s at 2.048 MS/s (about 20 waterfall rows), so pixels are roughly
square and the whole loop takes about 3 minutes; zoom the waterfall in on the centre of the band.
To look at it in SDR++ or GQRX:

```bash
host_server/run_host.sh        # builds the server, serves the signal at real-time speed on :1234
```

then add an RTL-TCP source `127.0.0.1:1234` at 1.024-2.4 MS/s. `python3 host_server/pattern.py ascii`
prints the same picture as text.
