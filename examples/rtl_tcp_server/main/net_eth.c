#include "esp_check.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "sdkconfig.h"

#include "net_eth.h"

static const char *TAG = "net_eth";
static EventGroupHandle_t s_ev;
#define GOT_IP_BIT BIT0

static void on_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;

    ESP_LOGI(TAG, "got IP " IPSTR " - rtl_tcp port %d", IP2STR(&e->ip_info.ip),
             CONFIG_RTLTCP_PORT);
    xEventGroupSetBits(s_ev, GOT_IP_BIT);
}

static void on_eth(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (id == ETHERNET_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "link up");
    } else if (id == ETHERNET_EVENT_DISCONNECTED) {
        ESP_LOGW(TAG, "link down");
    }
}

esp_err_t net_eth_start(void)
{
    s_ev = xEventGroupCreate();
    ESP_RETURN_ON_FALSE(s_ev, ESP_ERR_NO_MEM, TAG, "event group");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif init");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");

    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_cfg);
    ESP_RETURN_ON_FALSE(netif, ESP_ERR_NO_MEM, TAG, "netif");

    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    eth_esp32_emac_config_t emac_cfg = ETH_ESP32_EMAC_DEFAULT_CONFIG();
    emac_cfg.smi_gpio.mdc_num = CONFIG_RTLTCP_ETH_MDC_GPIO;
    emac_cfg.smi_gpio.mdio_num = CONFIG_RTLTCP_ETH_MDIO_GPIO;
    /* RMII data pins and clock (external 50 MHz input, GPIO 50) are the
     * ESP32-P4 defaults of ETH_ESP32_EMAC_DEFAULT_CONFIG(). */
    esp_eth_mac_t *mac = esp_eth_mac_new_esp32(&emac_cfg, &mac_cfg);

    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.phy_addr = CONFIG_RTLTCP_ETH_PHY_ADDR;
    phy_cfg.reset_gpio_num = CONFIG_RTLTCP_ETH_PHY_RST_GPIO;
    esp_eth_phy_t *phy = esp_eth_phy_new_ip101(&phy_cfg);
    ESP_RETURN_ON_FALSE(mac && phy, ESP_FAIL, TAG, "mac/phy");

    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth = NULL;
    ESP_RETURN_ON_ERROR(esp_eth_driver_install(&eth_cfg, &eth), TAG, "eth install");
    ESP_RETURN_ON_ERROR(esp_netif_attach(netif, esp_eth_new_netif_glue(eth)), TAG, "attach");

    ESP_RETURN_ON_ERROR(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth, NULL),
                        TAG, "reg eth");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_ip, NULL),
                        TAG, "reg ip");
    ESP_RETURN_ON_ERROR(esp_eth_start(eth), TAG, "eth start");

    xEventGroupWaitBits(s_ev, GOT_IP_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    return ESP_OK;
}
