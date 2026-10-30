# Failures, backoff and going offline

## What fails a poll

- An action fails (nonzero exit, exception, or `action_timeout`).
- The check times out or errors.

An `unknown` outcome neither fails nor succeeds a poll. A poll in which the check answered and every action that ran succeeded resets the failure count to 0. Check failures count on purpose: a broken check never runs an action, so an action-only limit would let a broken watch do nothing forever without anyone being told.

## Backoff

After the k-th consecutive failed poll, the wait before the next poll is

```text
min(max(interval, 60s) * 2^(k-1), max(interval, 1h))
```

With the default `max_failures = 5` the waits are 1m, 2m, 4m and 8m, so a watch goes offline after about 15 minutes of continuous failure whatever its interval. The slow retry keeps a failing `scp` from producing a burst of failed ssh logins, which tools like fail2ban punish with an IP ban. Spacing is not enough when the credential itself is wrong, though: that case is handled before the backoff (see *Rejected credentials and host keys* below).

## Rejected credentials and host keys

Some failures are permanent. Retrying asks the same server the same question and gets the same no, while every
attempt spends the account's lockout budget (fail2ban, an AD policy). watchglass detects the shapes ssh and scp
print for a rejected login:

```text
Permission denied (publickey,password).
Permission denied (publickey,password,keyboard-interactive).
Permission denied (publickey).
Too many authentication failures
Received disconnect from <host>: ... Too many authentication failures
Host key verification failed
REMOTE HOST IDENTIFICATION HAS CHANGED
Host key for <host> has changed ...
```

A *file-level* `Permission denied` names a path (`/in/a.bin: Permission denied`) and is not one of these; neither
is an outage (`Connection refused`, a timeout, DNS). Those keep the ordinary backoff and trial polls.

The first rejection (`max_auth_failures = 1`, the default; `0` disables this) parks the watch **at once**, even
with `max_failures = 5`:

- it goes offline with `permanent = true` in `offline.json`, and its reason says what to fix, e.g.
  `authentication failed; fix the credential, then: watchglass enable NAME`;
- it makes **no trial polls**, however long it waits: `retry_after` is not used for this state;
- `status` shows the reason with `(permanent: no trial polls)`, the TUI notes that `watchglass enable` is needed,
  and the `watch.offline` record carries `permanent: true`;
- the marker survives a service restart, so a restart does not quietly resume the login attempts;
- `watchglass enable NAME` clears it after you have fixed the credential (or `known_hosts`).

An observer's rejected login is treated the same way: it logs `observer.rejected`, and with `max_auth_failures`
nonzero it is not restarted and the watch is parked, so a `remote_files` source cannot keep retrying on its own.

A larger `max_auth_failures` tolerates that many consecutive rejections from hooks first; an observer's first
rejection parks the watch as soon as the limit is nonzero (it cannot keep trying without repeating the login).
An ordinary failure in between does not reset the count; a poll that succeeds does. The recipes stop their queue
at the first rejection rather than trying the remaining files, so one poll costs one login attempt. In watch code,
`ctx.transfer` raises `watchglass.transfer.TransferAuthFailed` (a `TransferFailed`) and `ctx.run` raises
`CommandFailed` with its `permanent_reason` set; command hooks are classified from the same stderr shapes.

## Trial polls

An offline watch with `retry_after` keeps making trial polls, and their spacing is an accordion of the same
shape: the k-th consecutive failed trial waits

```text
min(retry_after, retry_base * 2^(k-1))
```

With `retry_after = "10m"` and `retry_base = "1m"`: 1m, 2m, 4m, 8m, 10m, 10m, … A blip is retried within a
minute, and a destination that is away for a day is probed minutes apart instead of 144 times. A poll in which
the check answered resets the failure count, and with it the accordion. Without `retry_base` the spacing is
`retry_after` flat, which is what watchglass did before, and a watch taken offline by hand
(`watchglass disable`, or `by_user`) is never probed at all.

## Going offline

When the failure count reaches `max_failures` (default 5; `0` means never):

- the watch stops polling;
- `offline.json` is written to its state directory (`$XDG_STATE_HOME/watchglass/watches/<name>/offline.json`) with the reason, the time and a summary of the last failure, so restarting the service does not quietly resume it;
- a `watch.offline` record is logged at CRITICAL;
- `alert_command` runs.

## Coming back online

- `watchglass enable NAME` deletes `offline.json`. A running service notices within a second, resets the failure count and polls at once.
- With `retry_after` set (for example `retry_after = "1h"`), an offline watch makes one **trial poll** every `retry_after`. A trial poll that succeeds (the check answers `true` or `false` and every action that runs succeeds) brings the watch back online. Any other trial poll, including one answering `unknown`, leaves it offline until the next trial. Set `retry_after` on watches that fail for outside reasons, like a laptop being off the network.
- `watchglass disable NAME` takes a watch offline by hand (reason "disabled by user"); such a watch makes no trial polls and stays offline until `watchglass enable`.

A poll cut short because the service is stopping never takes a watch offline.

## alert_command

Set `alert_command` in the global config to be told when a watch goes offline or comes back online:

```toml
alert_command = 'notify-send "watchglass: $WATCHGLASS_WATCH $WATCHGLASS_ALERT_EVENT" "$WATCHGLASS_ALERT_REASON"'
```

It receives `WATCHGLASS_ALERT_EVENT` (`offline` or `online`), `WATCHGLASS_WATCH` and `WATCHGLASS_ALERT_REASON`, plus the global `[environment]`. A string runs through the platform shell (`/bin/sh -c`, or Windows PowerShell); a list runs directly. Its timeout is `action_timeout` from `[defaults]` (default 60s). Each run is logged as an `alert.end` record; its failures never count against any watch.
