#define _POSIX_C_SOURCE 200809L
#include "sc_wire.h"
#include "cJSON.h"

#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SAFE_INT UINT64_C(9007199254740991)

typedef struct { bool ok; } writer_t;
static void num(writer_t *w, cJSON *o, const char *key, double value) {
    if (!isfinite(value) || fabs(value) > (double)SAFE_INT ||
        !cJSON_AddNumberToObject(o, key, value)) w->ok = false;
}
static void nullable(writer_t *w, cJSON *o, const char *key, double value) {
    if (isfinite(value)) num(w, o, key, value);
    else if (!cJSON_AddNullToObject(o, key)) w->ok = false;
}
static void str(writer_t *w, cJSON *o, const char *key, const char *value) {
    if (!cJSON_AddStringToObject(o, key, value)) w->ok = false;
}
static void boolean(writer_t *w, cJSON *o, const char *key, bool value) {
    if (!cJSON_AddBoolToObject(o, key, value)) w->ok = false;
}
static cJSON *object(writer_t *w, cJSON *parent, const char *key) {
    cJSON *result = cJSON_AddObjectToObject(parent, key);
    if (!result) w->ok = false;
    return result;
}
static cJSON *array(writer_t *w, cJSON *parent, const char *key) {
    cJSON *result = cJSON_AddArrayToObject(parent, key);
    if (!result) w->ok = false;
    return result;
}
static void item(writer_t *w, cJSON *array, cJSON *value) {
    if (!value || !cJSON_AddItemToArray(array, value)) { cJSON_Delete(value); w->ok = false; }
}
static void quality(writer_t *w, cJSON *o, uint32_t flags, bool truncated) {
    const struct { uint32_t flag; const char *name; } names[] = {
        {SC_Q_MISSING, "missing"}, {SC_Q_PARTIAL, "partial_window"},
        {SC_Q_INVALID, "sensor_error"}, {SC_Q_DMA_OVERFLOW, "dma_overflow"},
        {SC_Q_CLIPPED, "clipped"}, {SC_Q_NUMERIC_FLOOR, "below_numeric_floor"},
        {SC_Q_WARMING, "warming"}, {SC_Q_INTERRUPTED, "audio_gap"},
        {SC_Q_MERGED, "merged"}, {SC_Q_INCOMPLETE, "incomplete"},
        {SC_Q_LOW_LIGHT, "low_light_limit"}, {SC_Q_OUT_OF_RANGE, "out_of_range"},
        {SC_Q_BUS_ERROR, "bus_error"}
    };
    cJSON *a = array(w, o, "quality");
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i)
        if (flags & names[i].flag) item(w, a, cJSON_CreateString(names[i].name));
    if (flags & ~((1u << 13) - 1)) w->ok = false;
    if (truncated) item(w, a, cJSON_CreateString("ranges_truncated"));
}
static const char *state(uint32_t expected, uint32_t observed, uint32_t valid) {
    return valid ? (valid == expected ? "available" : "partial") :
                   (observed ? "invalid" : "missing");
}
static void channel(writer_t *w, cJSON *o, const char *unit,
                    uint32_t expected, uint32_t observed, uint32_t valid) {
    str(w, o, "unit", unit); str(w, o, "state", state(expected, observed, valid));
    num(w, o, "expected_count", expected); num(w, o, "observed_count", observed);
    num(w, o, "valid_count", valid);
}
static void scalar(writer_t *w, cJSON *metrics, const char *name,
                   const char *unit, const sc_scalar_t *s) {
    cJSON *o = object(w, metrics, name);
    channel(w, o, unit, s->expected_count, s->observed_count, s->valid_count);
    nullable(w, o, "mean", sc_scalar_mean(s));
    nullable(w, o, "min", s->valid_count ? s->min : NAN);
    nullable(w, o, "max", s->valid_count ? s->max : NAN);
    /* Sampling-time quality survives replay under a different hardware profile. */
    quality(w, o, s->quality, false);
}
static void base(writer_t *w, cJSON *o, const sc_source_t *s, const char *kind,
                 uint64_t seq, uint64_t start, uint64_t end, uint32_t flags) {
    str(w, o, "record_type", kind); num(w, o, "seq", seq);
    str(w, o, "measurement_profile_id", s->measurement_profile_id);
    num(w, o, "mono_start_ms", start); num(w, o, "mono_end_ms", end);
    quality(w, o, flags, false);
}
static cJSON *minute(writer_t *w, const sc_queue_item_t *row) {
    sc_minute_t m;
    if (!sc_minute_decode(row->payload, row->length, &m) ||
        m.mono_start_ms > SAFE_INT - m.duration_ms) { w->ok = false; return NULL; }
    cJSON *o = cJSON_CreateObject();
    base(w, o, &row->source, "minute", m.seq, m.mono_start_ms,
         m.mono_start_ms + m.duration_ms, m.quality | (m.duration_ms < 60000 ? SC_Q_PARTIAL : 0));
    cJSON *metrics = object(w, o, "metrics");
    scalar(w, metrics, "temperature_c", "degC", &m.temperature);
    scalar(w, metrics, "relative_humidity_pct", "percent", &m.humidity);
    scalar(w, metrics, "illuminance_lux", "lx", &m.light);
    cJSON *s = object(w, metrics, "sound_relative");
    channel(w, s, "dBFS", m.sound_expected_count, m.sound_observed_count, m.sound_valid_count);
    nullable(w, s, "energy_mean_dbfs", sc_minute_sound_dbfs(&m));
    nullable(w, s, "max_1s_rms_dbfs", m.sound_valid_count ? m.sound_max_dbfs : NAN);
    num(w, s, "expected_samples", m.sound_expected_samples);
    num(w, s, "observed_samples", m.sound_observed_samples);
    num(w, s, "valid_samples", m.sound_valid_samples);
    num(w, s, "valid_duration_ms", (uint32_t)m.sound_valid_count * 1000);
    num(w, s, "valid_1s_count", m.sound_valid_count);
    num(w, s, "dma_overflow_count", m.sound_dma_overflow_count);
    num(w, s, "clipped_samples", m.sound_clipped_samples);
    num(w, s, "event_valid_duration_ms", m.events.valid_duration_ms);
    const char *detector = m.detector_state == SC_DETECTOR_READY ? "ready" :
                          m.detector_state == SC_DETECTOR_INTERRUPTED ? "interrupted" : "warming";
    str(w, s, "event_detector_state", detector);
    bool known = m.events.valid_duration_ms > 0;
    nullable(w, s, "detected_count", known ? m.events.detected_count : NAN);
    nullable(w, s, "stored_count", known ? m.events.stored_count : NAN);
    nullable(w, s, "merged_count", known ? m.events.merged_count : NAN);
    nullable(w, s, "dropped_count", known ? m.events.dropped_count : NAN);
    uint32_t flags = m.sound_quality;
    if (!known) flags |= m.detector_state == SC_DETECTOR_INTERRUPTED ? SC_Q_INTERRUPTED : SC_Q_WARMING;
    if (m.sound_valid_count && m.sound_energy_sum == 0) flags |= SC_Q_NUMERIC_FLOOR;
    quality(w, s, flags, false);
    return o;
}
static cJSON *event(writer_t *w, const sc_queue_item_t *row) {
    sc_event_bucket_t e; uint64_t seq;
    if (!sc_event_decode(row->payload, row->length, &seq, &e)) { w->ok = false; return NULL; }
    cJSON *o = cJSON_CreateObject();
    base(w, o, &row->source, "event", seq, e.mono_start_ms, e.mono_end_ms, e.quality);
    str(w, o, "event_kind", "relative_sound");
    num(w, o, "active_duration_ms", e.active_duration_ms);
    num(w, o, "max_1s_rms_dbfs", e.max_1s_rms_dbfs);
    nullable(w, o, "baseline_dbfs", e.baseline_dbfs);
    str(w, o, "threshold_profile_id", SC_THRESHOLD_PROFILE);
    boolean(w, o, "merged", (e.quality & SC_Q_MERGED) != 0);
    boolean(w, o, "incomplete", (e.quality & SC_Q_INCOMPLETE) != 0);
    boolean(w, o, "continued_from_previous", e.continued_from_previous);
    boolean(w, o, "continues_next", e.continues_next);
    return o;
}
static cJSON *anchor(writer_t *w, const sc_queue_item_t *row) {
    sc_anchor_t a;
    if (!sc_anchor_decode(row->payload, row->length, &a) ||
        a.utc_ms < INT64_C(946684800000) || a.utc_ms >= INT64_C(4102444800000)) {
        w->ok = false; return NULL;
    }
    time_t seconds = (time_t)(a.utc_ms / 1000); struct tm utc;
    char stamp[32], date[24], id[24];
    if (!gmtime_r(&seconds, &utc) || !strftime(date, sizeof(date), "%Y-%m-%dT%H:%M:%S", &utc)) {
        w->ok = false; return NULL;
    }
    snprintf(stamp, sizeof(stamp), "%s.%03uZ", date, (unsigned)(a.utc_ms % 1000));
    snprintf(id, sizeof(id), "%" PRIu64, a.seq);
    cJSON *o = cJSON_CreateObject();
    str(w, o, "anchor_id", id); num(w, o, "monotonic_ms", a.monotonic_ms);
    str(w, o, "utc_at", stamp); str(w, o, "source", "ntp");
    nullable(w, o, "uncertainty_ms", a.uncertainty_ms == UINT32_MAX ? NAN : (double)a.uncertainty_ms);
    quality(w, o, a.quality, false);
    return o;
}
static cJSON *status(writer_t *w, const sc_queue_item_t *row) {
    sc_status_t s;
    if (!sc_status_decode(row->payload, row->length, &s)) { w->ok = false; return NULL; }
    cJSON *o = cJSON_CreateObject();
    num(w, o, "status_seq", s.seq); num(w, o, "monotonic_ms", s.monotonic_ms);
    num(w, o, "queued_records", s.queued_records);
    num(w, o, "quarantined_count", s.quarantined_records);
    cJSON *loss = object(w, o, "lost_counts");
    num(w, loss, "minute", s.dropped_minutes); num(w, loss, "event", s.dropped_events);
    num(w, loss, "anchor", s.dropped_anchors);
    array(w, o, "lost_ranges");
    bool truncated = s.dropped_minutes || s.dropped_events || s.dropped_anchors || s.corrupt_records;
    boolean(w, o, "ranges_truncated", truncated);
    quality(w, o, 0, truncated);
    return o;
}
static bool identifier(const char *s, size_t capacity) {
    size_t n = strnlen(s, capacity);
    if (!n || n >= capacity || n > 64) return false;
    for (size_t i = 0; i < n; ++i)
        if (!((s[i] >= 'a' && s[i] <= 'z') || (s[i] >= 'A' && s[i] <= 'Z') ||
              (s[i] >= '0' && s[i] <= '9') || strchr("_.:-", s[i]))) return false;
    return true;
}
static bool same_source(const sc_source_t *a, const sc_source_t *b) {
    return !strcmp(a->device_id, b->device_id) && !strcmp(a->boot_id, b->boot_id) &&
           !strcmp(a->measurement_profile_id, b->measurement_profile_id);
}
static bool identity(const sc_queue_item_t *row, uint64_t *seq) {
    switch (row->type) {
    case SC_RECORD_MINUTE: { sc_minute_t m;
        if (!sc_minute_decode(row->payload, row->length, &m)) return false;
        *seq = m.seq; break; }
    case SC_RECORD_EVENT: { sc_event_bucket_t e;
        if (!sc_event_decode(row->payload, row->length, seq, &e)) return false;
        break; }
    case SC_RECORD_ANCHOR: { sc_anchor_t a;
        if (!sc_anchor_decode(row->payload, row->length, &a)) return false;
        *seq = a.seq; break; }
    case SC_RECORD_STATUS: { sc_status_t s;
        if (!sc_status_decode(row->payload, row->length, &s)) return false;
        *seq = s.seq; break; }
    default: return false;
    }
    return *seq <= SAFE_INT;
}
static bool valid_batch(const sc_queue_item_t *rows, size_t count) {
    if (!rows || !count || count > SC_WIRE_MAX_ITEMS) return false;
    unsigned statuses = 0;
    for (size_t i = 0; i < count; ++i) {
        const sc_source_t *s = &rows[i].source; uint64_t seq;
        if (!identifier(s->device_id, sizeof(s->device_id)) ||
            !identifier(s->boot_id, sizeof(s->boot_id)) ||
            !identifier(s->measurement_profile_id, sizeof(s->measurement_profile_id)) ||
            !same_source(s, &rows[0].source) || !identity(&rows[i], &seq)) return false;
        if (rows[i].type == SC_RECORD_STATUS && ++statuses > 1) return false;
        for (size_t j = 0; j < i; ++j) {
            uint64_t prior;
            if (!identity(&rows[j], &prior) || (rows[j].type == rows[i].type && prior == seq)) return false;
        }
    }
    return true;
}
char *sc_wire_batch(const sc_queue_item_t *rows, size_t count) {
    if (!valid_batch(rows, count)) return NULL;
    writer_t w = {.ok = true}; cJSON *root = cJSON_CreateObject();
    num(&w, root, "schema_version", 1);
    str(&w, root, "device_id", rows[0].source.device_id);
    str(&w, root, "boot_id", rows[0].source.boot_id);
    cJSON *records = array(&w, root, "records"), *anchors = array(&w, root, "time_anchors");
    cJSON *device_status = NULL;
    for (size_t i = 0; i < count; ++i) {
        switch (rows[i].type) {
        case SC_RECORD_MINUTE: item(&w, records, minute(&w, &rows[i])); break;
        case SC_RECORD_EVENT: item(&w, records, event(&w, &rows[i])); break;
        case SC_RECORD_ANCHOR: item(&w, anchors, anchor(&w, &rows[i])); break;
        case SC_RECORD_STATUS: device_status = status(&w, &rows[i]); break;
        default: w.ok = false;
        }
    }
    if (!device_status) device_status = cJSON_CreateNull();
    if (!cJSON_AddItemToObject(root, "device_status", device_status)) {
        cJSON_Delete(device_status); w.ok = false;
    }
    char *result = w.ok ? cJSON_PrintUnformatted(root) : NULL;
    cJSON_Delete(root);
    if (result && strlen(result) > SC_WIRE_MAX_BYTES) { free(result); result = NULL; }
    return result;
}

