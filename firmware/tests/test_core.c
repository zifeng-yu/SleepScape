#include "sc_core.h"
#include "sc_queue.h"
#include "sc_aggregate.h"

#include <assert.h>
#include <dirent.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Only the host queue object redirects readdir to this fault injector. */
static int directory_reads_before_error = -1;
struct dirent *sc_test_readdir(DIR *directory) {
    if (directory_reads_before_error == 0) { errno = EIO; return NULL; }
    if (directory_reads_before_error > 0) --directory_reads_before_error;
    return readdir(directory);
}

static unsigned tests;
#define RUN(test) do { test(); ++tests; printf("ok %u - %s\n", tests, #test); } while (0)
#define NEAR(a,b,t) assert(fabs((double)(a) - (double)(b)) < (t))

static sc_audio_second_t level(float dbfs) {
    sc_audio_second_t s = {0};
    s.complete = true; s.dbfs = dbfs; s.energy = pow(10, dbfs / 10.0);
    s.observed_samples = s.valid_samples = SC_SAMPLE_RATE;
    return s;
}
static void test_pcm_chunked_rms_and_clipping(void) {
    sc_pcm_t pcm; sc_pcm_reset(&pcm);
    int16_t samples[128];
    for (unsigned i = 0; i < 128; ++i) samples[i] = i % 2 ? -16384 : 16384;
    for (unsigned i = 0; i < SC_SAMPLE_RATE / 128; ++i) assert(sc_pcm_add(&pcm, samples, 128));
    sc_audio_second_t s = sc_pcm_finish(&pcm);
    assert(s.complete && s.valid_samples == SC_SAMPLE_RATE); NEAR(s.energy, .25, 1e-12);
    NEAR(s.dbfs, -6.020599913, 1e-5); assert(!s.clipped_samples);
    sc_pcm_reset(&pcm);
    samples[0] = INT16_MIN; samples[1] = INT16_MAX;
    assert(sc_pcm_add(&pcm, samples, 2));
    s = sc_pcm_finish(&pcm); assert(!s.complete && s.clipped_samples == 2);
    assert((s.quality & (SC_Q_CLIPPED | SC_Q_PARTIAL)) == (SC_Q_CLIPPED | SC_Q_PARTIAL));
}
static void test_pcm_silence_missing_and_overflow(void) {
    sc_pcm_t pcm; sc_pcm_reset(&pcm); sc_audio_second_t empty = sc_pcm_finish(&pcm);
    assert(!empty.complete && isnan(empty.dbfs) && (empty.quality & SC_Q_MISSING));
    int16_t zero[160] = {0};
    for (unsigned i = 0; i < 100; ++i) assert(sc_pcm_add(&pcm, zero, 160));
    sc_audio_second_t silence = sc_pcm_finish(&pcm);
    assert(silence.complete && silence.energy == 0 && isnan(silence.dbfs));
    assert((silence.quality & SC_Q_NUMERIC_FLOOR) && !(silence.quality & SC_Q_MISSING));
    assert(!sc_pcm_add(&pcm, zero, 1)); assert(!sc_pcm_finish(&pcm).complete);
    sc_pcm_reset(&pcm); assert(sc_pcm_add(&pcm, zero, 160)); sc_pcm_gap(&pcm, 1);
    sc_audio_second_t gap = sc_pcm_finish(&pcm);
    assert(gap.observed_samples == 160 && !gap.complete && gap.dma_overflow_count == 1);
}
static void test_scalar_missing_is_not_zero(void) {
    sc_scalar_t s; sc_scalar_reset(&s, 6); assert(isnan(sc_scalar_mean(&s)));
    assert(sc_scalar_add(&s, false, false, 999));
    assert(sc_scalar_add(&s, true, false, 999));
    assert(sc_scalar_add(&s, true, true, 0));
    assert(sc_scalar_add(&s, true, true, 6));
    assert(sc_scalar_add(&s, true, true, NAN));
    assert(s.observed_count == 4 && s.valid_count == 2);
    assert(s.min == 0 && s.max == 6 && sc_scalar_mean(&s) == 3);
}
static void test_minute_averages_linear_energy(void) {
    sc_minute_t m; sc_minute_reset(&m, 3, 180000);
    sc_audio_second_t quiet = level(-40), loud = level(-20);
    assert(sc_minute_add_sound(&m, &quiet)); assert(sc_minute_add_sound(&m, &loud));
    NEAR(sc_minute_sound_dbfs(&m), 10 * log10((.0001 + .01) / 2), 1e-5);
    assert(fabs(sc_minute_sound_dbfs(&m) - -30) > 5);
    assert(m.sound_valid_count == 2 && m.sound_max_dbfs == -20);
    sc_audio_second_t lost = {0}; lost.dbfs = NAN; lost.quality = SC_Q_MISSING;
    assert(sc_minute_add_sound(&m, &lost)); assert(m.sound_valid_count == 2);
}
static void test_events_warmup_hysteresis_and_delay(void) {
    sc_events_t detector; sc_events_reset(&detector);
    sc_event_bucket_t bucket; sc_event_bucket_reset(&bucket);
    sc_event_second_t out; sc_audio_second_t s = level(-50);
    for (unsigned i = 0; i < 60; ++i)
        if (sc_events_step(&detector, i * 1000, &s, &out)) sc_event_bucket_add(&bucket, &out);
    assert(!bucket.has_event && sc_events_state(&detector) == SC_DETECTOR_READY);
    s = level(-35); assert(sc_events_step(&detector, 60000, &s, &out));
    assert(out.mono_start_ms == 57000 && !out.event);
    s = level(-50);
    for (unsigned i = 61; i < 67; ++i)
        if (sc_events_step(&detector, i * 1000, &s, &out)) sc_event_bucket_add(&bucket, &out);
    assert(bucket.has_event && bucket.mono_start_ms == 60000 && bucket.mono_end_ms == 61000);
    assert(bucket.active_duration_ms == 1000 && bucket.detected_count == 1);
    assert(bucket.stored_count == 1 && bucket.merged_count == 0);
    NEAR(bucket.baseline_dbfs, -50, 1e-5);
}
static void test_events_merge_counts_and_active_duration(void) {
    sc_events_t detector; sc_events_reset(&detector);
    sc_event_bucket_t bucket; sc_event_bucket_reset(&bucket);
    sc_event_second_t out;
    for (unsigned i = 0; i < 80; ++i) {
        sc_audio_second_t s = level(i == 60 || i == 67 ? -30 : -50);
        if (sc_events_step(&detector, i * 1000, &s, &out) && out.mono_start_ms >= 60000)
            sc_event_bucket_add(&bucket, &out);
    }
    assert(bucket.detected_count == 2 && bucket.stored_count == 1 && bucket.merged_count == 1);
    assert(bucket.active_duration_ms == 2000 && bucket.mono_end_ms - bucket.mono_start_ms == 8000);
    assert(bucket.quality & SC_Q_MERGED);
}
static void test_events_missing_rewarms_and_marks_incomplete(void) {
    sc_events_t detector; sc_events_reset(&detector);
    sc_event_second_t out; sc_event_bucket_t bucket; sc_event_bucket_reset(&bucket);
    for (unsigned i = 0; i < 61; ++i) {
        sc_audio_second_t s = level(i == 60 ? -30 : -50);
        if (sc_events_step(&detector, i * 1000, &s, &out)) sc_event_bucket_add(&bucket, &out);
    }
    sc_audio_second_t missing = {0}; missing.dbfs = NAN;
    sc_events_step(&detector, 61000, &missing, &out);
    assert(sc_events_state(&detector) == SC_DETECTOR_INTERRUPTED);
    for (unsigned i = 62; i < 122; ++i) {
        sc_audio_second_t s = level(-50);
        if (sc_events_step(&detector, i * 1000, &s, &out)) sc_event_bucket_add(&bucket, &out);
    }
    assert(bucket.has_event && (bucket.quality & SC_Q_INCOMPLETE));
    assert(sc_events_state(&detector) == SC_DETECTOR_READY);
}
static void test_events_cross_minute_continuation(void) {
    sc_events_t detector; sc_events_reset(&detector); sc_event_second_t out;
    sc_event_bucket_t before, after; sc_event_bucket_reset(&before); sc_event_bucket_reset(&after);
    for (unsigned i = 0; i < 130; ++i) {
        sc_audio_second_t s = level(i >= 118 && i <= 121 ? -30 : -50);
        if (!sc_events_step(&detector, i * 1000, &s, &out)) continue;
        if (out.mono_start_ms >= 60000 && out.mono_start_ms < 120000)
            sc_event_bucket_add(&before, &out);
        if (out.mono_start_ms >= 120000) sc_event_bucket_add(&after, &out);
    }
    assert(before.active_duration_ms == 2000 && before.continues_next);
    assert(after.active_duration_ms == 2000 && after.continued_from_previous);
    assert(!after.continues_next && before.last_event_id == after.first_event_id);
}
static void test_codecs_and_crc(void) {
    assert(sc_crc32("123456789", 9) == 0xcbf43926u);
    uint8_t bytes[SC_RECORD_MAX]; sc_minute_t a, b; sc_minute_reset(&a, 42, 900000);
    sc_scalar_add(&a.temperature, true, true, 21.5f);
    sc_scalar_add(&a.humidity, true, true, 45); sc_scalar_add(&a.light, true, true, 0);
    sc_audio_second_t s = level(-50); sc_minute_add_sound(&a, &s);
    assert(sc_minute_encode(&a, bytes, sizeof(bytes)) == SC_MINUTE_BYTES);
    assert(sc_minute_decode(bytes, SC_MINUTE_BYTES, &b)); assert(b.seq == 42);
    assert(b.mono_start_ms == 900000 && sc_scalar_mean(&b.temperature) == 21.5f);
    NEAR(sc_minute_sound_dbfs(&b), -50, 1e-5);
    assert(!sc_minute_decode(bytes, SC_MINUTE_BYTES - 1, &b));
    bytes[0] = 7; assert(!sc_minute_decode(bytes, SC_MINUTE_BYTES, &b));
    sc_event_bucket_t e, restored; sc_event_bucket_reset(&e);
    sc_event_second_t event = {.mono_start_ms = 60000, .event_id = 1,
        .dbfs = -30, .baseline_dbfs = -50, .event = true, .detector_ready = true,
        .event_started = true};
    sc_event_bucket_add(&e, &event); uint64_t seq;
    assert(sc_event_encode(4, &e, bytes, sizeof(bytes)) == SC_EVENT_BYTES);
    assert(sc_event_decode(bytes, SC_EVENT_BYTES, &seq, &restored));
    assert(seq == 4 && restored.active_duration_ms == 1000);
    sc_anchor_t anchor = {.seq=7,.monotonic_ms=65000,.utc_ms=1767225600000LL,
        .uncertainty_ms=UINT32_MAX}, anchor2;
    assert(sc_anchor_encode(&anchor, bytes, sizeof(bytes)) == SC_ANCHOR_BYTES);
    assert(sc_anchor_decode(bytes, SC_ANCHOR_BYTES, &anchor2));
    assert(anchor2.utc_ms == anchor.utc_ms && anchor2.uncertainty_ms == UINT32_MAX);
    sc_status_t status = {.seq=1,.dropped_events=7,.queued_records=4,.quarantined_records=12}, status2;
    assert(sc_status_encode(&status, bytes, sizeof(bytes)) == SC_STATUS_BYTES);
    assert(sc_status_decode(bytes, SC_STATUS_BYTES, &status2));
    assert(status2.dropped_events == 7 && status2.queued_records == 4 && status2.quarantined_records == 12);
}

