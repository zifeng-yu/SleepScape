#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "esp_random.h"
#include "esp_spiffs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sc_aggregate.h"
#include "sc_board.h"
#include "sc_config.h"
#include "sc_network.h"
#include "sc_queue.h"
#include "sc_reject.h"
#include "sc_wire.h"

/* PCM exists only in the DMA buffers and audio task stack. Never queue PCM. */
typedef enum { MSG_SOUND, MSG_SENSOR, MSG_ANCHOR } message_type_t;
typedef struct {
    message_type_t type;
    uint64_t slot_ms;
    union {
        sc_audio_second_t sound;
        struct { bool light_ok, aht_attempt, aht_ok; float lux, temperature, humidity; } sensor;
        int64_t utc_ms;
    } value;
} observation_t;
static QueueHandle_t observations;
static SemaphoreHandle_t journal_lock;
static sc_config_t config;
static sc_queue_t journal;
static sc_aggregate_t aggregate;
static uint64_t anchor_sequence;
static bool storage_ok;
static atomic_uint observation_drops;
static uint32_t quarantined_records;
static bool quarantine_healthy = true;
static uint64_t monotonic_ms(void) { return (uint64_t)esp_timer_get_time() / 1000; }

static void fail(const char *code) {
    /* Codes only: no credentials, payloads or audio. USB provisioning stays alive. */
    printf("{\"status\":\"error\",\"code\":\"%s\"}\n", code);
}
static bool mount_storage(void) {
    esp_vfs_spiffs_conf_t fs = {.base_path = "/spiffs", .partition_label = "storage",
        .max_files = 5, .format_if_mount_failed = false};
    if (esp_vfs_spiffs_register(&fs) == ESP_OK) return true;
    /* Format only a completely erased, never-used partition, not a damaged queue. */
    const esp_partition_t *partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "storage");
    if (!partition) return false;
    uint8_t block[256];
    for (size_t offset = 0; offset < partition->size; offset += sizeof(block)) {
        if (esp_partition_read(partition, offset, block, sizeof(block)) != ESP_OK) return false;
        for (size_t i = 0; i < sizeof(block); ++i) if (block[i] != 0xff) return false;
    }
    fs.format_if_mount_failed = true;
    return esp_vfs_spiffs_register(&fs) == ESP_OK;
}
static void boot_uuid(char out[37]) {
    uint8_t b[16]; esp_fill_random(b, sizeof(b));
    b[6] = (b[6] & 15) | 0x40; b[8] = (b[8] & 63) | 0x80;
    snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        b[0],b[1],b[2],b[3],b[4],b[5],b[6],b[7],b[8],b[9],b[10],b[11],b[12],b[13],b[14],b[15]);
}
static bool append_record(sc_record_type_t type, const uint8_t *bytes, size_t length) {
    bool ok = storage_ok && length && sc_queue_append(&journal, type, bytes, length, NULL);
    if (!ok) fail("journal_write_failed");
    return ok;
}
static void emit_minute(void *unused, const sc_minute_t *minute) {
    (void)unused;
    uint8_t bytes[SC_RECORD_MAX];
    xSemaphoreTake(journal_lock, portMAX_DELAY);
    append_record(SC_RECORD_MINUTE, bytes, sc_minute_encode(minute, bytes, sizeof(bytes)));
    if (minute->events.has_event)
        append_record(SC_RECORD_EVENT, bytes,
            sc_event_encode(minute->seq, &minute->events, bytes, sizeof(bytes)));
    if (minute->seq % 5 == 0) {
        sc_status_t status = {.seq = minute->seq / 5, .monotonic_ms = monotonic_ms(),
            .dropped_minutes = journal.dropped[0], .dropped_events = journal.dropped[1],
            .dropped_anchors = journal.dropped[2], .queued_records = journal.queued_records,
            .corrupt_records = journal.corrupt_records, .quarantined_records = quarantined_records};
        append_record(SC_RECORD_STATUS, bytes, sc_status_encode(&status, bytes, sizeof(bytes)));
        printf("{\"status\":\"collecting\",\"queued_records\":%"PRIu32
               ",\"heap_min_bytes\":%u,\"audio_dma_overflows\":%"PRIu32
               ",\"observation_drops\":%"PRIu32"}\n", journal.queued_records,
            (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
            sc_board_audio_overflows(), (uint32_t)atomic_load(&observation_drops));
    }
    xSemaphoreGive(journal_lock);
}
static void time_synced(uint64_t mono, int64_t utc) {
    observation_t message = {.type = MSG_ANCHOR, .slot_ms = mono, .value.utc_ms = utc};
    if (xQueueSend(observations, &message, 0) != pdTRUE) ++observation_drops;
}
static void collect_task(void *unused) {
    (void)unused;
    observation_t message;
    for (;;) {
        if (xQueueReceive(observations, &message, portMAX_DELAY) != pdTRUE) continue;
        if (message.type == MSG_ANCHOR) {
            uint8_t bytes[SC_ANCHOR_BYTES];
            sc_anchor_t anchor = {.seq = ++anchor_sequence, .monotonic_ms = message.slot_ms,
                .utc_ms = message.value.utc_ms, .uncertainty_ms = UINT32_MAX};
            size_t length = sc_anchor_encode(&anchor, bytes, sizeof(bytes));
            xSemaphoreTake(journal_lock, portMAX_DELAY);
            append_record(SC_RECORD_ANCHOR, bytes, length);
            xSemaphoreGive(journal_lock);
        } else if (message.type == MSG_SOUND) {
            sc_aggregate_audio(&aggregate, message.slot_ms, &message.value.sound);
        } else {
            sc_aggregate_sensor(&aggregate, message.slot_ms,
                message.value.sensor.light_ok, message.value.sensor.lux,
                message.value.sensor.aht_attempt, message.value.sensor.aht_ok,
                message.value.sensor.temperature, message.value.sensor.humidity);
        }
    }
}
static void sensor_task(void *unused) {
    (void)unused;
    uint64_t previous = UINT64_MAX;
    for (;;) {
        uint64_t slot = monotonic_ms() / 1000 * 1000;
        if (slot == previous) { vTaskDelay(pdMS_TO_TICKS(20)); continue; }
        previous = slot;
        observation_t message = {.type = MSG_SENSOR, .slot_ms = slot};
        message.value.sensor.light_ok = sc_board_light(&message.value.sensor.lux);
        message.value.sensor.aht_attempt = slot % 10000 == 0;
        if (message.value.sensor.aht_attempt)
            message.value.sensor.aht_ok = sc_board_temperature(
                &message.value.sensor.temperature, &message.value.sensor.humidity);
        if (xQueueSend(observations, &message, 0) != pdTRUE) ++observation_drops;
    }
}
static void send_sound(uint64_t slot, sc_pcm_t *pcm) {
    observation_t message = {.type = MSG_SOUND, .slot_ms = slot,
        .value.sound = sc_pcm_finish(pcm)};
    if (xQueueSend(observations, &message, 0) != pdTRUE) ++observation_drops;
}
static void audio_task(void *unused) {
    (void)unused;
    int16_t samples[256];
    sc_pcm_t pcm; sc_pcm_reset(&pcm);
    bool aligning = true;
    uint64_t slot = (monotonic_ms() / 1000 + 1) * 1000;
    uint32_t overflow_before = sc_board_audio_overflows();
    for (;;) {
        size_t count = 0;
        esp_err_t result = sc_board_audio_read(samples, 256, &count);
        uint64_t now = monotonic_ms();
        uint32_t overflow_after = sc_board_audio_overflows();
        if (aligning) {
            /* Drain old DMA samples to the next monotonic boundary. At most one
             * 16ms DMA block is discarded; the first slot is marked partial. */
            if (now >= slot) { aligning = false; sc_pcm_reset(&pcm); sc_pcm_gap(&pcm, 0); }
            overflow_before = overflow_after;
            memset(samples, 0, sizeof(samples));
            if (!count) vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (result != ESP_OK || overflow_after != overflow_before)
            sc_pcm_gap(&pcm, overflow_after - overflow_before);
        overflow_before = overflow_after;
        size_t offset = 0;
        while (offset < count) {
            size_t take = SC_SAMPLE_RATE - pcm.observed_samples;
            if (take > count - offset) take = count - offset;
            sc_pcm_add(&pcm, samples + offset, take); offset += take;
            if (pcm.observed_samples == SC_SAMPLE_RATE) {
                bool late = now > slot + 1250 || now + 250 < slot + 1000;
                if (late) sc_pcm_gap(&pcm, 0);
                send_sound(slot, &pcm); sc_pcm_reset(&pcm); slot += 1000;
                if (late) {
                    slot = (now / 1000 + 1) * 1000;
                    aligning = true; break;
                }
            }
        }
        memset(samples, 0, sizeof(samples));
        if (!count && now >= slot + 1200) {
            sc_pcm_gap(&pcm, 0); send_sound(slot, &pcm); sc_pcm_reset(&pcm);
            slot = (now / 1000 + 1) * 1000; aligning = true;
        }
        if (!count) vTaskDelay(pdMS_TO_TICKS(10));
    }
}
static void upload_task(void *unused) {
    (void)unused;
    static sc_queue_item_t batch[12];
    static char response[8192];
    uint32_t backoff = 5;
    bool isolate = false;
    for (;;) {
        if (!sc_network_ready()) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
        size_t count = 0;
        xSemaphoreTake(journal_lock, portMAX_DELAY);
        uint64_t cursor = 0;
        while (count < (isolate ? 1u : 12u) && sc_queue_peek(&journal, cursor, &batch[count])) {
            if (strcmp(batch[count].source.device_id, config.device_id)) {
                fail("queued_device_identity_mismatch"); break;
            }
            if (count && (strcmp(batch[count].source.boot_id, batch[0].source.boot_id) ||
                strcmp(batch[count].source.measurement_profile_id,
                       batch[0].source.measurement_profile_id))) break;
            cursor = batch[count].queue_id;
            if (batch[count++].type == SC_RECORD_STATUS) break;
        }
        if (!count && journal.queued_records) fail("journal_read_or_identity_needs_attention");
        xSemaphoreGive(journal_lock);
        if (!count) { isolate = false; vTaskDelay(pdMS_TO_TICKS(300000)); continue; }
        char *payload = sc_wire_batch(batch, count);
        if (!payload) { fail("batch_encoding_failed"); vTaskDelay(pdMS_TO_TICKS(30000)); continue; }
        int status = sc_network_post(payload, response, sizeof(response));
        if (status == 200 && sc_wire_ack(response, batch, count)) {
            bool ok = true;
            xSemaphoreTake(journal_lock, portMAX_DELAY);
            for (size_t i = 0; i < count && ok; ++i) ok = sc_queue_ack(&journal, batch[i].queue_id);
            xSemaphoreGive(journal_lock);
            if (!ok) fail("journal_ack_failed");
            free(payload); backoff = 5; isolate = false;
            vTaskDelay(pdMS_TO_TICKS(1000));
        } else {
            if ((status == 400 || status == 409 || status == 422 || status == 413) && count > 1) {
                isolate = true; free(payload); continue;
            }
            if (status == 400 || status == 409 || status == 422 || status == 413) {
                xSemaphoreTake(journal_lock, portMAX_DELAY);
                bool saved = quarantine_healthy && sc_reject_append("/spiffs/rejected", batch[0].queue_id,
                    status, payload, &quarantined_records);
                bool advanced = saved && sc_queue_ack(&journal, batch[0].queue_id);
                xSemaphoreGive(journal_lock);
                if (advanced) {
                    fail("record_quarantined_requires_attention");
                    free(payload); isolate = false; vTaskDelay(pdMS_TO_TICKS(1000)); continue;
                }
                fail("quarantine_unavailable_record_retained");
            }
            free(payload);
            /* An HTTP success without matching per-record ACK never deletes local data. */
            if (status == 401 || status == 403) fail("device_auth_rejected");
            else if (status == 409 || status == 422 || status == 400)
                fail("batch_rejected_retained");
            uint32_t pause = backoff + esp_random() % 4;
            if (status == 401 || status == 403 || status == 409 || status == 422 || status == 400)
                pause = 300;
            uint32_t retry_after = sc_network_retry_after_s();
            if (retry_after > pause) pause = retry_after;
            if (backoff < 300) backoff = backoff * 2 > 300 ? 300 : backoff * 2;
            vTaskDelay(pdMS_TO_TICKS(pause * 1000));
        }
    }
}
void app_main(void) {
    if (sc_config_init() != ESP_OK) { fail("configuration_storage_unavailable"); return; }
    if (xTaskCreate(sc_config_uart_task, "provision", 6144, NULL, 2, NULL) != pdPASS) {
        fail("provision_task_unavailable"); return;
    }
    if (!sc_config_load(&config)) {
        puts("{\"status\":\"needs_configuration\"}"); return;
    }
    observations = xQueueCreate(96, sizeof(observation_t));
    journal_lock = xSemaphoreCreateMutex();
    if (!observations || !journal_lock) { fail("memory_unavailable"); return; }
    if (!mount_storage()) { fail("storage_needs_attention_no_automatic_erase"); return; }
    sc_source_t source = {0};
    boot_uuid(source.boot_id);
    strcpy(source.device_id, config.device_id);
    strcpy(source.measurement_profile_id, config.profile_id);
    storage_ok = sc_queue_open(&journal, "/spiffs/queue", &source, 1024 * 1024);
    if (!storage_ok) { fail("journal_recovery_failed"); return; }
    if (!sc_reject_count("/spiffs/rejected", &quarantined_records)) {
        quarantine_healthy = false;
        fail("quarantine_integrity_needs_attention");
    }
    if (sc_board_init() != ESP_OK) fail("sensor_initialization_degraded");
    sc_aggregate_init(&aggregate, monotonic_ms(), emit_minute, NULL);
    if (xTaskCreate(collect_task, "aggregate", 8192, NULL, 4, NULL) != pdPASS ||
        xTaskCreatePinnedToCore(audio_task, "audio", 4096, NULL, 12, NULL, 1) != pdPASS ||
        xTaskCreate(sensor_task, "sensors", 3072, NULL, 5, NULL) != pdPASS) {
        fail("collection_task_unavailable"); return;
    }
    if (sc_network_init(&config, time_synced) != ESP_OK) {
        fail("network_initialization_failed"); return;
    }
    if (xTaskCreate(upload_task, "upload", 8192, NULL, 3, NULL) != pdPASS)
        fail("upload_task_unavailable");
}
