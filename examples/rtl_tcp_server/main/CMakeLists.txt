idf_component_register(
    SRCS "main.cpp" "rtl_tcp_server.cpp" "net_eth.c"
    INCLUDE_DIRS "."
    REQUIRES esp_rtl_sdr nvs_flash esp_netif esp_eth esp_event lwip esp_driver_gpio
)