/* Aggregate fixtures are synthetic. These buffers are host-only, not ESP RAM. */
typedef struct { sc_minute_t minutes[12]; unsigned count; } collected_t;
static void collect(void *context, const sc_minute_t *minute) {
    collected_t *out = context;
    assert(out->count < 12); out->minutes[out->count++] = *minute;
}
static void test_aggregate_delays_and_preserves_sensor_channels(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    sc_audio_second_t s = level(-50);
    for (unsigned i = 0; i < 63; ++i) {
        assert(sc_aggregate_sensor(&a, i * 1000, true, (float)i, i % 10 == 0,
                                   true, 20 + (float)i / 10, 50));
        assert(sc_aggregate_audio(&a, i * 1000, &s));
        assert(out.count == (i >= 62 ? 1u : 0u));
    }
    sc_minute_t *m = &out.minutes[0];
    assert(m->seq == 1 && m->mono_start_ms == 0 && m->duration_ms == 60000);
    assert(m->temperature.valid_count == 6 && m->light.valid_count == 60);
    NEAR(sc_scalar_mean(&m->temperature), 22.5, 1e-6);
    NEAR(sc_scalar_mean(&m->light), 29.5, 1e-6);
    assert(m->sound_valid_count == 60 && m->sound_valid_samples == 960000);
    assert(!m->events.valid_duration_ms && m->detector_state == SC_DETECTOR_WARMING);
    assert(!m->temperature.quality && !m->light.quality);
    assert((m->sound_quality & SC_Q_WARMING) && !(m->sound_quality & SC_Q_MISSING));
}
static void test_aggregate_startup_gap_duplicates_and_late_data(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 5200, collect, &out);
    sc_audio_second_t s = level(-50);
    for (unsigned i = 6; i < 63; ++i) {
        assert(sc_aggregate_sensor(&a, i * 1000, true, 0, i % 10 == 0, true, 20, 50));
        assert(!sc_aggregate_sensor(&a, i * 1000, true, 999, i % 10 == 0, true, 99, 99));
        assert(sc_aggregate_audio(&a, i * 1000, &s));
        assert(!sc_aggregate_audio(&a, i * 1000, &s));
    }
    assert(out.count == 1);
    sc_minute_t *m = &out.minutes[0];
    assert(m->sound_valid_count == 54 && m->light.valid_count == 54);
    assert(m->temperature.valid_count == 5);
    assert(sc_scalar_mean(&m->light) == 0 && sc_scalar_mean(&m->temperature) == 20);
    assert((m->sound_quality & SC_Q_MISSING) && (m->light.quality & SC_Q_MISSING));
    assert(!sc_aggregate_sensor(&a, 59000, true, 10, true, true, 20, 50));
    assert(!sc_aggregate_audio(&a, 59000, &s));
    assert(!sc_aggregate_audio(&a, 63001, &s));
}
static void test_aggregate_audio_outage_emits_missing_minutes(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    sc_audio_second_t s = level(-50);
    assert(sc_aggregate_audio(&a, 0, &s));
    assert(sc_aggregate_audio(&a, 182000, &s));
    assert(out.count == 3);
    assert(out.minutes[0].sound_valid_count == 1);
    for (unsigned i = 0; i < 3; ++i) {
        sc_minute_t *m = &out.minutes[i];
        assert(m->seq == i + 1 && m->mono_start_ms == i * 60000);
        assert(isnan(sc_scalar_mean(&m->temperature)) && !m->temperature.valid_count);
        assert(m->sound_quality & SC_Q_MISSING);
        assert(m->detector_state == SC_DETECTOR_INTERRUPTED);
    }
    assert(isnan(sc_minute_sound_dbfs(&out.minutes[1])));
    assert(!out.minutes[1].sound_observed_samples);
}
static void test_aggregate_events_cross_minute_finalization(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    for (unsigned i = 0; i < 183; ++i) {
        sc_audio_second_t s = level(i >= 118 && i <= 121 ? -30 : -50);
        assert(sc_aggregate_audio(&a, i * 1000, &s));
    }
    assert(out.count == 3);
    sc_event_bucket_t *before = &out.minutes[1].events, *after = &out.minutes[2].events;
    assert(before->active_duration_ms == 2000 && before->continues_next);
    assert(after->active_duration_ms == 2000 && after->continued_from_previous);
    assert(before->valid_duration_ms == 60000 && after->valid_duration_ms == 60000);
    assert(before->detected_count == 1 && after->detected_count == 1);
    assert(after->mono_end_ms == 122000 && !after->continues_next);
}
static void test_aggregate_short_quiet_gap_excluded_from_activity(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    for (unsigned i = 0; i < 183; ++i) {
        /* A two-second quiet gap spans the minute edge. It stays one logical
         * event but contributes no active time to either minute. */
        sc_audio_second_t s = level(i == 118 || i == 121 ? -30 : -50);
        assert(sc_aggregate_audio(&a, i * 1000, &s));
    }
    sc_event_bucket_t *before = &out.minutes[1].events, *after = &out.minutes[2].events;
    assert(before->active_duration_ms == 1000 && after->active_duration_ms == 1000);
    assert(before->detected_count == 1 && after->detected_count == 1);
    assert(before->continues_next && after->continued_from_previous);
    assert(before->last_event_id == after->first_event_id);
    assert(before->mono_end_ms == 119000 && after->mono_start_ms == 121000);
    sc_aggregate_init(&a, 0, collect, &out); out.count = 0;
    for (unsigned i = 0; i < 123; ++i) {
        sc_audio_second_t s = level(i == 70 || i == 72 || i == 75 ? -30 : -50);
        assert(sc_aggregate_audio(&a, i * 1000, &s));
    }
    const sc_event_bucket_t *e = &out.minutes[1].events;
    assert(e->detected_count == 1 && e->merged_count == 0);
    assert(e->active_duration_ms == 3000 && e->mono_end_ms - e->mono_start_ms == 6000);
}

