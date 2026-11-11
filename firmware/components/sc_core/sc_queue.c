#include "sc_queue.h"

#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define META_BYTES 96u

static void set32(uint8_t *p, uint32_t n) {
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(n >> (8 * i));
}
static void set64(uint8_t *p, uint64_t n) {
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(n >> (8 * i));
}
static uint32_t read32(const uint8_t *p) {
    uint32_t n = 0; for (unsigned i = 0; i < 4; ++i) n |= (uint32_t)p[i] << (8 * i);
    return n;
}
static uint64_t read64(const uint8_t *p) {
    uint64_t n = 0; for (unsigned i = 0; i < 8; ++i) n |= (uint64_t)p[i] << (8 * i);
    return n;
}
static bool safe_id(const char *value, size_t capacity) {
    const char *end = memchr(value, 0, capacity);
    if (!end || end == value) return false;
    for (const char *p = value; p < end; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '_' || *p == '-' || *p == '.' || *p == ':'))
            return false;
    return true;
}
static bool valid_source(const sc_source_t *source) {
    return source && safe_id(source->device_id, sizeof(source->device_id)) &&
        safe_id(source->boot_id, sizeof(source->boot_id)) &&
        safe_id(source->measurement_profile_id, sizeof(source->measurement_profile_id));
}
static bool equal_source(const sc_source_t *a, const sc_source_t *b) {
    return !strcmp(a->device_id, b->device_id) && !strcmp(a->boot_id, b->boot_id) &&
        !strcmp(a->measurement_profile_id, b->measurement_profile_id);
}
static bool durable_close(FILE *file) {
    bool ok = fflush(file) == 0;
    if (ok) ok = fsync(fileno(file)) == 0;
    if (fclose(file) != 0) ok = false;
    return ok;
}
static bool segment_path(const sc_queue_t *q, uint64_t id, char *out) {
    int n = snprintf(out, SC_QUEUE_PATH_MAX + 32, "%s-%016llx.scq", q->prefix,
                     (unsigned long long)id);
    return n >= 0 && (unsigned)n < SC_QUEUE_PATH_MAX + 32;
}
static void meta_path(const sc_queue_t *q, unsigned copy, char *out) {
    snprintf(out, SC_QUEUE_PATH_MAX + 32, "%s.meta%u", q->prefix, copy);
}
static void encode_meta(const sc_queue_t *q, uint8_t *data, uint64_t generation) {
    memset(data, 0, META_BYTES); memcpy(data, "SCQM", 4); set32(data + 4, 1);
    set64(data + 8, generation); set64(data + 16, q->acked_id); set64(data + 24, q->next_id);
    for (unsigned i = 0; i < 4; ++i) set64(data + 32 + i * 8, q->dropped[i]);
    set64(data + 64, q->first_dropped_id); set64(data + 72, q->last_dropped_id);
    set32(data + 80, q->corrupt_records); set32(data + 92, sc_crc32(data, 92));
}
static bool load_meta(const sc_queue_t *q, unsigned copy, uint8_t *data, bool *exists) {
    char path[SC_QUEUE_PATH_MAX + 32]; meta_path(q, copy, path);
    FILE *f = fopen(path, "rb");
    if (!f) { *exists = errno != ENOENT; return false; }
    *exists = true;
    bool ok = fread(data, 1, META_BYTES, f) == META_BYTES && fgetc(f) == EOF;
    fclose(f);
    return ok && !memcmp(data, "SCQM", 4) && read32(data + 4) == 1 &&
        read32(data + 92) == sc_crc32(data, 92);
}
static void decode_meta(sc_queue_t *q, const uint8_t *data) {
    q->generation = read64(data + 8); q->acked_id = read64(data + 16);
    q->next_id = read64(data + 24);
    for (unsigned i = 0; i < 4; ++i) q->dropped[i] = read64(data + 32 + i * 8);
    q->first_dropped_id = read64(data + 64); q->last_dropped_id = read64(data + 72);
    q->corrupt_records = read32(data + 80);
}
static bool save_meta(sc_queue_t *q) {
    uint8_t data[META_BYTES]; uint64_t generation = q->generation + 1;
    encode_meta(q, data, generation);
    /* Both durable copies precede deletion, so one corrupt copy cannot erase loss history. */
    for (unsigned i = 0; i < 2; ++i) {
        char path[SC_QUEUE_PATH_MAX + 32]; meta_path(q, i, path);
        FILE *f = fopen(path, "wb");
        if (!f) { q->healthy = false; return false; }
        bool ok = fwrite(data, 1, sizeof(data), f) == sizeof(data);
        if (!durable_close(f)) ok = false;
        if (!ok) { q->healthy = false; return false; }
    }
    q->generation = generation;
    return true;
}
static void encode_header(const sc_source_t *source, uint64_t id, uint8_t *data) {
    memset(data, 0, SC_QUEUE_HEADER_BYTES); memcpy(data, "SCQS", 4);
    set32(data + 4, 1); set64(data + 8, id);
    memcpy(data + 16, source->boot_id, strlen(source->boot_id));
    memcpy(data + 53, source->measurement_profile_id, strlen(source->measurement_profile_id));
    memcpy(data + 118, source->device_id, strlen(source->device_id));
    set32(data + 188, sc_crc32(data, 188));
}
static bool read_header(FILE *f, uint64_t id, sc_source_t *source) {
    uint8_t data[SC_QUEUE_HEADER_BYTES];
    if (fread(data, 1, sizeof(data), f) != sizeof(data) || memcmp(data, "SCQS", 4) ||
        read32(data + 4) != 1 || read64(data + 8) != id ||
        read32(data + 188) != sc_crc32(data, 188)) return false;
    memcpy(source->boot_id, data + 16, sizeof(source->boot_id));
    memcpy(source->measurement_profile_id, data + 53, sizeof(source->measurement_profile_id));
    memcpy(source->device_id, data + 118, sizeof(source->device_id));
    return valid_source(source);
}
static size_t payload_size(sc_record_type_t type) {
    switch (type) {
        case SC_RECORD_MINUTE: return SC_MINUTE_BYTES;
        case SC_RECORD_EVENT: return SC_EVENT_BYTES;
        case SC_RECORD_ANCHOR: return SC_ANCHOR_BYTES;
        case SC_RECORD_STATUS: return SC_STATUS_BYTES;
        default: return 0;
    }
}
/* 1 record, 0 clean EOF, -1 incomplete/corrupt tail. */
static int read_record(FILE *f, sc_queue_item_t *item) {
    uint8_t header[SC_QUEUE_ENVELOPE_BYTES];
    size_t n = fread(header, 1, sizeof(header), f);
    if (ferror(f)) return -2;
    if (!n && feof(f)) return 0;
    if (n != sizeof(header) || memcmp(header, "SCQR", 4) || header[5] != 1) return -1;
    item->type = (sc_record_type_t)header[4];
    item->length = (uint16_t)(header[6] | (uint16_t)header[7] << 8);
    item->queue_id = read64(header + 8);
    if (!item->queue_id || item->length != payload_size(item->type) ||
        !item->length || item->length > SC_RECORD_MAX) return -1;
    if (fread(item->payload, 1, item->length, f) != item->length)
        return ferror(f) ? -2 : -1;
    uint8_t checked[16 + SC_RECORD_MAX];
    memcpy(checked, header, 16); memcpy(checked + 16, item->payload, item->length);
    return read32(header + 16) == sc_crc32(checked, 16 + item->length) ? 1 : -1;
}
static int compare_segments(const void *a, const void *b) {
    const sc_segment_t *x = a, *y = b; return (x->id > y->id) - (x->id < y->id);
}
static bool list_segments(sc_queue_t *q) {
    char directory[SC_QUEUE_PATH_MAX], base[SC_QUEUE_PATH_MAX];
    const char *slash = strrchr(q->prefix, '/');
    if (slash) {
        size_t n = (size_t)(slash - q->prefix);
        if (!n) strcpy(directory, "/");
        else { memcpy(directory, q->prefix, n); directory[n] = 0; }
        strcpy(base, slash + 1);
    } else { strcpy(directory, "."); strcpy(base, q->prefix); }
    DIR *dir = opendir(directory);
    if (!dir) return false;
    size_t length = strlen(base); struct dirent *entry;
    for (;;) {
        /* SPIFFS reports directory I/O failure through errno, just as POSIX
         * does. A partial directory listing must never become an empty queue. */
        errno = 0;
        entry = readdir(dir);
        if (!entry) {
            bool ok = errno == 0;
            if (closedir(dir) != 0) ok = false;
            if (!ok) return false;
            break;
        }
        if (strncmp(entry->d_name, base, length) || entry->d_name[length] != '-') continue;
        unsigned long long id; int consumed = 0;
        const char *suffix = entry->d_name + length + 1;
        if (sscanf(suffix, "%16llx.scq%n", &id, &consumed) != 1 ||
            !consumed || suffix[consumed]) continue;
        if (!id || q->segment_count == SC_QUEUE_SEGMENTS) { closedir(dir); return false; }
        q->segments[q->segment_count++].id = (uint64_t)id;
    }
    qsort(q->segments, q->segment_count, sizeof(q->segments[0]), compare_segments);
    return true;
}
static bool remove_first(sc_queue_t *q) {
    if (!q->segment_count) return false;
    char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, q->segments[0].id, path);
    if (remove(path) != 0) { q->healthy = false; return false; }
    q->used_bytes -= q->segments[0].bytes;
    --q->segment_count;
    memmove(q->segments, q->segments + 1, q->segment_count * sizeof(q->segments[0]));
    return true;
}
static bool clean_acked(sc_queue_t *q) {
    while (q->segment_count && q->segments[0].last_id &&
           q->segments[0].last_id <= q->acked_id)
        if (!remove_first(q)) return false;
    return true;
}

