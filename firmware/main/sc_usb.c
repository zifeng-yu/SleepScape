#define _POSIX_C_SOURCE 200809L
#include "sc_usb.h"
#include "sc_wire.h"
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

bool sc_usb_identifier(const char *value) {
    if (!value) return false;
    size_t length = strnlen(value, 65);
    if (!length || length > 64) return false;
    for (size_t i = 0; i < length; ++i) {
        bool alnum = (value[i] >= 'a' && value[i] <= 'z') ||
            (value[i] >= 'A' && value[i] <= 'Z') || (value[i] >= '0' && value[i] <= '9');
        if (!alnum && (!i || !strchr("_.-", value[i]))) return false;
    }
    return true;
}
static bool bounded_json(const char *text) {
    if (!text || strnlen(text, SC_USB_COMMAND_BYTES) >= SC_USB_COMMAND_BYTES) return false;
    bool quoted = false, escaped = false; unsigned depth = 0;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
        if (quoted) {
            if (escaped) escaped = false;
            else if (*p == '\\') escaped = true;
            else if (*p == '"') quoted = false;
        } else if (*p == '"') quoted = true;
        else if (*p == '{' || *p == '[') { if (++depth > 16) return false; }
        else if (*p == '}' || *p == ']') { if (!depth) return false; --depth; }
    }
    return !depth && !quoted;
}
static bool unique_keys(const cJSON *value) {
    for (const cJSON *a = value->child; a; a = a->next) {
        if (cJSON_IsObject(value))
            for (const cJSON *b = a->next; b; b = b->next)
                if (!a->string || !b->string || !strcmp(a->string, b->string)) return false;
        if (!unique_keys(a)) return false;
    }
    return true;
}
cJSON *sc_usb_parse_command(const char *line) {
    if (!bounded_json(line)) return NULL;
    cJSON *json = cJSON_ParseWithOpts(line, NULL, true);
    if (!cJSON_IsObject(json) || !unique_keys(json)) { cJSON_Delete(json); return NULL; }
    return json;
}
void sc_usb_print_line(const char *line) {
    flockfile(stdout);
    fputs(line, stdout); fputc('\n', stdout); fflush(stdout);
    funlockfile(stdout);
}
void sc_usb_transfer_clear(sc_usb_transfer_t *tx) {
    free(tx->line);
    memset(tx, 0, sizeof(*tx));
}
const char *sc_usb_prepare(sc_usb_transfer_t *tx, sc_queue_t *queue,
                          const char *device_id) {
    if (tx->line) return NULL;
    tx->count = 0;
    uint64_t cursor = 0;
    while (tx->count < SC_USB_MAX_ITEMS &&
           sc_queue_peek(queue, cursor, &tx->pending[tx->count])) {
        const sc_source_t *source = &tx->pending[tx->count].source;
        if (strcmp(source->device_id, device_id)) {
            if (!tx->count) return "queued_device_identity_mismatch";
            break;
        }
        if (tx->count && (strcmp(source->boot_id, tx->pending[0].source.boot_id) ||
            strcmp(source->measurement_profile_id,
                   tx->pending[0].source.measurement_profile_id))) break;
        cursor = tx->pending[tx->count].queue_id;
        if (tx->pending[tx->count++].type == SC_RECORD_STATUS) break;
    }
    if (!tx->count) return queue->queued_records ? "journal_read_needs_attention" : NULL;
    char *batch = sc_wire_batch(tx->pending, tx->count);
    if (!batch) { tx->count = 0; return "batch_encoding_failed"; }
    int length = snprintf(tx->batch_id, sizeof(tx->batch_id), "%s-%" PRIu64 "-%" PRIu64,
        queue->source.boot_id, tx->pending[0].queue_id, tx->pending[tx->count - 1].queue_id);
    cJSON *response = cJSON_CreateObject();
    bool ok = length > 0 && (size_t)length < sizeof(tx->batch_id) && response &&
        cJSON_AddStringToObject(response, "status", "usb_batch") &&
        cJSON_AddStringToObject(response, "batch_id", tx->batch_id) &&
        cJSON_AddRawToObject(response, "batch", batch);
    free(batch);
    tx->line = ok ? cJSON_PrintUnformatted(response) : NULL;
    cJSON_Delete(response);
    if (!tx->line || strlen(tx->line) > SC_WIRE_MAX_BYTES) {
        free(tx->line); tx->line = NULL; tx->count = 0;
        return "batch_encoding_failed";
    }
    return NULL;
}
const char *sc_usb_acknowledge(sc_usb_transfer_t *tx, sc_queue_t *queue,
                              const char *batch_id, const cJSON *ack) {
    if (!batch_id || strnlen(batch_id, SC_USB_BATCH_ID_BYTES) >= SC_USB_BATCH_ID_BYTES ||
        !cJSON_IsObject(ack)) return "invalid_usb_ack";
    bool duplicate = tx->acknowledged_count && !strcmp(batch_id, tx->acknowledged_id);
    if (!duplicate && (!tx->count || !tx->line || strcmp(batch_id, tx->batch_id)))
        return "usb_batch_identity_mismatch";
    bool valid = sc_wire_ack_json(ack,
        duplicate ? tx->acknowledged : tx->pending,
        duplicate ? tx->acknowledged_count : tx->count);
    if (!valid) return "invalid_usb_ack";
    if (duplicate) return NULL;
    for (size_t i = 0; i < tx->count; ++i)
        if (!sc_queue_ack(queue, tx->pending[i].queue_id)) return "journal_ack_failed";
    memcpy(tx->acknowledged, tx->pending, tx->count * sizeof(tx->pending[0]));
    tx->acknowledged_count = tx->count;
    strcpy(tx->acknowledged_id, tx->batch_id);
    free(tx->line); tx->line = NULL; tx->count = 0;
    return NULL;
}
