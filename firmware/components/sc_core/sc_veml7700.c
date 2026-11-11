#include "sc_veml7700.h"
#include <math.h>
#include <string.h>

void sc_veml_reset(sc_veml_t *sensor) { memset(sensor, 0, sizeof(*sensor)); }
static bool read_word(const sc_veml_io_t *io, uint8_t reg, uint16_t *word) {
    uint8_t bytes[2];
    if (!io->read_word(io->context, reg, bytes)) return false;
    *word = (uint16_t)bytes[0] | ((uint16_t)bytes[1] << 8);
    return true;
}
static bool write_word(const sc_veml_io_t *io, uint8_t reg, uint16_t word) {
    const uint8_t bytes[2] = {(uint8_t)word, (uint8_t)(word >> 8)};
    return io->write_word(io->context, reg, bytes);
}
static bool settings(const sc_veml_io_t *io, bool *match) {
    uint16_t conf, psm;
    if (!read_word(io, SC_VEML_REG_CONF, &conf) ||
        !read_word(io, SC_VEML_REG_PSM, &psm)) return false;
    *match = conf == SC_VEML_CONF && psm == SC_VEML_PSM;
    return true;
}
static bool failure(sc_veml_t *sensor, sc_veml_sample_t *sample) {
    sensor->configured = false;
    sample->quality = SC_Q_INVALID | SC_Q_BUS_ERROR;
    return false;
}
static bool configure(sc_veml_t *sensor, const sc_veml_io_t *io,
                      sc_veml_sample_t *sample) {
    bool match = false;
    /* Program integration/gain while shut down, then disable PSM and enable ALS. */
    if (!write_word(io, SC_VEML_REG_CONF, SC_VEML_CONF | 1u) ||
        !write_word(io, SC_VEML_REG_PSM, SC_VEML_PSM) ||
        !write_word(io, SC_VEML_REG_CONF, SC_VEML_CONF) ||
        !settings(io, &match) || !match) return failure(sensor, sample);
    sensor->configured = true;
    /* Read time after I/O: a slow/retried transaction must not shorten settling. */
    sensor->ready_at_ms = io->now_ms(io->context) + SC_VEML_READY_MS;
    sample->quality = SC_Q_WARMING;
    return false;
}
bool sc_veml_poll(sc_veml_t *sensor, const sc_veml_io_t *io, sc_veml_sample_t *sample) {
    if (!sample) return false;
    memset(sample, 0, sizeof(*sample)); sample->lux = NAN;
    if (!sensor || !io || !io->read_word || !io->write_word || !io->now_ms) {
        sample->quality = SC_Q_INVALID | SC_Q_BUS_ERROR;
        return false;
    }
    if (!sensor->configured) return configure(sensor, io, sample);
    if (io->now_ms(io->context) < sensor->ready_at_ms) {
        sample->quality = SC_Q_WARMING;
        return false;
    }
    bool match = false;
    if (!settings(io, &match)) return failure(sensor, sample);
    if (!match) {
        sensor->configured = false;
        return configure(sensor, io, sample);
    }
    uint16_t raw;
    if (!read_word(io, SC_VEML_REG_ALS, &raw) || !settings(io, &match))
        return failure(sensor, sample);
    /* Catch a power reset during the ALS transaction; stale data is not usable. */
    if (!match) {
        sensor->configured = false;
        sample->quality = SC_Q_WARMING;
        return false;
    }
    sample->raw = raw;
    if (raw == SC_VEML_SATURATION_RAW) {
        sample->quality = SC_Q_INVALID | SC_Q_OUT_OF_RANGE;
        return false;
    }
    double lux = raw * SC_VEML_RESOLUTION_LUX;
    sample->lux = (float)lux;
    sample->valid = true;
    if (lux < SC_VEML_LOW_LIGHT_LUX) sample->quality |= SC_Q_LOW_LIGHT;
    return true;
}
