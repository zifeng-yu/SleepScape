#ifndef SC_QUEUE_H
#define SC_QUEUE_H
#include "sc_core.h"

#define SC_QUEUE_SEGMENTS 64u
#define SC_QUEUE_SEGMENT_RECORDS 128u
#define SC_QUEUE_HEADER_BYTES 192u
#define SC_QUEUE_ENVELOPE_BYTES 20u
#define SC_QUEUE_PATH_MAX 192u

/* A segment stores complete source identity once. Different boots/configs rotate. */
typedef struct {
    char device_id[65];
    char boot_id[37];
    char measurement_profile_id[65];
} sc_source_t;
typedef struct {
    uint64_t id, first_id, last_id;
    uint32_t bytes;
    uint16_t records;
} sc_segment_t;
typedef struct {
    char prefix[SC_QUEUE_PATH_MAX];
    sc_source_t source;
    sc_segment_t segments[SC_QUEUE_SEGMENTS];
    uint16_t segment_count;
    uint64_t next_id, acked_id, generation;
    uint64_t dropped[4];
    uint64_t first_dropped_id, last_dropped_id;
    uint32_t corrupt_records;
    uint32_t max_bytes, used_bytes, queued_records;
    bool healthy;
} sc_queue_t;
typedef struct {
    uint64_t queue_id;
    sc_source_t source;
    sc_record_type_t type;
    uint16_t length;
    uint8_t payload[SC_RECORD_MAX];
} sc_queue_item_t;

/* Prefix e.g. /spiffs/queue, not a directory. Caller serializes access with a mutex. */
bool sc_queue_open(sc_queue_t *queue, const char *prefix, const sc_source_t *source,
                   uint32_t max_bytes);
bool sc_queue_append(sc_queue_t *queue, sc_record_type_t type,
                     const void *payload, size_t length, uint64_t *queue_id);
/* after_id=0 finds oldest pending. Does not mutate queue. */
bool sc_queue_peek(const sc_queue_t *queue, uint64_t after_id, sc_queue_item_t *out);
/* Only current oldest pending may be ACKed. Duplicate old ACKs are harmless. */
bool sc_queue_ack(sc_queue_t *queue, uint64_t queue_id);

#endif
