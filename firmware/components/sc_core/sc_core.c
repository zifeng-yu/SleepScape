#include "sc_core.h"

#include <math.h>
#include <string.h>

void sc_pcm_reset(sc_pcm_t *pcm) { memset(pcm, 0, sizeof(*pcm)); }

bool sc_pcm_add(sc_pcm_t *pcm, const int16_t *samples, size_t count) {
    if ((!samples && count) || count > SC_SAMPLE_RATE - pcm->observed_samples) {
        pcm->invalid = true;
        return false;
    }
    for (size_t i = 0; i < count; ++i) {
        int64_t value = samples[i];
        pcm->squares += (uint64_t)(value * value);
        if (value == INT16_MIN || value == INT16_MAX) ++pcm->clipped_samples;
    }
    pcm->observed_samples += (uint32_t)count;
    return true;
}

void sc_pcm_gap(sc_pcm_t *pcm, uint32_t count) {
    pcm->invalid = true;
    if (UINT32_MAX - pcm->dma_overflow_count < count)
        pcm->dma_overflow_count = UINT32_MAX;
    else pcm->dma_overflow_count += count;
}

sc_audio_second_t sc_pcm_finish(const sc_pcm_t *pcm) {
    sc_audio_second_t result = {0};
    result.dbfs = NAN;
    result.observed_samples = pcm->observed_samples;
    result.valid_samples = pcm->observed_samples;
    result.clipped_samples = pcm->clipped_samples;
    result.dma_overflow_count = pcm->dma_overflow_count;
    result.complete = !pcm->invalid && pcm->observed_samples == SC_SAMPLE_RATE;
    if (!pcm->observed_samples) result.quality |= SC_Q_MISSING;
    if (!result.complete) result.quality |= SC_Q_PARTIAL;
    if (pcm->invalid) result.quality |= SC_Q_INVALID;
    if (pcm->dma_overflow_count) result.quality |= SC_Q_DMA_OVERFLOW;
    if (pcm->clipped_samples) result.quality |= SC_Q_CLIPPED;
    if (result.complete) {
        result.energy = (double)pcm->squares / SC_SAMPLE_RATE / 1073741824.0;
        if (result.energy > 0) result.dbfs = (float)(10.0 * log10(result.energy));
        else result.quality |= SC_Q_NUMERIC_FLOOR;
    }
    return result;
}

void sc_scalar_reset(sc_scalar_t *value, uint16_t expected) {
    memset(value, 0, sizeof(*value));
    value->expected_count = expected;
    value->min = value->max = NAN;
}

bool sc_scalar_add(sc_scalar_t *value, bool observed, bool valid, float sample) {
    if (!observed) { value->quality |= SC_Q_MISSING; return true; }
    if (value->observed_count >= value->expected_count) return false;
    ++value->observed_count;
    if (!valid || !isfinite(sample)) { value->quality |= SC_Q_INVALID; return true; }
    value->sum += sample;
    if (!value->valid_count || sample < value->min) value->min = sample;
    if (!value->valid_count || sample > value->max) value->max = sample;
    ++value->valid_count;
    return true;
}

float sc_scalar_mean(const sc_scalar_t *value) {
    return value->valid_count ? (float)(value->sum / value->valid_count) : NAN;
}

void sc_events_reset(sc_events_t *events) {
    memset(events, 0, sizeof(*events));
    events->next_event_id = 1;
    events->frozen_baseline = NAN;
}

static float median(const sc_events_t *events) {
    float values[SC_HISTORY_SECONDS];
    memcpy(values, events->history, sizeof(values));
    /* Sixty values, fixed stack and deterministic work; no dynamic allocation. */
    for (size_t i = 1; i < SC_HISTORY_SECONDS; ++i) {
        float value = values[i];
        size_t j = i;
        while (j && values[j - 1] > value) { values[j] = values[j - 1]; --j; }
        values[j] = value;
    }
    return (values[29] + values[30]) / 2.0f;
}