static bool text_is(const cJSON *object, const char *key, const char *expected) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsString(v) && !strcmp(v->valuestring, expected);
}
static bool number_is(const cJSON *object, const char *key, uint64_t expected) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsNumber(v) && v->valuedouble == (double)expected;
}
static bool acknowledged(const cJSON *object) {
    return text_is(object, "status", "accepted") || text_is(object, "status", "duplicate");
}
/* Bounds parsing depth before cJSON recursion and rejects trailing text afterwards. */
static bool shallow_json(const char *text) {
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
bool sc_wire_ack(const char *response, const sc_queue_item_t *rows, size_t count) {
    if (!response || strnlen(response, SC_WIRE_MAX_BYTES + 1) > SC_WIRE_MAX_BYTES ||
        !valid_batch(rows, count) || !shallow_json(response)) return false;
    cJSON *root = cJSON_ParseWithOpts(response, NULL, true);
    if (!root) return false;
    bool ok = cJSON_IsObject(root) && unique_keys(root) && cJSON_GetArraySize(root) == 7 &&
              number_is(root, "schema_version", 1) &&
              text_is(root, "device_id", rows[0].source.device_id) &&
              text_is(root, "boot_id", rows[0].source.boot_id) &&
              cJSON_IsString(cJSON_GetObjectItemCaseSensitive(root, "received_at"));
    const cJSON *records = cJSON_GetObjectItemCaseSensitive(root, "records");
    const cJSON *anchors = cJSON_GetObjectItemCaseSensitive(root, "time_anchors");
    const cJSON *s = cJSON_GetObjectItemCaseSensitive(root, "device_status");
    size_t record_count = 0, anchor_count = 0, status_count = 0;
    ok = ok && cJSON_IsArray(records) && cJSON_IsArray(anchors);
    for (size_t i = 0; ok && i < count; ++i) {
        uint64_t seq = 0; identity(&rows[i], &seq); unsigned matches = 0;
        if (rows[i].type == SC_RECORD_STATUS) {
            ++status_count;
            ok = cJSON_IsObject(s) && cJSON_GetArraySize(s) == 2 &&
                 number_is(s, "status_seq", seq) && acknowledged(s);
        } else {
            bool is_anchor = rows[i].type == SC_RECORD_ANCHOR;
            const cJSON *array = is_anchor ? anchors : records;
            if (is_anchor) ++anchor_count; else ++record_count;
            char id[24]; snprintf(id, sizeof(id), "%" PRIu64, seq);
            for (const cJSON *a = array->child; a; a = a->next) {
                if (!cJSON_IsObject(a) || !acknowledged(a)) continue;
                if (is_anchor) {
                    if (cJSON_GetArraySize(a) == 2 && text_is(a, "anchor_id", id)) ++matches;
                } else if (cJSON_GetArraySize(a) == 3 && number_is(a, "seq", seq) &&
                           text_is(a, "record_type", rows[i].type == SC_RECORD_MINUTE ? "minute" : "event")) {
                    ++matches;
                }
            }
            ok = matches == 1;
        }
    }
    ok = ok && (size_t)cJSON_GetArraySize(records) == record_count &&
         (size_t)cJSON_GetArraySize(anchors) == anchor_count && (status_count || cJSON_IsNull(s));
    cJSON_Delete(root); return ok;
}