static void test_aggregate_interruption_does_not_invent_continuation(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    for (unsigned i = 0; i < 183; ++i) {
        sc_audio_second_t s = level(i == 119 ? -30 : -50);
        if (i == 121) { memset(&s, 0, sizeof(s)); s.dbfs = NAN; s.quality = SC_Q_MISSING; }
        assert(sc_aggregate_audio(&a, i * 1000, &s));
    }
    const sc_event_bucket_t *e = &out.minutes[1].events;
    assert(e->active_duration_ms == 1000 && !e->continues_next);
    assert(e->quality & SC_Q_INCOMPLETE);
    assert(!out.minutes[2].events.has_event);
}

static void test_aggregate_independent_sensor_failure(void) {
    sc_aggregate_t a; collected_t out = {0}; sc_aggregate_init(&a, 0, collect, &out);
    sc_audio_second_t s = level(-50);
    for (unsigned i = 0; i < 63; ++i) {
        assert(sc_aggregate_sensor(&a, i * 1000, i != 4, i == 5 ? NAN : 0,
                                   i % 10 == 0, i != 20, 21, i == 10 ? 101 : 50));
        assert(sc_aggregate_audio(&a, i * 1000, &s));
    }
    const sc_minute_t *m = &out.minutes[0];
    assert(m->light.observed_count == 60 && m->light.valid_count == 58);
    assert(m->temperature.observed_count == 6 && m->temperature.valid_count == 5);
    assert(m->humidity.observed_count == 6 && m->humidity.valid_count == 4);
    assert(m->light.quality & SC_Q_INVALID);
    assert(!(m->light.quality & SC_Q_MISSING));
}

