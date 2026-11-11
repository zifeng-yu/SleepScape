/* Fault injection is host-only. Production uses the same driver state machine. */
#include "sc_veml7700.h"
#include "sc_aggregate.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint16_t registers[5];
    uint64_t now;
    unsigned latency, reads, als_reads, writes;
    unsigned fail_read, fail_write;
    bool disconnected, ignore_writes, reset_on_als;
    uint8_t write_reg[16], write_bytes[16][2];
} device_t;
static bool read_reg(void *context, uint8_t reg, uint8_t bytes[2]) {
    device_t *d = context; assert(reg < 5);
    ++d->reads; d->now += d->latency;
    if (d->disconnected || d->reads == d->fail_read) return false;
    uint16_t value = d->registers[reg];
    bytes[0] = (uint8_t)value; bytes[1] = (uint8_t)(value >> 8);
    if (reg == 4) {
        ++d->als_reads;
        if (d->reset_on_als) { d->registers[0] = 1; d->registers[3] = 0; }
    }
    return true;
}
static bool write_reg(void *context, uint8_t reg, const uint8_t bytes[2]) {
    device_t *d = context; assert(reg < 5);
    if (d->writes < 16) {
        d->write_reg[d->writes] = reg;
        memcpy(d->write_bytes[d->writes], bytes, 2);
    }
    ++d->writes; d->now += d->latency;
    if (d->disconnected || d->writes == d->fail_write) return false;
    if (!d->ignore_writes) d->registers[reg] = bytes[0] | ((uint16_t)bytes[1] << 8);
    return true;
}
static uint64_t now_ms(void *context) { return ((device_t *)context)->now; }
static sc_veml_io_t io_for(device_t *d) {
    sc_veml_io_t io = {.context=d, .read_word=read_reg, .write_word=write_reg, .now_ms=now_ms};
    return io;
}
static void ready(device_t *d, sc_veml_t *sensor, const sc_veml_io_t *io) {
    sc_veml_sample_t sample; sc_veml_reset(sensor);
    assert(!sc_veml_poll(sensor, io, &sample));
    assert(sample.quality == SC_Q_WARMING && isnan(sample.lux));
    d->now = sensor->ready_at_ms;
}
static void test_startup_and_real_bus_elapsed_time(void) {
    device_t d = {.latency=50}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d);
    sc_veml_reset(&sensor); sc_veml_sample_t s;
    assert(!sc_veml_poll(&sensor, &io, &s));
    assert(d.writes == 3 && d.reads == 2 && !d.als_reads);
    /* Independently specified on-wire words catch endian/register-mode errors. */
    assert(d.write_reg[0] == 0 && d.write_bytes[0][0] == 0x81 && d.write_bytes[0][1] == 0x08);
    assert(d.write_reg[1] == 3 && d.write_bytes[1][0] == 0 && d.write_bytes[1][1] == 0);
    assert(d.write_reg[2] == 0 && d.write_bytes[2][0] == 0x80 && d.write_bytes[2][1] == 0x08);
    assert(sensor.ready_at_ms == 653); /* 250 ms transactions + 403 ms settling. */
    d.now = 652;
    assert(!sc_veml_poll(&sensor, &io, &s) && !d.als_reads && isnan(s.lux));
    d.now = 653; d.registers[4] = 0x1234;
    assert(sc_veml_poll(&sensor, &io, &s));
    assert(fabs(s.lux - 39.144) < 0.00001 && s.raw == 4660 && !s.quality);
}
static void test_zero_low_light_and_saturation(void) {
    device_t d = {0}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d); ready(&d,&sensor,&io);
    sc_veml_sample_t s;
    assert(sc_veml_poll(&sensor,&io,&s) && s.lux == 0 && s.quality == SC_Q_LOW_LIGHT);
    d.registers[4] = 11;
    assert(sc_veml_poll(&sensor,&io,&s) && fabs(s.lux - 0.0924) < 1e-7);
    assert(s.quality == SC_Q_LOW_LIGHT);
    d.registers[4] = 12;
    assert(sc_veml_poll(&sensor,&io,&s) && fabs(s.lux - 0.1008) < 1e-7 && !s.quality);
    d.registers[4] = 65534;
    assert(sc_veml_poll(&sensor,&io,&s) && fabs(s.lux - 550.4856) < 0.0001);
    d.registers[4] = 65535;
    assert(!sc_veml_poll(&sensor,&io,&s) && isnan(s.lux) && !s.valid);
    assert(s.quality == (SC_Q_INVALID | SC_Q_OUT_OF_RANGE) && sensor.configured);
    d.registers[4] = 100;
    assert(sc_veml_poll(&sensor,&io,&s) && fabs(s.lux - 0.84) < 1e-7 && !s.quality);
    assert(d.writes == 3); /* Saturation does not restart valid continuous conversion. */
}
static void test_nack_disconnect_recovery(void) {
    device_t d = {0}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d); ready(&d,&sensor,&io);
    sc_veml_sample_t s; d.disconnected = true;
    assert(!sc_veml_poll(&sensor,&io,&s) && !sensor.configured && isnan(s.lux));
    assert(s.quality == (SC_Q_INVALID | SC_Q_BUS_ERROR));
    assert(!sc_veml_poll(&sensor,&io,&s) && isnan(s.lux));
    d.disconnected = false; d.registers[4] = 500;
    unsigned reads = d.als_reads;
    assert(!sc_veml_poll(&sensor,&io,&s) && s.quality == SC_Q_WARMING);
    assert(!sc_veml_poll(&sensor,&io,&s) && d.als_reads == reads);
    d.now = sensor.ready_at_ms;
    assert(sc_veml_poll(&sensor,&io,&s) && fabs(s.lux - 4.2) < 1e-6);
}
static void test_power_reset_before_or_during_read(void) {
    device_t d = {0}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d); ready(&d,&sensor,&io);
    sc_veml_sample_t s; d.registers[0] = 1; d.registers[4] = 999;
    assert(!sc_veml_poll(&sensor,&io,&s) && s.quality == SC_Q_WARMING && !d.als_reads);
    d.now = sensor.ready_at_ms; d.reset_on_als = true;
    assert(!sc_veml_poll(&sensor,&io,&s) && s.quality == SC_Q_WARMING && isnan(s.lux));
    assert(!sensor.configured && !s.valid);
    d.reset_on_als = false;
    assert(!sc_veml_poll(&sensor,&io,&s)); d.now = sensor.ready_at_ms;
    assert(sc_veml_poll(&sensor,&io,&s));
}
static void test_psm_and_gain_drift_reconfigured(void) {
    device_t d = {0}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d); ready(&d,&sensor,&io);
    sc_veml_sample_t s; d.registers[3] = 7;
    assert(!sc_veml_poll(&sensor,&io,&s) && s.quality == SC_Q_WARMING && !d.als_reads);
    assert(d.registers[3] == 0); d.now = sensor.ready_at_ms;
    d.registers[0] = 0; /* Same address, wrong gain/integration cannot use fixed factor. */
    assert(!sc_veml_poll(&sensor,&io,&s) && s.quality == SC_Q_WARMING && !d.als_reads);
    assert(d.registers[0] == 0x0880);
}
static void test_configuration_and_transaction_failures(void) {
    for (unsigned failure = 1; failure <= 3; ++failure) {
        device_t d = {.fail_write=failure}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d);
        sc_veml_reset(&sensor); sc_veml_sample_t s;
        assert(!sc_veml_poll(&sensor,&io,&s) && isnan(s.lux) && !sensor.configured);
        assert(s.quality & SC_Q_BUS_ERROR);
    }
    device_t d = {.ignore_writes=true}; sc_veml_t sensor; sc_veml_io_t io = io_for(&d);
    sc_veml_reset(&sensor); sc_veml_sample_t s;
    assert(!sc_veml_poll(&sensor,&io,&s) && (s.quality & SC_Q_BUS_ERROR) && !sensor.configured);
    /* Every normal read transaction, including the post-data reset guard, can fail. */
    for (unsigned failure = 1; failure <= 5; ++failure) {
        memset(&d,0,sizeof(d)); ready(&d,&sensor,&io); d.fail_read = d.reads + failure;
        assert(!sc_veml_poll(&sensor,&io,&s) && isnan(s.lux) && !sensor.configured);
        assert(s.quality & SC_Q_BUS_ERROR);
    }
}
static sc_minute_t emitted;
static void collect(void *context, const sc_minute_t *m) { (void)context; emitted = *m; }
static void test_quality_survives_aggregation_and_binary_storage(void) {
    sc_aggregate_t aggregate; sc_aggregate_init(&aggregate,0,collect,NULL);
    sc_audio_second_t missing = {.dbfs=NAN,.quality=SC_Q_MISSING};
    for (unsigned second=0;second<63;++second) {
        uint32_t q = second == 0 ? SC_Q_WARMING : second == 1 ? SC_Q_INVALID|SC_Q_OUT_OF_RANGE :
                     second == 2 ? SC_Q_INVALID|SC_Q_BUS_ERROR : SC_Q_LOW_LIGHT;
        bool valid = second > 2;
        assert(sc_aggregate_sensor(&aggregate,second*1000,valid,valid?0.0f:NAN,q,
                                   second%10==0,true,21,50));
        assert(sc_aggregate_audio(&aggregate,second*1000,&missing));
    }
    uint8_t encoded[SC_MINUTE_BYTES]; sc_minute_t decoded;
    assert(sc_minute_encode(&emitted,encoded,sizeof(encoded)) == sizeof(encoded));
    assert(sc_minute_decode(encoded,sizeof(encoded),&decoded));
    assert(decoded.light.observed_count == 60 && decoded.light.valid_count == 57);
    assert(decoded.light.quality == (SC_Q_WARMING|SC_Q_INVALID|SC_Q_OUT_OF_RANGE|
                                    SC_Q_BUS_ERROR|SC_Q_LOW_LIGHT|SC_Q_PARTIAL));
    assert(sc_scalar_mean(&decoded.light) == 0 && decoded.temperature.valid_count == 6);
}
static void test_warming_does_not_erase_real_failure(void) {
    sc_aggregate_t a; sc_aggregate_init(&a,0,collect,NULL);
    assert(sc_aggregate_sensor(&a,0,false,NAN,SC_Q_WARMING,false,false,NAN,NAN));
    sc_scalar_t *light = a.slots[1].used ? &a.slots[1].minute.light : &a.slots[0].minute.light;
    assert(light->quality == SC_Q_WARMING && light->valid_count == 0);
    assert(sc_aggregate_sensor(&a,1000,false,NAN,SC_Q_INVALID|SC_Q_BUS_ERROR,false,false,NAN,NAN));
    assert(sc_aggregate_sensor(&a,2000,false,NAN,SC_Q_WARMING,false,false,NAN,NAN));
    assert((light->quality & SC_Q_INVALID) && (light->quality & SC_Q_BUS_ERROR));
}
int main(void) {
    test_startup_and_real_bus_elapsed_time(); test_zero_low_light_and_saturation();
    test_nack_disconnect_recovery(); test_power_reset_before_or_during_read();
    test_psm_and_gain_drift_reconfigured(); test_configuration_and_transaction_failures();
    test_quality_survives_aggregation_and_binary_storage(); test_warming_does_not_erase_real_failure();
    puts("VEML7700: 8 host test groups passed (ASan/UBSan)"); return 0;
}
