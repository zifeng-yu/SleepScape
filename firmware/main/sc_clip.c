#include "sc_clip.h"
#include "sc_network.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

/* One aggregate producer and one HTTPS consumer. No audio in the flash queue. */
typedef struct {
    size_t length;
    uint8_t body[];
} packet_t;
static _Atomic(packet_t *) ready;
static packet_t *building;
static uint64_t first_slot;
static unsigned seconds;
static uint64_t used_minute = UINT64_MAX;
static int64_t counted_day;
static unsigned night_count;

uint8_t sc_clip_encode(int16_t sample) {
    int magnitude = sample < 0 ? -(int)sample : sample;
    if (magnitude > 32635) magnitude = 32635;
    magnitude += 132;
    unsigned exponent = 7;
    for (int mask = 0x4000; exponent && !(magnitude & mask); mask >>= 1) --exponent;
    return (uint8_t)~((sample < 0 ? 128 : 0) | (exponent << 4) |
                       ((magnitude >> (exponent + 3)) & 15));
}

int64_t sc_clip_clock(uint64_t slot) {
    if (!sc_network_ready()) return 0;
    struct timeval tv;
    gettimeofday(&tv, NULL);
    int64_t utc = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000 -
                  (esp_timer_get_time() / 1000 - (int64_t)slot);
    int hour = (int)((utc / 3600000 + 8) % 24);
    return hour >= 18 || hour < 12 ? utc : 0;
}

uint8_t *sc_clip_second(void) {
    return heap_caps_get_free_size(MALLOC_CAP_8BIT) > 56000 + SC_CLIP_RATE ?
        malloc(SC_CLIP_RATE) : NULL;
}

static void finish(void) {
    if (!building) return;
    packet_t *empty = NULL;
    if (!atomic_compare_exchange_strong(&ready, &empty, building)) free(building);
    building = NULL;
}

void sc_clip_accept(const char *boot, uint64_t slot, int64_t utc, bool active, uint8_t *second) {
    uint64_t minute = slot / 60000;
    if (building && (!second || !utc || !active || slot != first_slot + seconds * 1000 ||
                     minute != used_minute)) finish();
    if (!second || !utc || !active) goto done;
    int64_t day = (utc / 1000 + 14 * 3600) / 86400;
    if (day != counted_day) { counted_day = day; night_count = 0; }
    if (!building) {
        if (minute == used_minute || night_count >= 60 || atomic_load(&ready)) goto done;
        building = malloc(sizeof(*building) + 96);
        if (!building) goto done;
        building->length = (size_t)snprintf((char *)building->body, 96, "%s %"PRIu64" %"PRId64"\n",
                                           boot, slot, utc);
        first_slot = slot;
        seconds = 0;
        used_minute = minute;
        ++night_count;
    }
    packet_t *grown = NULL;
    if (heap_caps_get_free_size(MALLOC_CAP_8BIT) > 56000 + SC_CLIP_RATE)
        grown = realloc(building, sizeof(*building) + building->length + SC_CLIP_RATE);
    if (!grown) {
        if (seconds) finish(); else { free(building); building = NULL; }
        goto done;
    }
    building = grown;
    memcpy(building->body + building->length, second, SC_CLIP_RATE);
    building->length += SC_CLIP_RATE;
    if (++seconds == 12) finish();
done:
    free(second);
}

void sc_clip_upload(void) {
    packet_t *packet = atomic_exchange(&ready, NULL);
    if (packet) {
        sc_network_audio(packet->body, packet->length);
        free(packet);
    }
}