static char temporary[128], prefix[160];
static sc_source_t source = {.device_id="synthetic-device", .boot_id="synthetic-boot",
    .measurement_profile_id="synthetic-profile"};
static void setup(void) {
    strcpy(temporary, "/tmp/sleepscape-core-test-XXXXXX");
    assert(mkdtemp(temporary)); snprintf(prefix, sizeof(prefix), "%s/queue", temporary);
}
static void cleanup(void) {
    DIR *d = opendir(temporary); assert(d); struct dirent *entry;
    while ((entry = readdir(d))) {
        if (entry->d_name[0] == '.') continue;
        char path[512]; snprintf(path, sizeof(path), "%s/%s", temporary, entry->d_name);
        assert(!remove(path));
    }
    closedir(d); assert(!rmdir(temporary));
}
static uint64_t append_minute(sc_queue_t *q, uint64_t seq) {
    sc_minute_t m; sc_minute_reset(&m, seq, seq * SC_WINDOW_MS);
    uint8_t data[SC_MINUTE_BYTES]; uint64_t id;
    assert(sc_minute_encode(&m, data, sizeof(data)) == sizeof(data));
    assert(sc_queue_append(q, SC_RECORD_MINUTE, data, sizeof(data), &id)); return id;
}
static void test_queue_recovery_and_exact_ack(void) {
    setup(); sc_queue_t q, recovered; sc_queue_item_t item;
    assert(sc_queue_open(&q, prefix, &source, 1100000));
    uint64_t first = append_minute(&q, 0), second = append_minute(&q, 1);
    assert(!sc_queue_ack(&q, second)); assert(sc_queue_peek(&q, 0, &item));
    assert(item.queue_id == first && !strcmp(item.source.device_id, source.device_id));
    assert(sc_queue_peek(&q, first, &item) && item.queue_id == second);
    assert(sc_queue_open(&recovered, prefix, &source, 1100000));
    assert(recovered.queued_records == 2 && recovered.used_bytes == q.used_bytes);
    assert(sc_queue_ack(&recovered, first)); assert(sc_queue_ack(&recovered, first));
    assert(sc_queue_open(&q, prefix, &source, 1100000));
    assert(q.queued_records == 1 && sc_queue_peek(&q, 0, &item) && item.queue_id == second);
    assert(sc_queue_ack(&q, second)); assert(!sc_queue_peek(&q, 0, &item));
    assert(!q.used_bytes && !q.queued_records); cleanup();
}
static void test_queue_directory_io_error_preserves_existing_segments(void) {
    setup(); sc_queue_t q, failed, restored; sc_queue_item_t item;
    assert(sc_queue_open(&q, prefix, &source, 8192));
    uint64_t id = append_minute(&q, 42);
    for (int reads = 0; reads < 3; ++reads) {
        directory_reads_before_error = reads;
        assert(!sc_queue_open(&failed, prefix, &source, 8192));
        assert(!failed.healthy);
        directory_reads_before_error = -1;
        assert(sc_queue_open(&restored, prefix, &source, 8192));
        assert(restored.queued_records == 1 && sc_queue_peek(&restored, 0, &item));
        assert(item.queue_id == id);
        sc_minute_t minute;
        assert(sc_minute_decode(item.payload, item.length, &minute) && minute.seq == 42);
    }
    cleanup();
}

