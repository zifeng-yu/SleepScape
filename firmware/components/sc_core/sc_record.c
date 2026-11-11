#include "sc_core.h"

#include <math.h>
#include <string.h>

/* Explicit little endian format, independent of C padding, host endian and ABI. */
static void put16(uint8_t **p, uint16_t v) { (*p)[0] = v; (*p)[1] = v >> 8; *p += 2; }
static void put32(uint8_t **p, uint32_t v) {
    for (unsigned i = 0; i < 4; ++i) (*p)[i] = (uint8_t)(v >> (i * 8));
    *p += 4;
}
static void put64(uint8_t **p, uint64_t v) {
    for (unsigned i = 0; i < 8; ++i) (*p)[i] = (uint8_t)(v >> (i * 8));
    *p += 8;
}
static uint16_t get16(const uint8_t **p) {
    uint16_t v = (uint16_t)((*p)[0] | (uint16_t)(*p)[1] << 8); *p += 2; return v;
}
static uint32_t get32(const uint8_t **p) {
    uint32_t v = 0; for (unsigned i = 0; i < 4; ++i) v |= (uint32_t)(*p)[i] << (i * 8);
    *p += 4; return v;
}
static uint64_t get64(const uint8_t **p) {
    uint64_t v = 0; for (unsigned i = 0; i < 8; ++i) v |= (uint64_t)(*p)[i] << (i * 8);
    *p += 8; return v;
}
static void putf(uint8_t **p, float v) { uint32_t n; memcpy(&n, &v, 4); put32(p, n); }
static float getf(const uint8_t **p) { uint32_t n = get32(p); float f; memcpy(&f, &n, 4); return f; }
static void putd(uint8_t **p, double v) { uint64_t n; memcpy(&n, &v, 8); put64(p, n); }
static double getd(const uint8_t **p) { uint64_t n = get64(p); double d; memcpy(&d, &n, 8); return d; }

static bool header(uint8_t **p, uint8_t *out, size_t cap, size_t size, uint16_t kind) {
    if (!out || cap < size) return false;
    memset(out, 0, size); *p = out; put16(p, 1); put16(p, kind); return true;
}
static bool check(const uint8_t **p, const uint8_t *data, size_t len,
                  size_t size, uint16_t kind) {
    if (!data || len != size) return false;
    *p = data; return get16(p) == 1 && get16(p) == kind;
}

static void put_scalar(uint8_t **p, const sc_scalar_t *s) {
    putf(p, sc_scalar_mean(s)); putf(p, s->min); putf(p, s->max);
    put16(p, s->expected_count); put16(p, s->observed_count); put16(p, s->valid_count);
    put16(p, (uint16_t)s->quality);
}
static bool get_scalar(const uint8_t **p, sc_scalar_t *s) {
    float mean = getf(p); s->min = getf(p); s->max = getf(p);
    s->expected_count = get16(p); s->observed_count = get16(p); s->valid_count = get16(p);
    s->quality = get16(p); s->sum = s->valid_count ? (double)mean * s->valid_count : 0;
    return s->valid_count <= s->observed_count && s->observed_count <= s->expected_count &&
        (!s->valid_count || (isfinite(mean) && isfinite(s->min) && isfinite(s->max) &&
                            mean >= s->min && mean <= s->max));
}
static uint8_t event_flags(const sc_event_bucket_t *e) {
    return (uint8_t)(e->has_event | e->continued_from_previous << 1 | e->continues_next << 2);
}
static void set_event_flags(sc_event_bucket_t *e, uint8_t f) {
    e->has_event = (f & 1) != 0;
    e->continued_from_previous = (f & 2) != 0; e->continues_next = (f & 4) != 0;
}