bool sc_queue_open(sc_queue_t *q, const char *prefix, const sc_source_t *source,
                   uint32_t max_bytes) {
    if (!q || !prefix || !*prefix || strlen(prefix) >= SC_QUEUE_PATH_MAX ||
        !valid_source(source) || max_bytes < SC_QUEUE_HEADER_BYTES + SC_MINUTE_BYTES +
        SC_QUEUE_ENVELOPE_BYTES) return false;
    memset(q, 0, sizeof(*q)); strcpy(q->prefix, prefix); q->source = *source;
    q->max_bytes = max_bytes; q->next_id = 1;
    uint8_t a[META_BYTES], b[META_BYTES]; bool exists_a, exists_b;
    bool valid_a = load_meta(q, 0, a, &exists_a), valid_b = load_meta(q, 1, b, &exists_b);
    if (!list_segments(q)) return false;
    if (!valid_a && !valid_b && (exists_a || exists_b || q->segment_count)) return false;
    if (valid_a || valid_b)
        decode_meta(q, !valid_b || (valid_a && read64(a + 8) >= read64(b + 8)) ? a : b);
    q->healthy = true;
    uint64_t previous_id = 0;
    for (uint16_t i = 0; i < q->segment_count; ++i) {
        sc_segment_t *segment = &q->segments[i];
        char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, segment->id, path);
        FILE *f = fopen(path, "rb+"); sc_source_t recorded_source;
        if (!f || !read_header(f, segment->id, &recorded_source)) {
            if (f) fclose(f);
            q->healthy = false;
            return false;
        }
        segment->bytes = SC_QUEUE_HEADER_BYTES;
        for (;;) {
            sc_queue_item_t item; int result = read_record(f, &item);
            if (!result) break;
            if (result < 0) {
                if (result == -2) { fclose(f); q->healthy = false; return false; }
                ++q->corrupt_records;
                if (!save_meta(q)) { fclose(f); return false; }
                /* Never reinterpret a partial record or scan payload bytes as a header. */
                if (fflush(f) || ftruncate(fileno(f), segment->bytes) || fsync(fileno(f))) {
                    fclose(f); q->healthy = false; return false;
                }
                break;
            }
            if (item.queue_id <= previous_id) { fclose(f); q->healthy = false; return false; }
            previous_id = item.queue_id;
            if (!segment->records) segment->first_id = item.queue_id;
            segment->last_id = item.queue_id; ++segment->records;
            segment->bytes += SC_QUEUE_ENVELOPE_BYTES + item.length;
            if (item.queue_id > q->acked_id) ++q->queued_records;
            if (item.queue_id >= q->next_id) q->next_id = item.queue_id + 1;
        }
        fclose(f); q->used_bytes += segment->bytes;
    }
    if (q->next_id <= q->acked_id) q->next_id = q->acked_id + 1;
    if (!save_meta(q) || !clean_acked(q)) return false;
    return true;
}

