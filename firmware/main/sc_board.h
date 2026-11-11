#ifndef SC_BOARD_H
#define SC_BOARD_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define SC_BOARD_MODEL "m5stack-c008-c"
#define SC_FIRMWARE_VERSION "0.1.0"
/* Original C008-C only. EchoS3R uses a different pin map. */
#define SC_AHT_SDA 21
#define SC_AHT_SCL 25
#define SC_LIGHT_SDA 26
#define SC_LIGHT_SCL 32
#define SC_MIC_CLK 33
#define SC_MIC_DATA 23

esp_err_t sc_board_init(void);
bool sc_board_temperature(float *temperature, float *humidity);
bool sc_board_light(float *lux);
esp_err_t sc_board_audio_read(int16_t *samples, size_t capacity, size_t *count);
uint32_t sc_board_audio_overflows(void);
#endif