size_t sc_minute_encode(const sc_minute_t *m, uint8_t *out, size_t capacity) {
    uint8_t *p;
    if (!m || !header(&p, out, capacity, SC_MINUTE_BYTES, SC_RECORD_MINUTE)) return 0;
    put64(&p, m->seq); put64(&p, m->mono_start_ms);
    put32(&p, m->duration_ms); put32(&p, m->quality);
    put_scalar(&p, &m->temperature); put_scalar(&p, &m->humidity); put_scalar(&p, &m->light);
    putd(&p, m->sound_energy_sum); putf(&p, m->sound_max_dbfs);
    put32(&p, m->sound_expected_samples); put32(&p, m->sound_observed_samples);
    put32(&p, m->sound_valid_samples); put32(&p, m->sound_clipped_samples);
    put32(&p, m->sound_dma_overflow_count);
    put16(&p, m->sound_expected_count); put16(&p, m->sound_observed_count);
    put16(&p, m->sound_valid_count); put16(&p, (uint16_t)m->sound_quality);
    put64(&p, m->events.mono_start_ms); put64(&p, m->events.mono_end_ms);
    put64(&p, m->events.first_event_id); put64(&p, m->events.last_event_id);
    put32(&p, m->events.active_duration_ms); put32(&p, m->events.valid_duration_ms);
    put16(&p, m->events.detected_count); put16(&p, m->events.stored_count);
    put16(&p, m->events.merged_count); put16(&p, m->events.dropped_count);
    putf(&p, m->events.max_1s_rms_dbfs); putf(&p, m->events.baseline_dbfs);
    put32(&p, m->events.quality); *p++ = event_flags(&m->events);
    *p++ = (uint8_t)m->detector_state;
    return (size_t)(p - out) <= SC_MINUTE_BYTES ? SC_MINUTE_BYTES : 0;
}

bool sc_minute_decode(const uint8_t *data, size_t length, sc_minute_t *m) {
    const uint8_t *p;
    if (!m || !check(&p, data, length, SC_MINUTE_BYTES, SC_RECORD_MINUTE)) return false;
    memset(m, 0, sizeof(*m));
    m->seq = get64(&p); m->mono_start_ms = get64(&p);
    m->duration_ms = get32(&p); m->quality = get32(&p);
    if (!get_scalar(&p, &m->temperature) || !get_scalar(&p, &m->humidity) ||
        !get_scalar(&p, &m->light)) return false;
    m->sound_energy_sum = getd(&p); m->sound_max_dbfs = getf(&p);
    m->sound_expected_samples = get32(&p); m->sound_observed_samples = get32(&p);
    m->sound_valid_samples = get32(&p); m->sound_clipped_samples = get32(&p);
    m->sound_dma_overflow_count = get32(&p);
    m->sound_expected_count = get16(&p); m->sound_observed_count = get16(&p);
    m->sound_valid_count = get16(&p); m->sound_quality = get16(&p);
    m->events.mono_start_ms = get64(&p); m->events.mono_end_ms = get64(&p);
    m->events.first_event_id = get64(&p); m->events.last_event_id = get64(&p);
    m->events.active_duration_ms = get32(&p); m->events.valid_duration_ms = get32(&p);
    m->events.detected_count = get16(&p); m->events.stored_count = get16(&p);
    m->events.merged_count = get16(&p); m->events.dropped_count = get16(&p);
    m->events.max_1s_rms_dbfs = getf(&p); m->events.baseline_dbfs = getf(&p);
    m->events.quality = get32(&p); set_event_flags(&m->events, *p++);
    m->detector_state = (sc_detector_state_t)*p++;
    return m->duration_ms > 0 && m->duration_ms <= SC_WINDOW_MS &&
        isfinite(m->sound_energy_sum) && m->sound_energy_sum >= 0 &&
        m->sound_valid_count <= m->sound_observed_count &&
        m->sound_observed_count <= m->sound_expected_count &&
        m->sound_valid_samples <= m->sound_observed_samples &&
        m->sound_observed_samples <= m->sound_expected_samples &&
        m->detector_state <= SC_DETECTOR_INTERRUPTED;
}

size_t sc_event_encode(uint64_t seq, const sc_event_bucket_t *e,
                       uint8_t *out, size_t capacity) {
    uint8_t *p;
    if (!e || !e->has_event || !header(&p, out, capacity, SC_EVENT_BYTES, SC_RECORD_EVENT)) return 0;
    put64(&p, seq); put64(&p, e->mono_start_ms); put64(&p, e->mono_end_ms);
    put32(&p, e->active_duration_ms); put32(&p, e->valid_duration_ms);
    putf(&p, e->max_1s_rms_dbfs); putf(&p, e->baseline_dbfs); put32(&p, e->quality);
    put16(&p, e->detected_count); put16(&p, e->stored_count);
    put16(&p, e->merged_count); put16(&p, e->dropped_count); *p++ = event_flags(e);
    return (size_t)(p - out) <= SC_EVENT_BYTES ? SC_EVENT_BYTES : 0;
}

