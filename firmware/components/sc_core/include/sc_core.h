#ifndef SC_CORE_H
#define SC_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SC_SAMPLE_RATE 16000u
#define SC_WINDOW_MS 60000u
#define SC_HISTORY_SECONDS 60u
#define SC_RECORD_MAX 256u
#define SC_MINUTE_BYTES 192u
#define SC_EVENT_BYTES 64u
#define SC_ANCHOR_BYTES 48u
#define SC_STATUS_BYTES 64u

/* Flags describe observation quality, never replace a missing value with zero. */
enum {
    SC_Q_MISSING = 1u << 0, SC_Q_PARTIAL = 1u << 1,
    SC_Q_INVALID = 1u << 2, SC_Q_DMA_OVERFLOW = 1u << 3,
    SC_Q_CLIPPED = 1u << 4, SC_Q_NUMERIC_FLOOR = 1u << 5,
    SC_Q_WARMING = 1u << 6, SC_Q_INTERRUPTED = 1u << 7,
    SC_Q_MERGED = 1u << 8, SC_Q_INCOMPLETE = 1u << 9,
    SC_Q_LOW_LIGHT = 1u << 10, SC_Q_OUT_OF_RANGE = 1u << 11,
    SC_Q_BUS_ERROR = 1u << 12
};

typedef struct {
    uint64_t squares;
    uint32_t observed_samples;
    uint32_t clipped_samples;
    uint32_t dma_overflow_count;
    bool invalid;
} sc_pcm_t;

typedef struct {
    double energy;
    float dbfs; /* NAN for missing or exact digital zero; flags distinguish them. */
    uint32_t observed_samples;
    uint32_t valid_samples;
    uint32_t clipped_samples;
    uint32_t dma_overflow_count;
    uint32_t quality;
    bool complete;
} sc_audio_second_t;

void sc_pcm_reset(sc_pcm_t *pcm);
bool sc_pcm_add(sc_pcm_t *pcm, const int16_t *samples, size_t count);
void sc_pcm_gap(sc_pcm_t *pcm, uint32_t overflow_count);
sc_audio_second_t sc_pcm_finish(const sc_pcm_t *pcm);

typedef struct {
    double sum;
    float min, max;
    uint16_t expected_count, observed_count, valid_count;
    uint32_t quality;
} sc_scalar_t;

void sc_scalar_reset(sc_scalar_t *scalar, uint16_t expected_count);
/* observed=false is a missed slot; observed=true, valid=false is a failed read. */
bool sc_scalar_add(sc_scalar_t *scalar, bool observed, bool valid, float value);
float sc_scalar_mean(const sc_scalar_t *scalar);

typedef enum { SC_DETECTOR_WARMING = 0, SC_DETECTOR_READY = 1,
               SC_DETECTOR_INTERRUPTED = 2 } sc_detector_state_t;

/* One final decision may emerge per input second, delayed by at most 3 seconds. */
typedef struct {
    uint64_t mono_start_ms;
    uint64_t event_id;
    float dbfs, baseline_dbfs;
    bool event, detector_ready, incomplete, event_started, event_continues;
} sc_event_second_t;

typedef struct {
    float history[SC_HISTORY_SECONDS];
    uint8_t history_count, history_next, quiet_seconds, pending_count;
    uint64_t next_event_id, active_event_id, last_second_ms;
    bool has_last, active, interrupted;
    float frozen_baseline;
    sc_event_second_t pending[4];
} sc_events_t;

void sc_events_reset(sc_events_t *events);
bool sc_events_step(sc_events_t *events, uint64_t mono_start_ms,
                    const sc_audio_second_t *sound, sc_event_second_t *finalized);
/* End of stream: drains pending decisions, marking any open event incomplete. */
bool sc_events_flush(sc_events_t *events, sc_event_second_t *finalized);
sc_detector_state_t sc_events_state(const sc_events_t *events);

typedef struct {
    uint64_t mono_start_ms, mono_end_ms;
    uint64_t first_event_id, last_event_id;
    uint32_t active_duration_ms, valid_duration_ms;
    uint16_t detected_count, stored_count, merged_count, dropped_count;
    float max_1s_rms_dbfs, baseline_dbfs;
    uint32_t quality;
    bool has_event, continued_from_previous, continues_next;
} sc_event_bucket_t;

void sc_event_bucket_reset(sc_event_bucket_t *bucket);
void sc_event_bucket_add(sc_event_bucket_t *bucket, const sc_event_second_t *second);

typedef struct {
    uint64_t seq, mono_start_ms;
    uint32_t duration_ms, quality;
    sc_scalar_t temperature, humidity, light;
    double sound_energy_sum;
    float sound_max_dbfs;
    uint32_t sound_expected_samples, sound_observed_samples, sound_valid_samples;
    uint32_t sound_clipped_samples, sound_dma_overflow_count;
    uint16_t sound_expected_count, sound_observed_count, sound_valid_count;
    uint32_t sound_quality;
    sc_event_bucket_t events;
    sc_detector_state_t detector_state;
} sc_minute_t;

void sc_minute_reset(sc_minute_t *minute, uint64_t seq, uint64_t start_ms);
bool sc_minute_add_sound(sc_minute_t *minute, const sc_audio_second_t *sound);
float sc_minute_sound_dbfs(const sc_minute_t *minute);

/* Versioned LE payloads. CRC belongs to the queue envelope, not host struct layout. */
typedef enum { SC_RECORD_MINUTE = 1, SC_RECORD_EVENT = 2,
               SC_RECORD_ANCHOR = 3, SC_RECORD_STATUS = 4 } sc_record_type_t;
typedef struct {
    uint64_t seq, monotonic_ms;
    int64_t utc_ms;
    uint32_t uncertainty_ms; /* UINT32_MAX means unknown. */
    uint32_t quality;
} sc_anchor_t;
typedef struct {
    uint64_t seq, monotonic_ms;
    uint64_t dropped_minutes, dropped_events, dropped_anchors;
    uint32_t queued_records, corrupt_records, quarantined_records;
} sc_status_t;

size_t sc_minute_encode(const sc_minute_t *record, uint8_t *out, size_t capacity);
bool sc_minute_decode(const uint8_t *data, size_t length, sc_minute_t *out);
size_t sc_event_encode(uint64_t seq, const sc_event_bucket_t *record,
                       uint8_t *out, size_t capacity);
bool sc_event_decode(const uint8_t *data, size_t length, uint64_t *seq,
                     sc_event_bucket_t *out);
size_t sc_anchor_encode(const sc_anchor_t *record, uint8_t *out, size_t capacity);
bool sc_anchor_decode(const uint8_t *data, size_t length, sc_anchor_t *out);
size_t sc_status_encode(const sc_status_t *record, uint8_t *out, size_t capacity);
bool sc_status_decode(const uint8_t *data, size_t length, sc_status_t *out);
uint32_t sc_crc32(const void *data, size_t length);

#endif
