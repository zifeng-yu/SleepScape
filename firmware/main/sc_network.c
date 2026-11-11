#include "sc_network.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/time.h>
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/timers.h"

#define WIFI_CONNECTED BIT0
#define TIME_SYNCHRONIZED BIT1
#define RESPONSE_LIMIT 4096U
#define REQUEST_LIMIT 65536U

static EventGroupHandle_t network_events;
static TimerHandle_t reconnect_timer;
static portMUX_TYPE retry_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t reconnect_seconds = 1;
static uint32_t last_retry_after;
static sc_network_time_callback_t time_callback;
static char endpoint[256];
static char authorization[136];

static void schedule_reconnect(void) {
    uint32_t delay;
    portENTER_CRITICAL(&retry_lock);
    delay = reconnect_seconds;
    reconnect_seconds = reconnect_seconds < 150 ? reconnect_seconds * 2 : 300;
    portEXIT_CRITICAL(&retry_lock);
    /* Timer commands never wait inside the timer service task. A single timer
     * bounds retries to 1, 2, 4 ... 300 seconds instead of a tight reconnect loop. */
    xTimerChangePeriod(reconnect_timer, pdMS_TO_TICKS(delay * 1000), 0);
}

static void reconnect(TimerHandle_t timer) {
    (void)timer;
    if (xEventGroupGetBits(network_events) & WIFI_CONNECTED) return;
    if (esp_wifi_connect() != ESP_OK) schedule_reconnect();
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg; (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (esp_wifi_connect() != ESP_OK) schedule_reconnect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network_events, WIFI_CONNECTED);
        schedule_reconnect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xTimerStop(reconnect_timer, 0);
        portENTER_CRITICAL(&retry_lock);
        reconnect_seconds = 1;
        portEXIT_CRITICAL(&retry_lock);
        xEventGroupSetBits(network_events, WIFI_CONNECTED);
        /* Start/restart only after DNS and the STA interface are usable. */
        esp_netif_sntp_start();
    }
}

static void synchronized(struct timeval *value) {
    if (!value || value->tv_sec < 1704067200LL) return; /* reject pre-2024 time */
    uint64_t mono = (uint64_t)esp_timer_get_time() / 1000;
    int64_t utc = (int64_t)value->tv_sec * 1000 + value->tv_usec / 1000;
    if (time_callback) time_callback(mono, utc);
    xEventGroupSetBits(network_events, TIME_SYNCHRONIZED);
}

esp_err_t sc_network_init(const sc_config_t *config, sc_network_time_callback_t callback) {
    if (!config || !callback || network_events) return ESP_ERR_INVALID_STATE;
    if (strnlen(config->server_url, sizeof(config->server_url)) >= sizeof(endpoint) ||
        strnlen(config->token, sizeof(config->token)) >= sizeof(config->token) ||
        strncmp(config->server_url, "https://", 8)) return ESP_ERR_INVALID_ARG;
    memcpy(endpoint, config->server_url, strlen(config->server_url) + 1);
    int length = snprintf(authorization, sizeof(authorization), "Bearer %s", config->token);
    if (length < 0 || (size_t)length >= sizeof(authorization)) return ESP_ERR_INVALID_ARG;
    time_callback = callback;
    network_events = xEventGroupCreate();
    if (!network_events) return ESP_ERR_NO_MEM;
    reconnect_timer = xTimerCreate("sc-wifi", pdMS_TO_TICKS(1000), pdFALSE, NULL, reconnect);
    if (!reconnect_timer) return ESP_ERR_NO_MEM;

    /* Initialization is called once at boot. A failure is returned to app_main;
     * it must not retry initialization over partially initialized SDK services. */
    esp_err_t result = esp_netif_init();
    if (result != ESP_OK) return result;
    result = esp_event_loop_create_default();
    if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) return result;
    if (!esp_netif_create_default_wifi_sta()) return ESP_ERR_NO_MEM;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    result = esp_wifi_init(&init);
    if (result != ESP_OK) return result;
    result = esp_wifi_set_storage(WIFI_STORAGE_RAM);
    if (result != ESP_OK) return result;
    result = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL);
    if (result != ESP_OK) return result;
    result = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL);
    if (result != ESP_OK) return result;

    esp_sntp_config_t ntp = ESP_NETIF_SNTP_DEFAULT_CONFIG_MULTIPLE(
        3, ESP_SNTP_SERVER_LIST("ntp.aliyun.com", "ntp.tencent.com", "pool.ntp.org"));
    ntp.start = false;
    ntp.wait_for_sync = false;
    ntp.sync_cb = synchronized;
    result = esp_netif_sntp_init(&ntp);
    if (result != ESP_OK) return result;

    wifi_config_t wifi = {0};
    size_t ssid_length = strnlen(config->ssid, sizeof(config->ssid));
    size_t password_length = strnlen(config->password, sizeof(config->password));
    if (!ssid_length || ssid_length > sizeof(wifi.sta.ssid) ||
        password_length >= sizeof(wifi.sta.password)) return ESP_ERR_INVALID_ARG;
    memcpy(wifi.sta.ssid, config->ssid, ssid_length);
    memcpy(wifi.sta.password, config->password, password_length);
    wifi.sta.threshold.authmode = password_length ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wifi.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
    wifi.sta.pmf_cfg.capable = true;
    wifi.sta.pmf_cfg.required = false;
    result = esp_wifi_set_mode(WIFI_MODE_STA);
    if (result == ESP_OK) result = esp_wifi_set_config(WIFI_IF_STA, &wifi);
    /* The SDK keeps the connection configuration only in RAM, while sc_config
     * owns the single intentional NVS copy. Never print credentials or SSID. */
    volatile unsigned char *clear = (volatile unsigned char *)&wifi;
    for (size_t i = 0; i < sizeof(wifi); ++i) clear[i] = 0;
    if (result != ESP_OK) return result;
    return esp_wifi_start();
}

