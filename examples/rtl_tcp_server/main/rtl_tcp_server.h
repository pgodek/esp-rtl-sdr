#pragma once

#include "esp_rtl_sdr.h"

/**
 * Serve the rtl_tcp protocol for `sdr` on `port`, one client at a time.
 * Never returns. The dongle may be absent or replugged at any time; a client
 * that connects while no dongle is attached is dropped.
 */
void rtl_tcp_server_run(esp_rtl_sdr_handle_t sdr, int port);
