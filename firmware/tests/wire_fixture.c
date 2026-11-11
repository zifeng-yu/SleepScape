/* Synthetic native fixture: exercises production aggregation, binary storage and wire code. */
#include "sc_wire.h"
#include "sc_aggregate.h"
#include "sc_veml7700.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static sc_source_t source(void) {
    sc_source_t s = {0};
    strcpy(s.device_id, "synthetic-device"); strcpy(s.boot_id, "synthetic-boot");
    strcpy(s.measurement_profile_id, "synthetic-profile"); return s;
}
static sc_minute_t minute(const char *scenario, uint64_t seq) {
    sc_minute_t m; sc_minute_reset(&m, seq, seq * 60000);
    unsigned seconds = !strcmp(scenario, "partial") ? 30 : 60;
    m.duration_ms = seconds * 1000;
    m.temperature.expected_count = m.humidity.expected_count = seconds / 10;
    m.light.expected_count = m.sound_expected_count = seconds;
    m.sound_expected_samples = seconds * SC_SAMPLE_RATE;
    for (unsigned i = 0; i < seconds; ++i) {
        bool absent = !strcmp(scenario, "missing");
        if (!(i % 10)) {
            assert(sc_scalar_add(&m.temperature, !absent, !absent, 21.5f));
            assert(sc_scalar_add(&m.humidity, !absent, !absent, 50));
        }
        assert(sc_scalar_add(&m.light, !absent, !absent, 0));
        if (!absent) m.light.quality |= SC_Q_LOW_LIGHT;
        sc_pcm_t pcm; sc_pcm_reset(&pcm);
        int16_t samples[160];
        for (unsigned n = 0; n < 160; ++n) samples[n] = !strcmp(scenario, "zero") ? 0 : 320;
        if (!absent) for (unsigned n = 0; n < 100; ++n) assert(sc_pcm_add(&pcm, samples, 160));
        if (!strcmp(scenario, "overflow") && i == 0) sc_pcm_gap(&pcm, 1);
        sc_audio_second_t sound = sc_pcm_finish(&pcm);
        assert(sc_minute_add_sound(&m, &sound));
    }
    if (!strcmp(scenario, "ready") || !strcmp(scenario, "event")) {
        m.detector_state = SC_DETECTOR_READY;
        m.events.valid_duration_ms = seconds * 1000;
    }
    if (!strcmp(scenario, "event")) {
        m.events.has_event = true; m.events.mono_start_ms = m.mono_start_ms + 10000;
        m.events.mono_end_ms = m.mono_start_ms + 14000; m.events.active_duration_ms = 3000;
        m.events.detected_count = 2; m.events.stored_count = m.events.merged_count = 1;
        m.events.quality = SC_Q_MERGED | SC_Q_INCOMPLETE;
        m.events.max_1s_rms_dbfs = -20; m.events.baseline_dbfs = NAN;
    }
    return m;
}
typedef struct { sc_queue_item_t *rows; size_t count; } emitted_t;
static void emitted(void *context, const sc_minute_t *m) {
    emitted_t *out = context;
    assert(out->count < SC_WIRE_MAX_ITEMS);
    sc_queue_item_t *row = &out->rows[out->count++];
    row->type = SC_RECORD_MINUTE;
    row->length = sc_minute_encode(m, row->payload, SC_RECORD_MAX);
    if (m->events.has_event) {
        assert(out->count < SC_WIRE_MAX_ITEMS);
        row = &out->rows[out->count++]; row->type = SC_RECORD_EVENT;
        row->length = sc_event_encode(m->seq, &m->events, row->payload, SC_RECORD_MAX);
    }
}
static size_t aggregate_fixture(sc_queue_item_t *rows) {
    emitted_t out = {.rows = rows, .count = 0}; sc_aggregate_t aggregate;
    sc_aggregate_init(&aggregate, 0, emitted, &out);
    for (unsigned second = 0; second < 186; ++second) {
        assert(sc_aggregate_sensor(&aggregate, second * 1000, true, 0, SC_Q_LOW_LIGHT,
                                  second % 10 == 0, true, 21.5f, 50));
        sc_pcm_t pcm; sc_pcm_reset(&pcm); int16_t samples[160];
        for (unsigned i = 0; i < 160; ++i)
            samples[i] = second >= 117 && second <= 124 ? 3200 : 100;
        for (unsigned i = 0; i < 100; ++i) assert(sc_pcm_add(&pcm, samples, 160));
        sc_audio_second_t sound = sc_pcm_finish(&pcm);
        assert(sc_aggregate_audio(&aggregate, second * 1000, &sound));
    }
    return out.count;
}
typedef struct { uint16_t registers[5]; uint64_t now; bool fail; } fixture_light_t;
static bool fixture_light_read(void *context, uint8_t reg, uint8_t bytes[2]) {
    fixture_light_t *d = context;
    if (d->fail || reg >= 5) return false;
    bytes[0] = (uint8_t)d->registers[reg]; bytes[1] = (uint8_t)(d->registers[reg] >> 8);
    return true;
}
static bool fixture_light_write(void *context, uint8_t reg, const uint8_t bytes[2]) {
    fixture_light_t *d = context;
    if (d->fail || reg >= 5) return false;
    d->registers[reg] = bytes[0] | ((uint16_t)bytes[1] << 8);
    return true;
}
static uint64_t fixture_light_now(void *context) { return ((fixture_light_t *)context)->now; }
static size_t light_fixture(const char *scenario, sc_queue_item_t *rows) {
    if (!strcmp(scenario, "light-legacy")) {
        sc_minute_t m = minute("full", 0);
        sc_scalar_reset(&m.light, 60);
        for (unsigned i = 0; i < 60; ++i) assert(sc_scalar_add(&m.light, true, true, 0.05f));
        rows[0].type = SC_RECORD_MINUTE;
        rows[0].length = sc_minute_encode(&m, rows[0].payload, SC_RECORD_MAX);
        return 1;
    }
    emitted_t out = {.rows=rows}; sc_aggregate_t aggregate;
    sc_aggregate_init(&aggregate, 0, emitted, &out);
    fixture_light_t d = {0}; sc_veml_t sensor; sc_veml_reset(&sensor);
    sc_veml_io_t io = {.context=&d, .read_word=fixture_light_read,
                       .write_word=fixture_light_write, .now_ms=fixture_light_now};
    sc_veml_sample_t sample;
    assert(!sc_veml_poll(&sensor, &io, &sample));
    for (unsigned second = 0; second < 63; ++second) {
        d.now = 1000 + second * 1000;
        d.fail = !strcmp(scenario, "light-bus-error") ||
                 (!strcmp(scenario, "light-recovered") && second == 1);
        d.registers[4] = !strcmp(scenario, "light-saturated") ? 65535 :
                          !strcmp(scenario, "light-low") ? 11 : 12;
        if (!strcmp(scenario, "light-warming")) sc_veml_reset(&sensor);
        bool valid = sc_veml_poll(&sensor, &io, &sample);
        assert(sc_aggregate_sensor(&aggregate, second * 1000, valid, sample.lux,
                                   sample.quality, second % 10 == 0, true, 21.5f, 50));
        sc_audio_second_t missing = {.dbfs=NAN, .quality=SC_Q_MISSING};
        assert(sc_aggregate_audio(&aggregate, second * 1000, &missing));
    }
    return out.count;
}
static void profile(void) {
    printf("{\"firmware_version\":\"%s\",\"light_sensor\":\"%s\","
           "\"light_measurement\":{\"gain\":%.10g,\"integration_ms\":%u,"
           "\"resolution_lux\":%.10g,\"als_conf\":%u,\"power_saving_enabled\":false,"
           "\"auto_range\":false,\"saturation_raw\":%u,\"range_min_lux\":0.0,"
           "\"range_max_lux\":%.10g,\"low_light_limit_lux\":%.10g,"
           "\"conversion\":\"linear\",\"reference\":\"%s\"},"
           "\"i2c_bus\":{\"topology\":\"shared\",\"hub_model\":\"%s\","
           "\"sda_gpio\":%u,\"scl_gpio\":%u,\"frequency_hz\":%u,"
           "\"supply_voltage_v\":%.10g,\"temperature_address\":%u,\"light_address\":%u}}\n",
           SC_FIRMWARE_VERSION, SC_LIGHT_SENSOR_MODEL, SC_VEML_GAIN, SC_VEML_INTEGRATION_MS,
           SC_VEML_RESOLUTION_LUX, SC_VEML_CONF, SC_VEML_SATURATION_RAW, SC_VEML_MAX_LUX,
           SC_VEML_LOW_LIGHT_LUX, SC_LIGHT_REFERENCE, SC_SENSOR_HUB_MODEL, SC_SENSOR_SDA,
           SC_SENSOR_SCL, SC_SENSOR_I2C_HZ, SC_SENSOR_SUPPLY_V, SC_AHT_ADDRESS, SC_VEML_ADDRESS);
}
static size_t fixture(const char *scenario, sc_queue_item_t *rows) {
    memset(rows, 0, SC_WIRE_MAX_ITEMS * sizeof(*rows)); size_t count = 0;
    bool anchor_only = !strcmp(scenario, "anchor"), status_only = !strcmp(scenario, "status");
    bool maximal = !strcmp(scenario, "maximal");
    if (!strncmp(scenario, "light-", 6)) count = light_fixture(scenario, rows);
    else if (!strcmp(scenario, "aggregate")) count = aggregate_fixture(rows);
    else if (!anchor_only && !status_only) {
        unsigned minutes = maximal ? SC_WIRE_MAX_ITEMS : 1;
        for (unsigned i = 0; i < minutes; ++i) {
            sc_minute_t m = minute(scenario, i);
            rows[count].type = SC_RECORD_MINUTE;
            rows[count].length = sc_minute_encode(&m, rows[count].payload, SC_RECORD_MAX); ++count;
            if (!strcmp(scenario, "event")) {
                rows[count].type = SC_RECORD_EVENT;
                rows[count].length = sc_event_encode(m.seq, &m.events, rows[count].payload, SC_RECORD_MAX);
                ++count;
            }
        }
    }
    if (!maximal && !status_only) {
        sc_anchor_t a = {.seq = 0, .monotonic_ms = 0, .utc_ms = INT64_C(1767225600123),
                         .uncertainty_ms = UINT32_MAX};
        rows[count].type = SC_RECORD_ANCHOR;
        rows[count].length = sc_anchor_encode(&a, rows[count].payload, SC_RECORD_MAX); ++count;
    }
    if (!maximal && !anchor_only) {
        sc_status_t s = {.seq = 0, .monotonic_ms = 60000, .queued_records = 3};
        if (status_only) { s.dropped_minutes = 2; s.dropped_events = 3; s.corrupt_records = 1; s.quarantined_records = 4; }
        rows[count].type = SC_RECORD_STATUS;
        rows[count].length = sc_status_encode(&s, rows[count].payload, SC_RECORD_MAX); ++count;
    }
    for (size_t i = 0; i < count; ++i) {
        rows[i].source = source(); rows[i].queue_id = i + 1;
        if (!strcmp(scenario, "light-legacy"))
            strcpy(rows[i].source.measurement_profile_id, "historical-profile");
    }
    return count;
}
static void guards(void) {
    sc_queue_item_t rows[SC_WIRE_MAX_ITEMS]; size_t count = fixture("full", rows);
    assert(!sc_wire_batch(NULL, count)); assert(!sc_wire_batch(rows, 0));
    assert(!sc_wire_batch(rows, SC_WIRE_MAX_ITEMS + 1));
    rows[1].source.boot_id[0] = 'X'; assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[1].source.device_id[0] = 'X';
    assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[1].source.measurement_profile_id[0] = 'X';
    assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[1] = rows[0]; assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[0].payload[0] = 99; assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[0].source.device_id[0] = ' '; assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); memset(rows[0].source.device_id, 'x', sizeof(rows[0].source.device_id));
    assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); rows[0] = rows[2]; assert(!sc_wire_batch(rows, count));
    count = fixture("full", rows); sc_minute_t m = minute("full", UINT64_C(9007199254740992));
    rows[0].length = sc_minute_encode(&m, rows[0].payload, SC_RECORD_MAX);
    assert(!sc_wire_batch(rows, count));
    assert(!sc_wire_ack(NULL, rows, count));
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "profile")) { profile(); return 0; }
    if (argc == 2 && !strcmp(argv[1], "guards")) { guards(); return 0; }
    if (argc != 3) return 2;
    sc_queue_item_t rows[SC_WIRE_MAX_ITEMS]; size_t count = fixture(argv[2], rows);
    if (!strcmp(argv[1], "batch")) {
        char *payload = sc_wire_batch(rows, count);
        if (!payload) return 3;
        puts(payload); free(payload); return 0;
    }
    if (!strcmp(argv[1], "ack")) {
        char *response = malloc(SC_WIRE_MAX_BYTES + 2); if (!response) return 4;
        size_t bytes = fread(response, 1, SC_WIRE_MAX_BYTES + 1, stdin); response[bytes] = 0;
        bool result = sc_wire_ack(response, rows, count); free(response); return result ? 0 : 1;
    }
    return 2;
}
