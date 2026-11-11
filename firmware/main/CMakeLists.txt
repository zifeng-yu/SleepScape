idf_component_register(SRCS "app_main.c" "sc_board.c" "sc_config.c" "sc_network.c" "sc_wire.c" "sc_reject.c"
    INCLUDE_DIRS "."
    REQUIRES sc_core esp_driver_gpio esp_driver_i2c esp_driver_i2s esp_driver_rmt esp_driver_uart
             esp_wifi esp_event esp_netif esp_timer esp_http_client esp-tls
             nvs_flash spiffs json mbedtls)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