bool sc_queue_peek(const sc_queue_t *q, uint64_t after_id, sc_queue_item_t *out) {
    if (!q || !q->healthy || !out) return false;
    uint64_t after = after_id > q->acked_id ? after_id : q->acked_id;
    for (uint16_t i = 0; i < q->segment_count; ++i) {
        const sc_segment_t *segment = &q->segments[i];
        if (!segment->last_id || segment->last_id <= after) continue;
        char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, segment->id, path);
        FILE *f = fopen(path, "rb");
        if (!f) return false;
        sc_source_t source;
        if (!read_header(f, segment->id, &source)) { fclose(f); return false; }
        int result;
        while ((result = read_record(f, out)) == 1) {
            if (out->queue_id > after) { out->source = source; fclose(f); return true; }
        }
        fclose(f);
        if (result < 0) return false;
    }
    return false;
}

static bool discard_oldest_segment(sc_queue_t *q) {
    if (!q->segment_count) return false;
    sc_segment_t *segment = &q->segments[0];
    if (!segment->records) return remove_first(q);
    char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, segment->id, path);
    FILE *f = fopen(path, "rb"); sc_source_t source;
    if (!f || !read_header(f, segment->id, &source)) {
        if (f) fclose(f);
        return false;
    }
    sc_queue_item_t item; int result;
    while ((result = read_record(f, &item)) == 1) {
        if (item.queue_id > q->acked_id) {
            ++q->dropped[item.type - 1];
            if (!q->first_dropped_id) q->first_dropped_id = item.queue_id;
            q->last_dropped_id = item.queue_id;
            --q->queued_records;
        }
    }
    fclose(f);
    if (result < 0) { q->healthy = false; return false; }
    if (segment->last_id > q->acked_id) q->acked_id = segment->last_id;
    /* Commit losses before deleting any evidence. Power loss can only cause replays. */
    return save_meta(q) && remove_first(q);
}

