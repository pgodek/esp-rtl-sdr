#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Bring up the on-board Ethernet and block until an IPv4 address is assigned. */
esp_err_t net_eth_start(void);

#ifdef __cplusplus
}
#endif
