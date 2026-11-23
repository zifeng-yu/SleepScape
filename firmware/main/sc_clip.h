#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#define SC_CLIP_RATE 8000
uint8_t sc_clip_encode(int16_t sample);
int64_t sc_clip_clock(uint64_t slot);
uint8_t *sc_clip_second(void);
void sc_clip_accept(const char *boot, uint64_t slot, int64_t utc, bool active, uint8_t *second);
void sc_clip_upload(void);