static bool make_segment(sc_queue_t *q) {
    while (q->segment_count >= SC_QUEUE_SEGMENTS ||
           q->used_bytes + SC_QUEUE_HEADER_BYTES > q->max_bytes)
        if (!discard_oldest_segment(q)) return false;
    uint64_t id = q->next_id;
    if (q->segment_count && id <= q->segments[q->segment_count - 1].id)
        id = q->segments[q->segment_count - 1].id + 1;
    char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, id, path);
    FILE *f = fopen(path, "wb");
    if (!f) { q->healthy = false; return false; }
    uint8_t header[SC_QUEUE_HEADER_BYTES]; encode_header(&q->source, id, header);
    bool ok = fwrite(header, 1, sizeof(header), f) == sizeof(header);
    if (!durable_close(f)) ok = false;
    if (!ok) { q->healthy = false; return false; }
    sc_segment_t *segment = &q->segments[q->segment_count++];
    memset(segment, 0, sizeof(*segment)); segment->id = id; segment->bytes = sizeof(header);
    q->used_bytes += sizeof(header); return true;
}

static bool current_source_matches(sc_queue_t *q) {
    if (!q->segment_count) return false;
    char path[SC_QUEUE_PATH_MAX + 32];
    segment_path(q, q->segments[q->segment_count - 1].id, path);
    FILE *f = fopen(path, "rb"); if (!f) return false;
    sc_source_t source;
    bool ok = read_header(f, q->segments[q->segment_count - 1].id, &source);
    fclose(f); return ok && equal_source(&source, &q->source);
}

bool sc_queue_append(sc_queue_t *q, sc_record_type_t type,
                     const void *payload, size_t length, uint64_t *queue_id) {
    if (!q || !q->healthy || !payload || length != payload_size(type) ||
        !length || length > SC_RECORD_MAX || q->next_id == UINT64_MAX) return false;
    uint32_t bytes = (uint32_t)(SC_QUEUE_ENVELOPE_BYTES + length);
    while (q->segment_count && q->used_bytes + bytes + SC_QUEUE_HEADER_BYTES > q->max_bytes)
        if (!discard_oldest_segment(q)) return false;
    if (!q->segment_count || q->segments[q->segment_count - 1].records >=
        SC_QUEUE_SEGMENT_RECORDS || !current_source_matches(q))
        if (!make_segment(q)) return false;
    sc_segment_t *segment = &q->segments[q->segment_count - 1];
    uint8_t header[SC_QUEUE_ENVELOPE_BYTES] = {0}, checked[16 + SC_RECORD_MAX];
    memcpy(header, "SCQR", 4); header[4] = (uint8_t)type; header[5] = 1;
    header[6] = (uint8_t)length; header[7] = (uint8_t)(length >> 8);
    set64(header + 8, q->next_id);
    memcpy(checked, header, 16); memcpy(checked + 16, payload, length);
    set32(header + 16, sc_crc32(checked, 16 + length));
    char path[SC_QUEUE_PATH_MAX + 32]; segment_path(q, segment->id, path);
    FILE *f = fopen(path, "ab");
    if (!f) { q->healthy = false; return false; }
    bool ok = fwrite(header, 1, sizeof(header), f) == sizeof(header) &&
        fwrite(payload, 1, length, f) == length;
    if (!durable_close(f)) ok = false;
    if (!ok) { q->healthy = false; return false; }
    if (!segment->records) segment->first_id = q->next_id;
    segment->last_id = q->next_id; segment->bytes += bytes; ++segment->records;
    q->used_bytes += bytes; ++q->queued_records;
    if (queue_id) *queue_id = q->next_id;
    ++q->next_id; return true;
}

bool sc_queue_ack(sc_queue_t *q, uint64_t queue_id) {
    if (!q || !q->healthy || !queue_id) return false;
    if (queue_id <= q->acked_id) return true;
    sc_queue_item_t item;
    if (!sc_queue_peek(q, 0, &item) || item.queue_id != queue_id) return false;
    q->acked_id = queue_id;
    if (!save_meta(q)) return false;
    --q->queued_records;
    return clean_acked(q);
}
