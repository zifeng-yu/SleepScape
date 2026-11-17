#ifndef SC_USB_H
#define SC_USB_H
#include "cJSON.h"
#include "sc_queue.h"

#define SC_USB_MAX_ITEMS 4u
#define SC_USB_COMMAND_BYTES 2048u
#define SC_USB_BATCH_ID_BYTES 96u

/* Pure bounded USB queue transport. Caller serializes queue access. No PCM or
 * secrets enter this structure. A batch remains unchanged until exact ACK. */
typedef struct {
    sc_queue_item_t pending[SC_USB_MAX_ITEMS];
    size_t count;
    char batch_id[SC_USB_BATCH_ID_BYTES];
    char *line;
    sc_queue_item_t acknowledged[SC_USB_MAX_ITEMS];
    size_t acknowledged_count;
    char acknowledged_id[SC_USB_BATCH_ID_BYTES];
} sc_usb_transfer_t;

bool sc_usb_identifier(const char *value);
cJSON *sc_usb_parse_command(const char *line);
/* NULL means success; tx->line == NULL then means no pending records. */
const char *sc_usb_prepare(sc_usb_transfer_t *tx, sc_queue_t *queue,
                          const char *device_id);
const char *sc_usb_acknowledge(sc_usb_transfer_t *tx, sc_queue_t *queue,
                              const char *batch_id, const cJSON *ack);
void sc_usb_transfer_clear(sc_usb_transfer_t *tx);

/* Whole-line output also shares libc's stdout lock with ESP-IDF logging. */
void sc_usb_print_line(const char *line);
#endif
