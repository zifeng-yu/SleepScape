#include "sc_reject.h"
#include "sc_core.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#define REJECT_MAX 65536u
#define PAYLOAD_MAX 8192u
/* Independent quarantine: durable evidence of a rejected record, never an ACK.
 * Records contain the complete source-bound JSON and a CRC; no credentials/PCM.
 * A torn quarantine blocks further retirement rather than erasing evidence. */
static uint32_t record_crc(uint64_t id, int status, const char *payload, size_t length) {
    uint8_t header[16] = {0};
    for (unsigned i = 0; i < 8; ++i) header[i] = (uint8_t)(id >> (8 * i));
    for (unsigned i = 0; i < 4; ++i) {
        header[8 + i] = (uint8_t)((uint32_t)status >> (8 * i));
        header[12 + i] = (uint8_t)((uint32_t)length >> (8 * i));
    }
    return sc_crc32(header, sizeof(header)) ^ sc_crc32(payload, length);
}
static bool scan(FILE *file, uint32_t *count, uint64_t *last_id, uint32_t *last_crc) {
    char line[100]; *count = 0; *last_id = 0; *last_crc = 0;
    while (fgets(line, sizeof(line), file)) {
        unsigned long long id; unsigned length, crc; int status, end = 0;
        if (sscanf(line, "SCREJ1 %llx %d %u %x%n", &id, &status, &length, &crc, &end) != 4 ||
            line[end] != '\n' || !id || length > PAYLOAD_MAX || length == 0 ||
            status < 400 || status >= 500) return false;
        char *payload = malloc(length);
        if (!payload) return false;
        bool valid = fread(payload, 1, length, file) == length &&
            record_crc(id, status, payload, length) == crc && fgetc(file) == '\n';
        free(payload);
        if (!valid) return false;
        ++*count; *last_id = id; *last_crc = crc;
    }
    return !ferror(file) && ftell(file) >= 0 && (unsigned long)ftell(file) <= REJECT_MAX;
}
bool sc_reject_count(const char *path, uint32_t *count) {
    *count = 0;
    FILE *file = fopen(path, "rb");
    if (!file) return errno == ENOENT;
    uint64_t last; uint32_t crc;
    bool valid = scan(file, count, &last, &crc);
    fclose(file); return valid;
}
bool sc_reject_append(const char *path, uint64_t id, int status,
                      const char *payload, uint32_t *count) {
    size_t length = strlen(payload);
    if (!id || !length || length > PAYLOAD_MAX || status < 400 || status >= 500) return false;
    FILE *file = fopen(path, "rb");
    uint64_t last = 0; uint32_t crc = 0; uint32_t records = 0; long used = 0;
    if (file) {
        bool ok = scan(file, &records, &last, &crc); used = ftell(file);
        fclose(file); if (!ok || used < 0) return false;
    } else if (errno != ENOENT) return false;
    uint32_t checksum = record_crc(id, status, payload, length);
    if (last == id) { *count = records; return crc == checksum; }
    char header[100];
    int n = snprintf(header, sizeof(header), "SCREJ1 %016llx %d %u %08x\n",
        (unsigned long long)id, status, (unsigned)length, (unsigned)checksum);
    if (n < 0 || (size_t)n >= sizeof(header) || (size_t)used + n + length + 1 > REJECT_MAX)
        return false;
    file = fopen(path, "ab"); if (!file) return false;
    bool ok = fwrite(header, 1, n, file) == (size_t)n &&
        fwrite(payload, 1, length, file) == length && fputc('\n', file) != EOF;
    if (fflush(file) != 0 || fsync(fileno(file)) != 0) ok = false;
    if (fclose(file) != 0) ok = false;
    if (ok) *count = records + 1;
    return ok;
}
