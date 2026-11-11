#include "sc_config.h"
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "driver/uart.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

static bool identifier(const char *s) {
    size_t length = strlen(s);
    if (length == 0 || length > 64) return false;
    for (size_t i = 0; i < length; ++i)
        if (!((s[i] >= 'a' && s[i] <= 'z') || (s[i] >= 'A' && s[i] <= 'Z') ||
              (s[i] >= '0' && s[i] <= '9') || s[i] == '-' || s[i] == '_' || s[i] == '.'))
            return false;
    return true;
}
static bool text_field(const cJSON *json, const char *name, char *out, size_t capacity) {
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(json, name);
    if (!cJSON_IsString(value) || !value->valuestring) return false;
    size_t length = strlen(value->valuestring);
    if (length >= capacity) return false;
    memcpy(out, value->valuestring, length + 1);
    return true;
}
static bool valid_config(const sc_config_t *config) {
    const char *path = "/api/v1/ingest/environment-batches";
    size_t url_length = strlen(config->server_url), path_length = strlen(path);
    if (!identifier(config->device_id) || !identifier(config->profile_id) ||
        !config->ssid[0] || strlen(config->token) < 43 ||
        strncmp(config->server_url, "https://", 8) ||
        url_length <= 8 + path_length ||
        strcmp(config->server_url + url_length - path_length, path)) return false;
    for (const char *p = config->server_url; *p; ++p)
        if ((unsigned char)*p <= 32 || *p == '@' || *p == '?' || *p == '#') return false;
    for (const char *p = config->token; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '-' || *p == '_')) return false;
    size_t password_length = strlen(config->password);
    return password_length == 0 || (password_length >= 8 && password_length <= 63);
}
esp_err_t sc_config_init(void) {
    /* Never erase credentials automatically when NVS has a version/error problem. */
    esp_err_t result = nvs_flash_init();
    if (result != ESP_OK) return result;
    uart_config_t uart = {.baud_rate = 115200, .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE, .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT};
    result = uart_param_config(UART_NUM_0, &uart);
    if (result != ESP_OK) return result;
    return uart_driver_install(UART_NUM_0, 2048, 0, 0, NULL, 0);
}
bool sc_config_load(sc_config_t *config) {
    nvs_handle_t handle;
    memset(config, 0, sizeof(*config));
    if (nvs_open("sleepscape", NVS_READONLY, &handle) != ESP_OK) return false;
    size_t length = sizeof(*config);
    esp_err_t result = nvs_get_blob(handle, "config", config, &length);
    nvs_close(handle);
    /* Require terminators before strlen; corrupted NVS is not trusted input. */
    if (result != ESP_OK || length != sizeof(*config) ||
        !memchr(config->device_id, 0, sizeof(config->device_id)) ||
        !memchr(config->profile_id, 0, sizeof(config->profile_id)) ||
        !memchr(config->server_url, 0, sizeof(config->server_url)) ||
        !memchr(config->token, 0, sizeof(config->token)) ||
        !memchr(config->ssid, 0, sizeof(config->ssid)) ||
        !memchr(config->password, 0, sizeof(config->password))) {
        memset(config, 0, sizeof(*config)); return false;
    }
    return valid_config(config);
}
static void process_line(const char *line) {
    cJSON *json = cJSON_Parse(line);
    const cJSON *command = cJSON_GetObjectItemCaseSensitive(json, "command");
    if (!cJSON_IsObject(json) || !cJSON_IsString(command)) {
        puts("{\"status\":\"error\",\"code\":\"invalid_json\"}");
        cJSON_Delete(json); return;
    }
    if (!strcmp(command->valuestring, "status")) {
        sc_config_t config;
        bool configured = sc_config_load(&config);
        memset(&config, 0, sizeof(config));
        puts(configured ? "{\"status\":\"ready\",\"configured\":true}" :
                          "{\"status\":\"ready\",\"configured\":false}");
        cJSON_Delete(json); return;
    }
    sc_config_t config = {0};
    bool valid = !strcmp(command->valuestring, "configure") &&
        text_field(json, "device_id", config.device_id, sizeof(config.device_id)) &&
        text_field(json, "profile_id", config.profile_id, sizeof(config.profile_id)) &&
        text_field(json, "server_url", config.server_url, sizeof(config.server_url)) &&
        text_field(json, "token", config.token, sizeof(config.token)) &&
        text_field(json, "ssid", config.ssid, sizeof(config.ssid)) &&
        text_field(json, "password", config.password, sizeof(config.password)) && valid_config(&config);
    /* Configuration strings are never echoed, including parse/validation failures. */
    cJSON_Delete(json);
    if (!valid) {
        memset(&config, 0, sizeof(config));
        puts("{\"status\":\"error\",\"code\":\"invalid_configuration\"}"); return;
    }
    nvs_handle_t handle;
    esp_err_t result = nvs_open("sleepscape", NVS_READWRITE, &handle);
    if (result == ESP_OK) {
        result = nvs_set_blob(handle, "config", &config, sizeof(config));
        if (result == ESP_OK) result = nvs_commit(handle);
        nvs_close(handle);
    }
    memset(&config, 0, sizeof(config));
    if (result != ESP_OK) {
        puts("{\"status\":\"error\",\"code\":\"configuration_write_failed\"}"); return;
    }
    puts("{\"status\":\"configured\"}"); fflush(stdout);
    uart_wait_tx_done(UART_NUM_0, pdMS_TO_TICKS(1000));
    vTaskDelay(pdMS_TO_TICKS(100));
    esp_restart();
}
void sc_config_uart_task(void *unused) {
    (void)unused;
    char line[2048]; size_t used = 0; bool overflow = false;
    for (;;) {
        uint8_t byte;
        if (uart_read_bytes(UART_NUM_0, &byte, 1, pdMS_TO_TICKS(100)) != 1) continue;
        if (byte == '\n') {
            line[used] = 0;
            if (overflow) puts("{\"status\":\"error\",\"code\":\"configuration_too_large\"}");
            else if (used) process_line(line);
            memset(line, 0, sizeof(line)); used = 0; overflow = false;
        } else if (byte != '\r') {
            if (used + 1 < sizeof(line)) line[used++] = (char)byte;
            else overflow = true;
        }
    }
}