static void discard_unconfirmed_quiet_tail(sc_events_t *events) {
    for (uint8_t i = events->pending_count; i > 0; --i) {
        sc_event_second_t *second = &events->pending[i - 1];
        if (second->event) break;
        second->event_id = 0;
    }
}

static void interrupt_events(sc_events_t *events) {
    discard_unconfirmed_quiet_tail(events);
    for (uint8_t i = 0; i < events->pending_count; ++i)
        if (events->pending[i].event) events->pending[i].incomplete = true;
    events->active = false;
    events->quiet_seconds = 0;
    events->history_count = events->history_next = 0;
    events->interrupted = true;
}

static bool pop_pending(sc_events_t *events, sc_event_second_t *out) {
    if (!events->pending_count) return false;
    *out = events->pending[0];
    out->event_continues = out->event_id != 0 && events->pending_count > 1 &&
        events->pending[1].event_id == out->event_id;
    --events->pending_count;
    memmove(events->pending, events->pending + 1,
            events->pending_count * sizeof(events->pending[0]));
    return true;
}

bool sc_events_step(sc_events_t *events, uint64_t start,
                    const sc_audio_second_t *sound, sc_event_second_t *out) {
    sc_event_second_t current = {0};
    current.mono_start_ms = start;
    current.dbfs = sound->dbfs;
    current.baseline_dbfs = NAN;
    if (events->has_last && start != events->last_second_ms + 1000)
        interrupt_events(events);
    events->last_second_ms = start;
    events->has_last = true;
    if (!sound->complete || !isfinite(sound->dbfs)) {
        interrupt_events(events);
    } else {
        current.detector_ready = events->history_count == SC_HISTORY_SECONDS;
        if (current.detector_ready) {
            if (!events->active) {
                float baseline = median(events);
                if (sound->dbfs >= baseline + 10.0f) {
                    events->active = true;
                    current.event_started = true;
                    events->active_event_id = events->next_event_id++;
                    events->frozen_baseline = baseline;
                    events->quiet_seconds = 0;
                }
            }
            if (events->active) {
                current.event = true;
                current.event_id = events->active_event_id;
                current.baseline_dbfs = events->frozen_baseline;
                if (sound->dbfs < events->frozen_baseline + 6.0f) {
                    ++events->quiet_seconds;
                    /* A short quiet gap keeps the logical event ID but is not
                     * sound activity. The three-second pending buffer decides
                     * whether that ID really continues over a minute edge. */
                    current.event = false;
                } else events->quiet_seconds = 0;
                if (events->quiet_seconds == 3) {
                    current.event = false;
                    current.event_id = 0;
                    /* The two earlier quiet seconds are still pending. */
                    for (uint8_t n = 0; n < 2 && n < events->pending_count; ++n) {
                        sc_event_second_t *prior =
                            &events->pending[events->pending_count - 1 - n];
                        if (prior->event_id == events->active_event_id) {
                            prior->event = false;
                            prior->event_id = 0;
                        }
                    }
                    events->active = false;
                    events->quiet_seconds = 0;
                }
            }
        }
        events->history[events->history_next] = sound->dbfs;
        events->history_next = (uint8_t)((events->history_next + 1) % SC_HISTORY_SECONDS);
        if (events->history_count < SC_HISTORY_SECONDS) ++events->history_count;
        if (events->history_count == SC_HISTORY_SECONDS) events->interrupted = false;
    }
    events->pending[events->pending_count++] = current;
    return events->pending_count > 3 ? pop_pending(events, out) : false;
}

bool sc_events_flush(sc_events_t *events, sc_event_second_t *out) {
    discard_unconfirmed_quiet_tail(events);
    for (uint8_t i = 0; i < events->pending_count; ++i)
        if (events->pending[i].event) events->pending[i].incomplete = true;
    events->active = false;
    return pop_pending(events, out);
}

sc_detector_state_t sc_events_state(const sc_events_t *events) {
    if (events->history_count == SC_HISTORY_SECONDS) return SC_DETECTOR_READY;
    return events->interrupted ? SC_DETECTOR_INTERRUPTED : SC_DETECTOR_WARMING;
}

