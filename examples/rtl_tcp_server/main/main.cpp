/*
 * rtl_tcp server for ESP32-P4 on top of the esp_rtl_sdr driver.
 *
 * Any rtl_tcp client (SDR++, GQRX, SDR#, rtl_sdr -f ... via rtl_tcp://) can
 * connect to <board-ip>:1234. Uses the public esp_rtl_sdr API only.
 */
#include "esp_log.h"
#include "esp_rtl_sdr.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "net_eth.h"
#include "rtl_tcp_server.h"

static const char *TAG = "rtl_tcp";

static void on_event(esp_rtl_sdr_event_t ev, const void *payload, void *ctx)
{
    (void)ctx;
    switch (ev) {
    case ESP_RTL_SDR_EVT_READY:
        ESP_LOGI(TAG, "dongle ready");
        break;
    case ESP_RTL_SDR_EVT_DISCONNECTED:
        ESP_LOGW(TAG, "dongle disconnected");
        break;
    case ESP_RTL_SDR_EVT_ERROR:
        if (payload != nullptr) {
            const auto *err = static_cast<const esp_rtl_sdr_error_info_t *>(payload);
            ESP_LOGE(TAG, "driver error %s: %s", esp_rtl_sdr_err_to_name(err->code), err->message);
        }
        break;
    default:
        break;
    }
}

extern "C" void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    ESP_ERROR_CHECK(net_eth_start());

    esp_rtl_sdr_config_t cfg;
    esp_rtl_sdr_config_default(&cfg);
    cfg.event_cb = on_event;
    /* Samples are pulled with esp_rtl_sdr_read(); no per-block event callback. */
    cfg.delivery_mode = ESP_RTL_SDR_DELIVERY_READ;
    cfg.pull_ring_bytes = CONFIG_RTLTCP_PULL_RING_BYTES;

    esp_rtl_sdr_handle_t sdr = nullptr;
    err = esp_rtl_sdr_install(&cfg, &sdr);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_rtl_sdr_install failed: %s", esp_rtl_sdr_err_to_name(err));
        if (err == ESP_RTL_SDR_ERR_USB_SAFE_MODE) {
            ESP_LOGE(TAG, "USB safe mode latched; see esp_rtl_sdr_usb_fault_guard_reset()");
        }
        return;
    }
    ESP_LOGI(TAG, "esp_rtl_sdr %s installed; waiting for a dongle", esp_rtl_sdr_get_version_string());

    rtl_tcp_server_run(sdr, CONFIG_RTLTCP_PORT);
}
