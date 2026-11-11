#ifndef SC_CONFIG_H
#define SC_CONFIG_H
#include <stdbool.h>
#include "esp_err.h"
typedef struct {
    char device_id[65], profile_id[65], server_url[256];
    char token[129], ssid[33], password[65];
} sc_config_t;
esp_err_t sc_config_init(void);
bool sc_config_load(sc_config_t *config);
void sc_config_uart_task(void *unused);
#endif
