#include "sc_aggregate.h"
#include <math.h>
#include <string.h>

static sc_aggregate_slot_t *find_slot(sc_aggregate_t *a, uint64_t start, bool create) {
    if (start < a->emitted_through_ms) return NULL;
    sc_aggregate_slot_t *empty = NULL;
    for (unsigned i = 0; i < 2; ++i) {
        if (a->slots[i].used && a->slots[i].minute.mono_start_ms == start)
            return &a->slots[i];
        if (!a->slots[i].used) empty = &a->slots[i];
    }
    if (!create || !empty) return NULL;
    memset(empty, 0, sizeof(*empty)); empty->used = true;
    sc_minute_reset(&empty->minute, start / SC_WINDOW_MS + 1, start);
    return empty;
}

void sc_aggregate_init(sc_aggregate_t *a, uint64_t start,
                       sc_minute_emit_fn emit, void *context) {
    memset(a, 0, sizeof(*a));
    uint64_t window = start / SC_WINDOW_MS * SC_WINDOW_MS;
    a->next_audio_ms = a->emitted_through_ms = window;
    a->emit = emit; a->emit_context = context; sc_events_reset(&a->detector);
    find_slot(a, window, true);
}

bool sc_aggregate_sensor(sc_aggregate_t *a, uint64_t timestamp,
                         bool light_valid, float lux, uint32_t light_quality, bool aht_attempt,
                         bool aht_valid, float temperature, float humidity) {
    if (!a || timestamp % 1000) return false;
    uint64_t start = timestamp / SC_WINDOW_MS * SC_WINDOW_MS;
    /* Do not overwrite an unfinalized minute if a sensor jumps far ahead of audio. */
    if (start > a->next_audio_ms / SC_WINDOW_MS * SC_WINDOW_MS + SC_WINDOW_MS) return false;
    sc_aggregate_slot_t *slot = find_slot(a, start, true);
    if (!slot) return false;
    unsigned second = (unsigned)((timestamp - start) / 1000);
    uint64_t light_bit = UINT64_C(1) << second;
    bool accepted = false;
    if (!(slot->light_slots & light_bit)) {
        slot->light_slots |= light_bit; accepted = true;
        sc_scalar_t *light = &slot->minute.light;
        uint32_t prior = light->quality;
        sc_scalar_add(light, true,
                      light_valid && isfinite(lux) && lux >= 0 && lux <= 200000, lux);
        /* Settling is an observed attempt, not a sensor malfunction. Preserve
         * earlier real failures if this minute also contains a later warm-up. */
        if (!light_valid && light_quality == SC_Q_WARMING)
            light->quality = (light->quality & ~SC_Q_INVALID) | (prior & SC_Q_INVALID);
        light->quality |= light_quality;
    }
    unsigned aht_index = second / 10;
    uint8_t aht_bit = (uint8_t)(1u << aht_index);
    if (aht_attempt && !(slot->aht_slots & aht_bit)) {
        slot->aht_slots |= aht_bit; accepted = true;
        sc_scalar_add(&slot->minute.temperature, true,
                      aht_valid && isfinite(temperature) && temperature >= -80 && temperature <= 125,
                      temperature);
        sc_scalar_add(&slot->minute.humidity, true,
                      aht_valid && isfinite(humidity) && humidity >= 0 && humidity <= 100,
                      humidity);
    }
    return accepted;
}

static void finish_scalar(sc_scalar_t *s) {
    if (s->observed_count < s->expected_count) s->quality |= SC_Q_MISSING;
    if (s->valid_count < s->expected_count) s->quality |= SC_Q_PARTIAL;
}
static void emit_completed(sc_aggregate_t *a, sc_aggregate_slot_t *slot) {
    sc_minute_t *m = &slot->minute;
    finish_scalar(&m->temperature); finish_scalar(&m->humidity); finish_scalar(&m->light);
    if (m->sound_observed_samples < m->sound_expected_samples) m->sound_quality |= SC_Q_MISSING;
    if (m->sound_valid_count < m->sound_expected_count) m->sound_quality |= SC_Q_PARTIAL;
    if (!m->events.valid_duration_ms && m->detector_state == SC_DETECTOR_READY)
        m->detector_state = SC_DETECTOR_WARMING;
    if (m->detector_state == SC_DETECTOR_WARMING) m->sound_quality |= SC_Q_WARMING;
    if (m->detector_state == SC_DETECTOR_INTERRUPTED) m->sound_quality |= SC_Q_INTERRUPTED;
    if (a->emit) a->emit(a->emit_context, m);
    a->emitted_through_ms = m->mono_start_ms + SC_WINDOW_MS;
    slot->used = false;
}
static bool process_second(sc_aggregate_t *a, uint64_t timestamp,
                           const sc_audio_second_t *sound) {
    uint64_t start = timestamp / SC_WINDOW_MS * SC_WINDOW_MS;
    sc_aggregate_slot_t *slot = find_slot(a, start, true);
    if (!slot || !sc_minute_add_sound(&slot->minute, sound)) return false;
    sc_event_second_t finalized;
    bool has_finalized = sc_events_step(&a->detector, timestamp, sound, &finalized);
    slot->minute.detector_state = sc_events_state(&a->detector);
    if (has_finalized) {
        uint64_t old_start = finalized.mono_start_ms / SC_WINDOW_MS * SC_WINDOW_MS;
        sc_aggregate_slot_t *old = find_slot(a, old_start, false);
        if (!old) return false;
        sc_event_bucket_add(&old->minute.events, &finalized);
        if (finalized.mono_start_ms == old_start + SC_WINDOW_MS - 1000)
            emit_completed(a, old);
    }
    a->next_audio_ms = timestamp + 1000;
    return true;
}

bool sc_aggregate_audio(sc_aggregate_t *a, uint64_t timestamp,
                        const sc_audio_second_t *sound) {
    if (!a || !sound || timestamp % 1000 || timestamp < a->next_audio_ms) return false;
    sc_audio_second_t missing = {0}; missing.dbfs = NAN;
    missing.quality = SC_Q_MISSING | SC_Q_PARTIAL;
    while (a->next_audio_ms < timestamp)
        if (!process_second(a, a->next_audio_ms, &missing)) return false;
    return process_second(a, timestamp, sound);
}
