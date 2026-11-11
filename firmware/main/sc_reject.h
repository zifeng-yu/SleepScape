#ifndef SC_REJECT_H
#define SC_REJECT_H
#include <stdbool.h>
#include <stdint.h>
bool sc_reject_count(const char *path, uint32_t *count);
bool sc_reject_append(const char *path, uint64_t queue_id, int status,
                      const char *payload, uint32_t *count);
#endif