void sc_event_bucket_reset(sc_event_bucket_t *bucket) {
    memset(bucket, 0, sizeof(*bucket));
    bucket->max_1s_rms_dbfs = bucket->baseline_dbfs = NAN;
}

void sc_event_bucket_add(sc_event_bucket_t *bucket, const sc_event_second_t *second) {
    if (second->detector_ready) bucket->valid_duration_ms += 1000;
    if (!second->event) {
        if (bucket->has_event && second->event_id == bucket->last_event_id &&
            second->mono_start_ms % SC_WINDOW_MS == SC_WINDOW_MS - 1000)
            bucket->continues_next = second->event_continues;
        return;
    }
    if (!bucket->has_event) {
        bucket->mono_start_ms = second->mono_start_ms;
        bucket->first_event_id = bucket->last_event_id = second->event_id;
        bucket->detected_count = bucket->stored_count = 1;
        bucket->max_1s_rms_dbfs = second->dbfs;
        bucket->baseline_dbfs = second->baseline_dbfs;
        bucket->has_event = true;
        bucket->continued_from_previous = !second->event_started;
    } else if (second->event_id != bucket->last_event_id) {
        ++bucket->detected_count;
        ++bucket->merged_count;
        bucket->last_event_id = second->event_id;
        bucket->quality |= SC_Q_MERGED;
        if (bucket->baseline_dbfs != second->baseline_dbfs) bucket->baseline_dbfs = NAN;
    }
    bucket->mono_end_ms = second->mono_start_ms + 1000;
    bucket->active_duration_ms += 1000;
    bucket->continues_next = second->event_continues &&
        second->mono_start_ms % SC_WINDOW_MS == SC_WINDOW_MS - 1000;
    if (second->dbfs > bucket->max_1s_rms_dbfs) bucket->max_1s_rms_dbfs = second->dbfs;
    if (second->incomplete) bucket->quality |= SC_Q_INCOMPLETE;
}

void sc_minute_reset(sc_minute_t *minute, uint64_t seq, uint64_t start) {
    memset(minute, 0, sizeof(*minute));
    minute->seq = seq;
    minute->mono_start_ms = start;
    minute->duration_ms = SC_WINDOW_MS;
    sc_scalar_reset(&minute->temperature, 6);
    sc_scalar_reset(&minute->humidity, 6);
    sc_scalar_reset(&minute->light, 60);
    minute->sound_expected_count = 60;
    minute->sound_expected_samples = 60 * SC_SAMPLE_RATE;
    minute->sound_max_dbfs = NAN;
    sc_event_bucket_reset(&minute->events);
}

bool sc_minute_add_sound(sc_minute_t *minute, const sc_audio_second_t *sound) {
    if (minute->sound_observed_samples + sound->observed_samples >
        minute->sound_expected_samples ||
        minute->sound_observed_count + (sound->observed_samples > 0) >
        minute->sound_expected_count) return false;
    minute->sound_observed_count += sound->observed_samples > 0;
    minute->sound_observed_samples += sound->observed_samples;
    minute->sound_valid_samples += sound->valid_samples;
    minute->sound_clipped_samples += sound->clipped_samples;
    minute->sound_dma_overflow_count += sound->dma_overflow_count;
    minute->sound_quality |= sound->quality;
    if (sound->complete) {
        ++minute->sound_valid_count;
        minute->sound_energy_sum += sound->energy;
        if (isfinite(sound->dbfs) &&
            (!isfinite(minute->sound_max_dbfs) || sound->dbfs > minute->sound_max_dbfs))
            minute->sound_max_dbfs = sound->dbfs;
    }
    return true;
}

float sc_minute_sound_dbfs(const sc_minute_t *minute) {
    if (!minute->sound_valid_count || minute->sound_energy_sum <= 0) return NAN;
    return (float)(10.0 * log10(minute->sound_energy_sum / minute->sound_valid_count));
}
