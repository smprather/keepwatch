# Failures, backoff and going offline

## What fails a poll

- An action fails (nonzero exit, exception, or `action_timeout`).
- The check times out or errors.

An `unknown` outcome neither fails nor succeeds a poll. A poll in which the check answered and every action that ran succeeded resets the failure count to 0. Check failures count on purpose: a broken check never runs an action, so an action-only limit would let a broken watch do nothing forever without anyone being told.

## Backoff

After the k-th consecutive failed poll, the wait before the next poll is

```
min(max(interval, 60s) * 2^(k-1), max(interval, 1h))
```

With the default `max_failures = 5` the waits are 1m, 2m, 4m and 8m, so a watch goes offline after about 15 minutes of continuous failure whatever its interval. The slow retry also keeps a failing `scp` from producing a burst of failed ssh logins, which tools like fail2ban punish with an IP ban.

## Going offline

When the failure count reaches `max_failures` (default 5; `0` means never):

- the watch stops polling;
- `offline.json` is written to its state directory (`$XDG_STATE_HOME/keepwatch/watches/<name>/offline.json`) with the reason, the time and a summary of the last failure, so restarting the service does not quietly resume it;
- a `watch.offline` record is logged at CRITICAL;
- `alert_command` runs.

## Coming back online

- `keepwatch enable NAME` deletes `offline.json`. A running service notices within a second, resets the failure count and polls at once.
- With `retry_after` set (for example `retry_after = "1h"`), an offline watch makes one **trial poll** every `retry_after`. A trial poll that succeeds (the check answers `true` or `false` and every action that runs succeeds) brings the watch back online. Any other trial poll, including one answering `unknown`, leaves it offline until the next trial. Set `retry_after` on watches that fail for outside reasons, like a laptop being off the network.
- `keepwatch disable NAME` takes a watch offline by hand (reason "disabled by user"); such a watch makes no trial polls and stays offline until `keepwatch enable`.

A poll cut short because the service is stopping never takes a watch offline.

## alert_command

Set `alert_command` in the global config to be told when a watch goes offline or comes back online:

```toml
alert_command = 'notify-send "keepwatch: $KEEPWATCH_WATCH $KEEPWATCH_ALERT_EVENT" "$KEEPWATCH_ALERT_REASON"'
```

It receives `KEEPWATCH_ALERT_EVENT` (`offline` or `online`), `KEEPWATCH_WATCH` and `KEEPWATCH_ALERT_REASON`, plus the global `[environment]`. A string runs through `/bin/sh -c`; a list runs directly. Its timeout is `action_timeout` from `[defaults]` (default 60s). Each run is logged as an `alert.end` record; its failures never count against any watch.
