idf_component_register(
    SRCS "main.cpp" "rtl_tcp_server.cpp" "net_eth.c"
    INCLUDE_DIRS "."
    REQUIRES esp_rtl_sdr nvs_flash esp_netif esp_eth esp_event lwip esp_driver_gpio
)

# IDF 6.x defaults to a newer dialect; the example is written for C++17.
target_compile_options(${COMPONENT_LIB} PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:-std=gnu++17>")
