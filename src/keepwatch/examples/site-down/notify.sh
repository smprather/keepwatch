#!/bin/sh
# Report a change. Replace the echo with notify-send, mail or a chat webhook; output goes to the log.
set -eu
echo "$KEEPWATCH_SETTING_URL $1 (watch $KEEPWATCH_WATCH, poll $KEEPWATCH_POLL_ID)"
