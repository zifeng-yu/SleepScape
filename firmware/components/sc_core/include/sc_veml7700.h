#ifndef SC_VEML7700_H
#define SC_VEML7700_H
#include "sc_core.h"

/* The immutable profile pins Vishay 84286 Rev 1.8 and 84323, 2025-03-06.
 * Older DFRobot examples use a different conversion; do not mix those values. */
#define SC_FIRMWARE_VERSION "0.2.0"
#define SC_LIGHT_SENSOR_MODEL "dfrobot-sen0228-veml7700"
#define SC_LIGHT_REFERENCE "vishay-84286-rev1.8-84323-20250306"
#define SC_SENSOR_HUB_MODEL "dfrobot-dfr0759"
#define SC_SENSOR_SDA 21
#define SC_SENSOR_SCL 25
#define SC_SENSOR_I2C_HZ 100000u
#define SC_SENSOR_SUPPLY_V 3.3
#define SC_AHT_ADDRESS 0x38u
#define SC_VEML_ADDRESS 0x10u
#define SC_VEML_GAIN 2.0
#define SC_VEML_INTEGRATION_MS 400u
#define SC_VEML_RESOLUTION_LUX 0.0084
#define SC_VEML_LOW_LIGHT_LUX 0.1
#define SC_VEML_SATURATION_RAW 65535u
#define SC_VEML_MAX_LUX ((SC_VEML_SATURATION_RAW - 1u) * SC_VEML_RESOLUTION_LUX)
#define SC_VEML_CONF 0x0880u
#define SC_VEML_PSM 0x0000u
/* >=2.5 ms power-up allowance plus one complete integration, PSM disabled. */
#define SC_VEML_READY_MS (SC_VEML_INTEGRATION_MS + 3u)
#define SC_VEML_REG_CONF 0x00u
#define SC_VEML_REG_PSM 0x03u
#define SC_VEML_REG_ALS 0x04u

typedef struct {
    void *context;
    bool (*read_word)(void *context, uint8_t reg, uint8_t bytes[2]);
    bool (*write_word)(void *context, uint8_t reg, const uint8_t bytes[2]);
    uint64_t (*now_ms)(void *context);
} sc_veml_io_t;
typedef struct { bool configured; uint64_t ready_at_ms; } sc_veml_t;
typedef struct { bool valid; float lux; uint16_t raw; uint32_t quality; } sc_veml_sample_t;

void sc_veml_reset(sc_veml_t *sensor);
/* Nonblocking warm-up. Poll once/second; failures always return NAN, never zero.
 * Register readbacks detect reset/misconfiguration; no revision-dependent ID probe. */
bool sc_veml_poll(sc_veml_t *sensor, const sc_veml_io_t *io, sc_veml_sample_t *sample);
#endif