bool sc_network_ready(void) {
    if (!network_events) return false;
    EventBits_t required = WIFI_CONNECTED | TIME_SYNCHRONIZED;
    return (xEventGroupGetBits(network_events) & required) == required;
}

static esp_err_t http_event(esp_http_client_event_t *event) {
    if (event->event_id == HTTP_EVENT_ON_HEADER && event->header_key && event->header_value &&
        !strcasecmp(event->header_key, "Retry-After")) {
        const char *value = event->header_value;
        uint32_t seconds = 0;
        if (!*value) return ESP_OK;
        for (; *value; ++value) {
            if (*value < '0' || *value > '9') return ESP_OK; /* dates use normal backoff */
            seconds = seconds < 86400 ? seconds * 10 + (uint32_t)(*value - '0') : 86400;
        }
        last_retry_after = seconds > 86400 ? 86400 : seconds;
    }
    return ESP_OK;
}

static int negative_error(esp_err_t error) {
    return error > 0 ? -error : -1;
}

static bool remaining_timeout(esp_http_client_handle_t client, int64_t deadline) {
    int64_t left = (deadline - esp_timer_get_time()) / 1000;
    if (left <= 0) return false;
    return esp_http_client_set_timeout_ms(client, (int)left) == ESP_OK;
}

int sc_network_post(const char *payload, char *response, size_t capacity) {
    last_retry_after = 0;
    if (!payload || !response || capacity < 2) return -ESP_ERR_INVALID_ARG;
    response[0] = 0;
    if (!sc_network_ready()) return -ESP_ERR_INVALID_STATE;
    size_t request_length = strnlen(payload, REQUEST_LIMIT + 1);
    if (!request_length || request_length > REQUEST_LIMIT) return -ESP_ERR_INVALID_SIZE;
    size_t limit = capacity - 1;
    if (limit > RESPONSE_LIMIT) limit = RESPONSE_LIMIT;
    esp_http_client_config_t options = {
        .url = endpoint,
        .method = HTTP_METHOD_POST,
        .transport_type = HTTP_TRANSPORT_OVER_SSL,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .skip_cert_common_name_check = false,
        .disable_auto_redirect = true,
        .max_authorization_retries = -1,
        .timeout_ms = 30000,
        .buffer_size = 1024,
        .buffer_size_tx = 1024,
        .keep_alive_enable = false,
        .event_handler = http_event,
    };
    esp_http_client_handle_t client = esp_http_client_init(&options);
    if (!client) return -ESP_ERR_NO_MEM;
    int status = -ESP_ERR_INVALID_STATE;
    esp_err_t error = esp_http_client_set_header(client, "Authorization", authorization);
    if (error == ESP_OK) error = esp_http_client_set_header(client, "Content-Type", "application/json");
    if (error == ESP_OK) error = esp_http_client_set_header(client, "Accept", "application/json");
    if (error == ESP_OK) error = esp_http_client_set_header(client, "Connection", "close");
    if (error != ESP_OK) { status = negative_error(error); goto done; }
    int64_t deadline = esp_timer_get_time() + 30000000;
    error = esp_http_client_open(client, (int)request_length);
    if (error != ESP_OK) { status = negative_error(error); goto done; }
    size_t sent = 0;
    while (sent < request_length) {
        if (!remaining_timeout(client, deadline)) { status = -ESP_ERR_TIMEOUT; goto done; }
        int count = esp_http_client_write(client, payload + sent, (int)(request_length - sent));
        if (count <= 0) { status = -ESP_ERR_HTTP_WRITE_DATA; goto done; }
        sent += (size_t)count;
    }
    if (!remaining_timeout(client, deadline)) { status = -ESP_ERR_TIMEOUT; goto done; }
    int64_t length = esp_http_client_fetch_headers(client);
    if (length < 0) { status = -ESP_ERR_HTTP_FETCH_HEADER; goto done; }
    if (length > (int64_t)limit) { status = -ESP_ERR_INVALID_SIZE; goto done; }
    status = esp_http_client_get_status_code(client);
    if (status < 100 || status > 599) { status = -ESP_ERR_INVALID_RESPONSE; goto done; }
    size_t received = 0;
    for (;;) {
        if (!remaining_timeout(client, deadline)) { status = -ESP_ERR_TIMEOUT; goto done; }
        char chunk[512];
        /* One extra byte detects overflow even with chunked/unknown length. */
        size_t available = limit - received + 1;
        if (available > sizeof(chunk)) available = sizeof(chunk);
        int count = esp_http_client_read(client, chunk, (int)available);
        if (count < 0) { status = -ESP_ERR_HTTP_EAGAIN; goto done; }
        if (count == 0) {
            if (!esp_http_client_is_complete_data_received(client)) status = -ESP_ERR_INVALID_RESPONSE;
            break;
        }
        if ((size_t)count > limit - received) { status = -ESP_ERR_INVALID_SIZE; goto done; }
        memcpy(response + received, chunk, (size_t)count);
        received += (size_t)count;
        response[received] = 0;
    }
done:
    /* No response, request body, Authorization header, or TLS peer content is
     * ever logged. Clearing failures prevents a caller from accepting a prefix. */
    if (status < 0) response[0] = 0;
    esp_http_client_cleanup(client);
    return status;
}

uint32_t sc_network_retry_after_s(void) {
    return last_retry_after;
}
