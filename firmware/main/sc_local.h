#ifndef SC_LOCAL_H
#define SC_LOCAL_H
#include <stdbool.h>
#include "cJSON.h"
/* Invoked only by the one USB command task. Returns false for non-USB commands. */
bool sc_local_command(const cJSON *command);
const char *sc_collection_mode(void);
#endif