bool sc_event_decode(const uint8_t *data, size_t length, uint64_t *seq,
                     sc_event_bucket_t *e) {
    const uint8_t *p;
    if (!e || !seq || !check(&p, data, length, SC_EVENT_BYTES, SC_RECORD_EVENT)) return false;
    sc_event_bucket_reset(e);
    *seq = get64(&p); e->mono_start_ms = get64(&p); e->mono_end_ms = get64(&p);
    e->active_duration_ms = get32(&p); e->valid_duration_ms = get32(&p);
    e->max_1s_rms_dbfs = getf(&p); e->baseline_dbfs = getf(&p); e->quality = get32(&p);
    e->detected_count = get16(&p); e->stored_count = get16(&p);
    e->merged_count = get16(&p); e->dropped_count = get16(&p); set_event_flags(e, *p++);
    return e->has_event && e->mono_end_ms > e->mono_start_ms &&
        e->mono_end_ms - e->mono_start_ms <= SC_WINDOW_MS && e->active_duration_ms > 0 &&
        e->active_duration_ms <= e->mono_end_ms - e->mono_start_ms &&
        isfinite(e->max_1s_rms_dbfs) && e->max_1s_rms_dbfs <= 0 &&
        e->detected_count == e->stored_count + e->merged_count + e->dropped_count;
}

size_t sc_anchor_encode(const sc_anchor_t *a, uint8_t *out, size_t capacity) {
    uint8_t *p;
    if (!a || !header(&p, out, capacity, SC_ANCHOR_BYTES, SC_RECORD_ANCHOR)) return 0;
    put64(&p, a->seq); put64(&p, a->monotonic_ms); put64(&p, (uint64_t)a->utc_ms);
    put32(&p, a->uncertainty_ms); put32(&p, a->quality); return SC_ANCHOR_BYTES;
}
bool sc_anchor_decode(const uint8_t *data, size_t length, sc_anchor_t *a) {
    const uint8_t *p;
    if (!a || !check(&p, data, length, SC_ANCHOR_BYTES, SC_RECORD_ANCHOR)) return false;
    a->seq = get64(&p); a->monotonic_ms = get64(&p); a->utc_ms = (int64_t)get64(&p);
    a->uncertainty_ms = get32(&p); a->quality = get32(&p); return true;
}
size_t sc_status_encode(const sc_status_t *s, uint8_t *out, size_t capacity) {
    uint8_t *p;
    if (!s || !header(&p, out, capacity, SC_STATUS_BYTES, SC_RECORD_STATUS)) return 0;
    put64(&p, s->seq); put64(&p, s->monotonic_ms); put64(&p, s->dropped_minutes);
    put64(&p, s->dropped_events); put64(&p, s->dropped_anchors);
    put32(&p, s->queued_records); put32(&p, s->corrupt_records);
    put32(&p, s->quarantined_records); return SC_STATUS_BYTES;
}
bool sc_status_decode(const uint8_t *data, size_t length, sc_status_t *s) {
    const uint8_t *p;
    if (!s || !check(&p, data, length, SC_STATUS_BYTES, SC_RECORD_STATUS)) return false;
    s->seq = get64(&p); s->monotonic_ms = get64(&p); s->dropped_minutes = get64(&p);
    s->dropped_events = get64(&p); s->dropped_anchors = get64(&p);
    s->queued_records = get32(&p); s->corrupt_records = get32(&p);
    s->quarantined_records = get32(&p); return true;
}

uint32_t sc_crc32(const void *data, size_t length) {
    const uint8_t *p = data;
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < length; ++i) {
        crc ^= p[i];
        for (unsigned b = 0; b < 8; ++b)
            crc = (crc >> 1) ^ (0xedb88320u & (uint32_t)-(int32_t)(crc & 1));
    }
    return ~crc;
}
