#include "sc_reject.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(void) {
    char directory[] = "/tmp/sleepscape-reject-XXXXXX";
    assert(mkdtemp(directory));
    char path[256]; snprintf(path, sizeof(path), "%s/reject", directory);
    uint32_t count = 999;
    assert(sc_reject_count(path, &count) && count == 0);
    assert(!sc_reject_append(path, 0, 409, "{}", &count));
    assert(!sc_reject_append(path, 1, 200, "{}", &count));
    assert(sc_reject_append(path, 1, 409, "{\"device_id\":\"synthetic\"}", &count));
    assert(count == 1);
    assert(sc_reject_append(path, 1, 409, "{\"device_id\":\"synthetic\"}", &count));
    assert(count == 1); /* lost local retirement confirmation is idempotent */
    assert(!sc_reject_append(path, 1, 409, "{\"different\":true}", &count));
    assert(sc_reject_count(path, &count) && count == 1);
    FILE *file = fopen(path, "ab"); assert(file); fputs("SCREJ1 torn", file); fclose(file);
    assert(!sc_reject_count(path, &count));
    assert(!sc_reject_append(path, 2, 422, "{}", &count));
    unlink(path);
    char payload[8001]; memset(payload, 'x', sizeof(payload) - 1); payload[8000] = 0;
    for (uint64_t n = 1; n <= 8; ++n) assert(sc_reject_append(path, n, 422, payload, &count));
    assert(!sc_reject_append(path, 9, 422, payload, &count));
    assert(sc_reject_count(path, &count) && count == 8);
    file = fopen(path, "r+b"); assert(file); fseek(file, 100, SEEK_SET); fputc('z', file); fclose(file);
    assert(!sc_reject_count(path, &count));
    unlink(path); rmdir(directory);
    puts("PASS quarantine durability, duplicates, bounds, truncation and CRC corruption");
    return 0;
}
