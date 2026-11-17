#define _POSIX_C_SOURCE 200809L
#include "sc_usb.h"
#include <assert.h>
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static sc_source_t source(const char *boot) {
    sc_source_t s = {0};
    memset(s.device_id, 'd', 64); memset(s.measurement_profile_id, 'p', 64);
    strcpy(s.boot_id, boot); return s;
}
static void append_minute(sc_queue_t *queue, uint64_t seq) {
    sc_minute_t minute; sc_minute_reset(&minute, seq, 60000);
    minute.duration_ms = 60000;
    uint8_t bytes[SC_RECORD_MAX];
    size_t size = sc_minute_encode(&minute, bytes, sizeof(bytes));
    assert(size && sc_queue_append(queue, SC_RECORD_MINUTE, bytes, size, NULL));
}
static cJSON *acknowledgement(const sc_usb_transfer_t *tx) {
    cJSON *response = cJSON_Parse(tx->line);
    cJSON *batch = cJSON_GetObjectItemCaseSensitive(response, "batch");
    cJSON *ack = cJSON_CreateObject();
    cJSON_AddNumberToObject(ack, "schema_version", 1);
    cJSON_AddStringToObject(ack, "device_id", tx->pending[0].source.device_id);
    cJSON_AddStringToObject(ack, "boot_id", tx->pending[0].source.boot_id);
    cJSON_AddStringToObject(ack, "received_at", "2026-01-01T00:00:00.000000Z");
    cJSON *records = cJSON_AddArrayToObject(ack, "records");
    cJSON_AddArrayToObject(ack, "time_anchors");
    cJSON_AddNullToObject(ack, "device_status");
    const cJSON *r = cJSON_GetObjectItemCaseSensitive(batch, "records");
    for (const cJSON *row = r->child; row; row = row->next) {
        cJSON *item = cJSON_CreateObject();
        cJSON_AddStringToObject(item, "record_type", "minute");
        cJSON_AddNumberToObject(item, "seq", cJSON_GetObjectItemCaseSensitive(row, "seq")->valuedouble);
        cJSON_AddStringToObject(item, "status", "accepted");
        assert(cJSON_AddItemToArray(records, item));
    }
    cJSON_Delete(response); return ack;
}
static void remove_directory(const char *path) {
    DIR *dir = opendir(path); assert(dir);
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        char name[512]; snprintf(name, sizeof(name), "%s/%s", path, entry->d_name);
        assert(unlink(name) == 0);
    }
    closedir(dir); assert(rmdir(path) == 0);
}
int main(void) {
    assert(sc_usb_identifier("bedroom-01"));
    assert(!sc_usb_identifier("-bad")); assert(!sc_usb_identifier("invalid\""));
    cJSON *parsed = sc_usb_parse_command("{\"command\":\"usb_poll\"}");
    assert(parsed); cJSON_Delete(parsed);
    assert(!sc_usb_parse_command("{\"command\":\"usb_poll\",\"command\":\"usb_stop\"}"));
    assert(!sc_usb_parse_command("{\"command\":\"usb_poll\"} trailing"));
    assert(!sc_usb_parse_command("{\"a\":{\"b\":0,\"b\":1}}"));
    assert(!sc_usb_parse_command("[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]"));
    char oversized[SC_USB_COMMAND_BYTES + 1]; memset(oversized, ' ', sizeof(oversized));
    oversized[sizeof(oversized) - 1] = 0; assert(!sc_usb_parse_command(oversized));

    char dir[] = "/tmp/sleepscape-usb-test-XXXXXX"; assert(mkdtemp(dir));
    char prefix[192]; snprintf(prefix, sizeof(prefix), "%s/queue", dir);
    sc_source_t old = source("11111111-1111-4111-8111-111111111111");
    sc_source_t now = source("22222222-2222-4222-8222-222222222222");
    sc_queue_t queue; assert(sc_queue_open(&queue, prefix, &old, 1024 * 1024));
    sc_usb_transfer_t tx = {0};
    assert(!sc_usb_prepare(&tx, &queue, old.device_id)); assert(!tx.line);
    for (uint64_t i = 0; i < 5; ++i) append_minute(&queue, UINT64_C(9007199254740980) + i);
    assert(sc_queue_open(&queue, prefix, &now, 1024 * 1024));
    append_minute(&queue, 0);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id));
    assert(tx.count == 4 && queue.queued_records == 6);
    assert(!strcmp(tx.pending[0].source.boot_id, old.boot_id));
    assert(strncmp(tx.batch_id, now.boot_id, strlen(now.boot_id)) == 0);
    char *first = strdup(tx.line); assert(first);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id)); assert(!strcmp(first, tx.line));
    assert(sc_usb_acknowledge(&tx, &queue, tx.batch_id, NULL));
    cJSON *ack = acknowledgement(&tx);
    assert(sc_usb_acknowledge(&tx, &queue, "different-batch", ack));
    cJSON_ReplaceItemInObjectCaseSensitive(ack, "boot_id", cJSON_CreateString(now.boot_id));
    assert(sc_usb_acknowledge(&tx, &queue, tx.batch_id, ack));
    assert(queue.queued_records == 6 && !strcmp(first, tx.line));
    cJSON_ReplaceItemInObjectCaseSensitive(ack, "boot_id", cJSON_CreateString(old.boot_id));

    /* Max-sized allowed IDs and full wrapper fit RX. Avoid cJSON's tolerant
     * float printer for seq in this host-generated wire fixture, as Python's
     * production JSON encoder also preserves integers exactly. */
    cJSON *command = cJSON_CreateObject();
    cJSON_AddStringToObject(command, "command", "usb_ack");
    cJSON_AddStringToObject(command, "batch_id", tx.batch_id);
    assert(cJSON_AddItemToObject(command, "ack", cJSON_Duplicate(ack, true)));
    cJSON *wire_ack = cJSON_GetObjectItemCaseSensitive(command, "ack");
    cJSON *wire_records = cJSON_GetObjectItemCaseSensitive(wire_ack, "records");
    for (cJSON *row = wire_records->child; row; row = row->next) {
        char integer[32]; snprintf(integer, sizeof(integer), "%.0f",
            cJSON_GetObjectItemCaseSensitive(row, "seq")->valuedouble);
        cJSON_ReplaceItemInObjectCaseSensitive(row, "seq", cJSON_CreateRaw(integer));
    }
    char *wire = cJSON_PrintUnformatted(command); assert(wire);
    assert(strlen(wire) + 1 < SC_USB_COMMAND_BYTES);
    parsed = sc_usb_parse_command(wire); assert(parsed);
    cJSON_Delete(ack);
    ack = cJSON_Duplicate(cJSON_GetObjectItemCaseSensitive(parsed, "ack"), true);
    cJSON_Delete(parsed);
    free(wire); cJSON_Delete(command);

    char first_id[SC_USB_BATCH_ID_BYTES]; strcpy(first_id, tx.batch_id);
    const char *error = sc_usb_acknowledge(&tx, &queue, first_id, ack);
    if (error) fprintf(stderr, "Unexpected valid ACK failure: %s\n", error);
    assert(!error);
    assert(queue.queued_records == 2 && !tx.line);
    assert(!sc_usb_acknowledge(&tx, &queue, first_id, ack));
    assert(queue.queued_records == 2);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id));
    assert(tx.count == 1 && !strcmp(tx.pending[0].source.boot_id, old.boot_id));
    char *second = strdup(tx.line); assert(second);
    assert(!sc_usb_acknowledge(&tx, &queue, first_id, ack));
    assert(!strcmp(second, tx.line) && queue.queued_records == 2);
    cJSON_Delete(ack); ack = acknowledgement(&tx);
    assert(!sc_usb_acknowledge(&tx, &queue, tx.batch_id, ack)); cJSON_Delete(ack);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id));
    assert(tx.count == 1 && !strcmp(tx.pending[0].source.boot_id, now.boot_id));
    /* A lost host ACK leaves the durable record available after a reset. */
    sc_usb_transfer_clear(&tx);
    assert(sc_queue_open(&queue, prefix, &now, 1024 * 1024));
    assert(queue.queued_records == 1);
    assert(sc_usb_prepare(&tx, &queue, "wrong-device"));
    assert(queue.queued_records == 1);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id));
    ack = acknowledgement(&tx);
    assert(!sc_usb_acknowledge(&tx, &queue, tx.batch_id, ack)); cJSON_Delete(ack);
    assert(!sc_usb_prepare(&tx, &queue, now.device_id)); assert(!tx.line);
    sc_usb_transfer_clear(&tx); free(first); free(second); remove_directory(dir);
    puts("USB transport: parser bounds, batch limit, retransmit, exact ACK, ACK retry, boot grouping and durable replay passed");
    return 0;
}
