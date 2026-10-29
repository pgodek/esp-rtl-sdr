# rtl_tcp_server

An **rtl_tcp**-compatible network server for ESP32-P4 built on the `esp_rtl_sdr`
public API (no librtlsdr code). Plug an RTL-SDR into the board, connect SDR++, GQRX or
SDR# with *RTL-TCP* source to `<board-ip>:1234`.

**Status:** hardware-verified on one Waveshare ESP32-P4-Module-DEV-KIT (chip v3.2) with one
Blog V4, ESP-IDF 5.5.6, macOS client (2026-10-29): the suite in `tests/` ran against the board.
The wire-protocol layer is also host-tested (`tests/host/test_rtl_tcp_proto.cpp`). Not covered:
unplugging the dongle while streaming (`tests/test_90_manual.py`, manual), other boards and dongles.

## Hardware: Waveshare ESP32-P4-Module-DEV-KIT

| Item | Setting |
|---|---|
| USB | Jumper on **HOST**, dongle in Type-A **port 1** (direct to the P4). With the DEVICE jumper use ports 2-4 behind the CH334 (`CONFIG_USB_HOST_HUBS_SUPPORTED=y` is set). See `docs/WAVESHARE_P4_MULTI_RTL_PROTOTYPE.md`. |
| Network | RJ45 Ethernet (internal EMAC + IP101, RMII). |
| Power | Keep the bias tee **off** unless your accessory needs it; check the port can supply the dongle's current. |

**Ethernet:** the defaults (MDC 31, MDIO 52, PHY reset 51, PHY address 1) are the values of
ESP-IDF's ethernet example for the ESP32-P4 Function-EV-Board and brought the link and a DHCP
address up on the Waveshare Module-DEV-KIT. On ESP32-P4 ESP-IDF 5.5 fixes the RMII data pins
(TXD0/1 34/35, TX_EN 49, RXD0/1 29/30, CRS_DV 28) and expects the 50 MHz RMII clock as an
**input on GPIO 50** (`ETH_ESP32_EMAC_DEFAULT_CONFIG()`); the example does not change that.
For another board adjust MDC/MDIO/reset/address under `menuconfig -> rtl_tcp_server -> Ethernet`;
a different clock source needs a code change in `main/net_eth.c` (`emac_cfg.clock_config`).

## Build (ESP-IDF 5.5.x)

```bash
cd examples/rtl_tcp_server
idf.py set-target esp32p4
idf.py build flash monitor
```

The log prints the IP address (`got IP ... - rtl_tcp port 1234`). Then in the client choose
*RTL-TCP* and enter `IP:1234`.

## How it maps onto the driver

| rtl_tcp command | Behaviour |
|---|---|
| `0x01` frequency | `esp_rtl_sdr_set_center_freq` (HF below 28.8 MHz goes through the V4 upconverter automatically) |
| `0x02` sample rate | must pass `esp_rtl_sdr_is_rate_supported`; applied with **stop/start** (driver requirement), so expect a short gap |
| `0x03` / `0x04` / `0x0d` gain mode / gain / gain by index | `set_tuner_gain_mode`, `set_tuner_gain` (nominal ladder) |
| `0x05` ppm | `set_freq_correction` (-200..200), then re-tune |
| `0x08` AGC | RTL2832 digital AGC (`set_rtl_agc`) |
| `0x0e` bias tee | `set_bias_tee` (logged as a warning) |
| `0x06` IF gain, `0x07` test mode, `0x09` direct sampling, `0x0a` offset tuning, `0x0b`/`0x0c` xtal | no driver counterpart: logged and ignored |

Tuner settings are kept across clients and re-applied after every start, because the
driver accepts gain/AGC/bias only on a started stream. The dongle only streams while a
client is connected; a second client is accepted only after the first disconnects.

Data path: `esp_rtl_sdr_read()` (pull ring, `DELIVERY_READ`) -> sender task -> TCP. A
status line every 5 s shows TCP MB/s, effective USB sample rate and the driver's
`ring_overrun` / `slot_starve` / USB error counters, so you can tell USB starvation from
a slow network.

## Tests

`tests/` holds a pytest suite that runs against a mock server (CI) or a real board
(`tests/run_suite.sh <board-ip>`); see `tests/README.md`.

## Gain

On the Waveshare ESP32-P4-Module-DEV-KIT with a Blog V4 (one unit, strong FM at the antenna) the
driver's AUTO gain behaved like the maximum gain: 55 % of the samples were clipped at 100 MHz, the same
as manual step 27. With manual steps the level rose smoothly (std 3 at step 0, 32 at step 12, 67 at
step 15, clipping from step 18). If your spectrum looks flat or distorted, switch to manual gain
(SDR++: untick AGC / gain slider). `tests/` runs with `--gain-index=12` to avoid the clipping.

## Known limits

- One client at a time; IPv4 only; Ethernet only (no Wi-Fi path yet).
- 2.048 MS/s is about 33 Mbit/s of TCP payload; use wired Ethernet. Use the rate passport
  (`esp_rtl_sdr_probe_rates`) or the status line to find what your setup sustains.
- Pull ring defaults to 256 KiB of internal RAM (`CONFIG_RTLTCP_PULL_RING_BYTES`);
  enable PSRAM and raise it if you see ring overruns on a bursty network.
