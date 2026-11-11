#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
mkdir -p build/firmware-tests
# Redirect directory reads in this host-only object to exercise real I/O failure
# handling without changing the production component or firmware build.
"${CC:-clang}" -std=c11 -D_POSIX_C_SOURCE=200809L -D_DARWIN_C_SOURCE -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer -g \
  -Ifirmware/components/sc_core/include -Dreaddir=sc_test_readdir \
  -c firmware/components/sc_core/sc_queue.c -o build/firmware-tests/sc_queue.o
"${CC:-clang}" -std=c11 -D_POSIX_C_SOURCE=200809L -D_DARWIN_C_SOURCE -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer -g \
  -Ifirmware/components/sc_core/include \
  firmware/components/sc_core/sc_core.c firmware/components/sc_core/sc_record.c \
  build/firmware-tests/sc_queue.o firmware/components/sc_core/sc_aggregate.c \
  firmware/tests/test_core.c \
  -lm -o build/firmware-tests/test_core
build/firmware-tests/test_core

"${CC:-clang}" -std=c11 -D_POSIX_C_SOURCE=200809L -D_DARWIN_C_SOURCE -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer -g \
  -Ifirmware/components/sc_core/include -Ifirmware/main \
  firmware/components/sc_core/sc_core.c firmware/components/sc_core/sc_record.c \
  firmware/main/sc_reject.c firmware/tests/test_reject.c \
  -lm -o build/firmware-tests/test_reject
build/firmware-tests/test_reject

"${CC:-clang}" -std=c11 -D_POSIX_C_SOURCE=200809L -D_DARWIN_C_SOURCE -Wall -Wextra -Werror \
  -fsanitize=address,undefined -fno-omit-frame-pointer -g \
  -Ifirmware/components/sc_core/include \
  firmware/components/sc_core/sc_core.c firmware/components/sc_core/sc_record.c \
  firmware/components/sc_core/sc_aggregate.c firmware/components/sc_core/sc_veml7700.c \
  firmware/tests/test_veml7700.c -lm -o build/firmware-tests/test_veml7700
build/firmware-tests/test_veml7700
