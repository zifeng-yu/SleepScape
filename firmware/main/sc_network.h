#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "sc_config.h"

/* Both timestamps are milliseconds. Callback runs on lwIP's TCP/IP task and
 * must only enqueue the anchor, never block or access the offline queue. */
typedef void (*sc_network_time_callback_t)(uint64_t monotonic_ms, int64_t utc_ms);
esp_err_t sc_network_init(const sc_config_t *config, sc_network_time_callback_t callback);
bool sc_network_ready(void);
/* Single uploader task only. Returns HTTP status or a negative error. No
 * redirect is followed; response is NUL-terminated and limited to 4096 bytes. */
int sc_network_post(const char *payload, char *response, size_t capacity);
/* Last response's integer Retry-After, capped at one day; zero means absent. */
uint32_t sc_network_retry_after_s(void);