static void test_queue_old_boot_and_device_are_preserved(void) {
    setup(); sc_queue_t q; sc_queue_item_t item;
    assert(sc_queue_open(&q, prefix, &source, 1100000)); uint64_t old = append_minute(&q, 0);
    sc_source_t next = source; strcpy(next.boot_id, "new-boot"); strcpy(next.device_id, "new-device");
    assert(sc_queue_open(&q, prefix, &next, 1100000)); uint64_t newer = append_minute(&q, 0);
    assert(q.segment_count == 2 && sc_queue_peek(&q, 0, &item));
    assert(!strcmp(item.source.boot_id, "synthetic-boot"));
    assert(!strcmp(item.source.device_id, "synthetic-device"));
    assert(sc_queue_ack(&q, old) && sc_queue_peek(&q, 0, &item));
    assert(item.queue_id == newer && !strcmp(item.source.device_id, "new-device")); cleanup();
}
static void test_queue_partial_tail_is_not_valid_data(void) {
    setup(); sc_queue_t q; sc_queue_item_t item;
    assert(sc_queue_open(&q, prefix, &source, 1100000)); append_minute(&q, 0); append_minute(&q, 1);
    char path[256]; snprintf(path, sizeof(path), "%s-%016llx.scq", prefix,
        (unsigned long long)q.segments[0].id);
    FILE *f = fopen(path, "rb+"); assert(f);
    assert(!ftruncate(fileno(f), q.segments[0].bytes - 7)); fclose(f);
    assert(sc_queue_open(&q, prefix, &source, 1100000));
    assert(q.queued_records == 1 && q.corrupt_records == 1);
    assert(sc_queue_peek(&q, 0, &item) && !sc_queue_peek(&q, item.queue_id, &item));
    assert(sc_queue_open(&q, prefix, &source, 1100000)); assert(q.corrupt_records == 1);
    append_minute(&q, 2); assert(q.queued_records == 2); cleanup();
}
static void test_queue_crc_corruption_fails_closed(void) {
    setup(); sc_queue_t q; assert(sc_queue_open(&q, prefix, &source, 1100000));
    append_minute(&q, 0); append_minute(&q, 1);
    char path[256]; snprintf(path, sizeof(path), "%s-%016llx.scq", prefix,
        (unsigned long long)q.segments[0].id);
    FILE *f = fopen(path, "rb+"); assert(f);
    long offset = SC_QUEUE_HEADER_BYTES + SC_QUEUE_ENVELOPE_BYTES + SC_MINUTE_BYTES +
        SC_QUEUE_ENVELOPE_BYTES + 10;
    assert(!fseek(f, offset, SEEK_SET)); int value = fgetc(f); assert(value != EOF);
    assert(!fseek(f, offset, SEEK_SET)); assert(fputc(value ^ 1, f) != EOF); fclose(f);
    assert(sc_queue_open(&q, prefix, &source, 1100000));
    assert(q.queued_records == 1 && q.corrupt_records == 1); cleanup();
}
static void test_queue_metadata_redundancy_and_damage(void) {
    setup(); sc_queue_t q; assert(sc_queue_open(&q, prefix, &source, 1100000));
    uint64_t first = append_minute(&q, 0); append_minute(&q, 1); assert(sc_queue_ack(&q, first));
    char path[256]; snprintf(path, sizeof(path), "%s.meta0", prefix);
    FILE *f = fopen(path, "wb"); assert(f); fputs("partial", f); fclose(f);
    assert(sc_queue_open(&q, prefix, &source, 1100000)); assert(q.queued_records == 1);
    for (unsigned i = 0; i < 2; ++i) {
        snprintf(path, sizeof(path), "%s.meta%u", prefix, i);
        f = fopen(path, "wb"); assert(f); fputs("broken", f); fclose(f);
    }
    assert(!sc_queue_open(&q, prefix, &source, 1100000)); cleanup();
}
static void test_queue_overflow_reports_durable_losses(void) {
    setup(); sc_queue_t q; assert(sc_queue_open(&q, prefix, &source, 700));
    for (uint64_t i = 0; i < 20; ++i) append_minute(&q, i);
    assert(q.dropped[SC_RECORD_MINUTE - 1] > 0 && q.queued_records > 0);
    uint64_t losses = q.dropped[0], pending = q.queued_records;
    assert(losses + pending == 20 && q.used_bytes <= 700);
    assert(sc_queue_open(&q, prefix, &source, 700));
    assert(q.dropped[0] == losses && q.queued_records == pending);
    assert(q.first_dropped_id == 1 && q.last_dropped_id == losses); cleanup();
}
static void test_queue_segment_rotation_and_source_header_crc(void) {
    setup(); sc_queue_t q; assert(sc_queue_open(&q, prefix, &source, 1100000));
    for (uint64_t i = 0; i < 130; ++i) append_minute(&q, i);
    assert(q.segment_count == 2 && q.segments[0].records == 128);
    char path[256]; snprintf(path, sizeof(path), "%s-%016llx.scq", prefix,
        (unsigned long long)q.segments[0].id);
    FILE *f = fopen(path, "rb+"); assert(f); assert(!fseek(f, 119, SEEK_SET));
    fputc('X', f); fclose(f);
    assert(!sc_queue_open(&q, prefix, &source, 1100000)); cleanup();
}
static void test_queue_64_boot_segments_recover_and_report_eviction(void) {
    setup(); sc_queue_t q, restored; sc_source_t boot = source;
    for (unsigned i = 0; i < SC_QUEUE_SEGMENTS; ++i) {
        snprintf(boot.boot_id, sizeof(boot.boot_id), "synthetic-boot-%u", i);
        assert(sc_queue_open(&q, prefix, &boot, 100000));
        append_minute(&q, 1);
    }
    assert(q.segment_count == SC_QUEUE_SEGMENTS && q.queued_records == SC_QUEUE_SEGMENTS);
    strcpy(boot.boot_id, "synthetic-next-boot");
    assert(sc_queue_open(&restored, prefix, &boot, 100000));
    assert(restored.segment_count == SC_QUEUE_SEGMENTS && !restored.dropped[0]);
    append_minute(&restored, 1);
    assert(restored.segment_count == SC_QUEUE_SEGMENTS && restored.dropped[0] == 1);
    assert(sc_queue_open(&q, prefix, &boot, 100000));
    assert(q.queued_records == SC_QUEUE_SEGMENTS && q.dropped[0] == 1);
    sc_queue_item_t item;
    assert(sc_queue_peek(&q, 0, &item) && !strcmp(item.source.boot_id, "synthetic-boot-1"));
    cleanup();
}

