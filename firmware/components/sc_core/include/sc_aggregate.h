#ifndef SC_AGGREGATE_H
#define SC_AGGREGATE_H
#include "sc_core.h"

typedef void (*sc_minute_emit_fn)(void *context, const sc_minute_t *minute);
typedef struct {
    sc_minute_t minute;
    uint64_t light_slots;
    uint8_t aht_slots;
    bool used;
} sc_aggregate_slot_t;
typedef struct {
    sc_aggregate_slot_t slots[2];
    sc_events_t detector;
    uint64_t next_audio_ms, emitted_through_ms;
    sc_minute_emit_fn emit;
    void *emit_context;
} sc_aggregate_t;

/* Single-threaded owner. callback must copy/encode before returning; no retained pointer. */
void sc_aggregate_init(sc_aggregate_t *aggregate, uint64_t start_ms,
                       sc_minute_emit_fn emit, void *context);
/* Slots are one-second start times, aligned to boot monotonic second boundaries. */
bool sc_aggregate_sensor(sc_aggregate_t *aggregate, uint64_t slot_ms,
                         bool light_valid, float lux, uint32_t light_quality, bool aht_attempt,
                         bool aht_valid, float temperature_c, float humidity_pct);
/* Skipped seconds become missing, never zeros. Duplicate/late slots return false. */
bool sc_aggregate_audio(sc_aggregate_t *aggregate, uint64_t slot_ms,
                        const sc_audio_second_t *sound);
#endif
