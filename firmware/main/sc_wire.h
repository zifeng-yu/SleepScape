#ifndef SC_WIRE_H
#define SC_WIRE_H
#include "sc_queue.h"

#define SC_WIRE_MAX_ITEMS 12u
#define SC_WIRE_MAX_BYTES 65536u
#define SC_THRESHOLD_PROFILE "relative-sound-60s-10db-6db"

/* Pure protocol adapter. NULL rejects invalid/mixed/oversize batches or allocation failure.
 * The caller owns the returned allocation and releases it with free(). */
char *sc_wire_batch(const sc_queue_item_t *items, size_t count);
/* Acknowledge only a complete, exact identity match after an HTTP 200 response. */
bool sc_wire_ack(const char *response, const sc_queue_item_t *items, size_t count);
#endif