static void test_queue_48_hour_worst_event_occupancy(void) {
    setup(); sc_queue_t q; assert(sc_queue_open(&q, prefix, &source, 1100000));
    for (uint64_t i = 0; i < 2880; ++i) {
        append_minute(&q, i);
        sc_event_bucket_t e; sc_event_bucket_reset(&e);
        sc_event_second_t second = {.mono_start_ms=i*SC_WINDOW_MS, .event_id=i+1,
            .dbfs=-30,.baseline_dbfs=-50,.event=true,.detector_ready=true,.event_started=true};
        sc_event_bucket_add(&e, &second); uint8_t bytes[SC_EVENT_BYTES];
        assert(sc_event_encode(i, &e, bytes, sizeof(bytes)) == sizeof(bytes));
        assert(sc_queue_append(&q, SC_RECORD_EVENT, bytes, sizeof(bytes), NULL));
        if (i % 5 == 0) {
            sc_status_t s = {.seq=i/5,.monotonic_ms=i*SC_WINDOW_MS};
            uint8_t status[SC_STATUS_BYTES]; sc_status_encode(&s, status, sizeof(status));
            assert(sc_queue_append(&q, SC_RECORD_STATUS, status, sizeof(status), NULL));
        }
        if (i % 60 == 0) {
            sc_anchor_t a = {.seq=i/60,.monotonic_ms=i*SC_WINDOW_MS,
                .utc_ms=1767225600000LL + (int64_t)i*SC_WINDOW_MS,.uncertainty_ms=UINT32_MAX};
            uint8_t anchor[SC_ANCHOR_BYTES]; sc_anchor_encode(&a, anchor, sizeof(anchor));
            assert(sc_queue_append(&q, SC_RECORD_ANCHOR, anchor, sizeof(anchor), NULL));
        }
    }
    for (unsigned i = 0; i < 4; ++i) assert(q.dropped[i] == 0);
    assert(q.used_bytes < 1179648 && q.queued_records == 2880*2+576+48);
    printf("48h payload+envelope+segment bytes=%u, metadata=192, records=%u, segments=%u\n",
           q.used_bytes, q.queued_records, q.segment_count);
    assert(sc_queue_open(&q, prefix, &source, 1100000));
    assert(q.queued_records == 6384 && q.dropped[0] == 0); cleanup();
}
int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    RUN(test_pcm_chunked_rms_and_clipping);
    RUN(test_pcm_silence_missing_and_overflow);
    RUN(test_scalar_missing_is_not_zero);
    RUN(test_minute_averages_linear_energy);
    RUN(test_events_warmup_hysteresis_and_delay);
    RUN(test_events_merge_counts_and_active_duration);
    RUN(test_events_missing_rewarms_and_marks_incomplete);
    RUN(test_events_cross_minute_continuation);
    RUN(test_codecs_and_crc);
    RUN(test_aggregate_delays_and_preserves_sensor_channels);
    RUN(test_aggregate_startup_gap_duplicates_and_late_data);
    RUN(test_aggregate_audio_outage_emits_missing_minutes);
    RUN(test_aggregate_events_cross_minute_finalization);
    RUN(test_aggregate_independent_sensor_failure);
    RUN(test_aggregate_short_quiet_gap_excluded_from_activity);
    RUN(test_aggregate_interruption_does_not_invent_continuation);
    RUN(test_queue_recovery_and_exact_ack);
    RUN(test_queue_directory_io_error_preserves_existing_segments);
    RUN(test_queue_old_boot_and_device_are_preserved);
    RUN(test_queue_partial_tail_is_not_valid_data);
    RUN(test_queue_crc_corruption_fails_closed);
    RUN(test_queue_metadata_redundancy_and_damage);
    RUN(test_queue_overflow_reports_durable_losses);
    RUN(test_queue_segment_rotation_and_source_header_crc);
    RUN(test_queue_64_boot_segments_recover_and_report_eviction);
    RUN(test_queue_48_hour_worst_event_occupancy);
    printf("%u core tests passed; sizeof(sc_minute_t)=%zu sizeof(sc_queue_t)=%zu\n",
           tests, sizeof(sc_minute_t), sizeof(sc_queue_t));
    return 0;
}
