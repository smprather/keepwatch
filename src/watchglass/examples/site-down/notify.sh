#!/bin/sh
# Report a change. Replace the echo with notify-send, mail or a chat webhook; output goes to the log.
set -eu
echo "$WATCHGLASS_SETTING_URL $1 (watch $WATCHGLASS_WATCH, poll $WATCHGLASS_POLL_ID)"
