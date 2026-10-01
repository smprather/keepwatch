#!/bin/sh
# TRUE (exit 0) until the greeting has been done; the action leaves a marker in the data directory.
if [ -e "$KEEPWATCH_DATA_DIR/greeted" ]; then
    exit 1
fi
exit 0
