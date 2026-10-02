# Logging

## The log file

Everything keepwatch does is written to `$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl`, one JSON object per line. When the file would grow past `max_bytes` (default 10 MB) it is rotated to `keepwatch.jsonl.1`, `.2`, … keeping `backups` (default 10) old files. Rotation settings are read when the service starts.

Every record has `ts` (local time, RFC 3339 with offset), `level` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`), `event` and `pid`, and, where they apply, `watch`, `poll_id` and `hook`. All records of one poll share its `poll_id`.

## Events

| Event | Fields |
|---|---|
| `service.start` / `service.stop` | `version`, `config`, `watch_dirs`, `only` |
| `config.loaded` / `config.error` | `path`; `error` (with file and line); `watch` and `running_previous` for a watch's config |
| `watch.added` / `watch.changed` / `watch.removed` | `watch` |
| `poll.start` | `condition`, `faked`, `dry_run`, `trial`, `events` (how many observer events the poll got) |
| `command` | from `ctx.run`: `argv`, `shell`, `exit_code`, `timed_out`, `duration`, `stdout`, `stderr`, `*_truncated` |
| `plugin.log` | from `ctx.log`: `level`, `logger`, `message`, `fields`, `traceback` |
| `hook.end` | `hook`, `kind`, `target`, `status`, `reason`, `payload`, `exit_code`, `signal`, `exception` (type, message, traceback), `stdout`, `stderr`, `*_truncated`, `duration` |
| `check.outcome` | `outcome`, `reason`, `payload`, `condition_before`, `condition_after`, `pending_edge`, `actions`, `faked` |
| `poll.end` | `failed`, `failures`, `condition`, `pending_edge`, `dry_run` |
| `watch.offline` / `watch.online` | `reason`, `by_user`, `last_failure` |
| `watch.crash` | `error`, `traceback` (a bug in keepwatch itself; the watch retries after a minute) |
| `observer.started` | `observer`, `kind`; `argv` and `pid` (command) or `path`, `native`, `native_error` (files) |
| `observer.connected` | `observer`, `remote`, `dir`, `python`, `version`, `inotify`: a remote_files observer's ssh connection is up |
| `observer.event` | DEBUG: `observer`, `data` (the event as the source produced it), or `data_clipped` (its JSON, shortened) when that is over 4 KiB |
| `observer.output` | `observer`, `stream`, `text`; `suppressed` on the record counting lines over the limit |
| `observer.stopped` / `observer.restarting` | `observer`; `reason`, `exit_code`, `duration`, `stderr_tail` / `delay` |
| `observer.dropped` | `observer`, `dropped` (total so far), `cap`, `max_bytes`: the event queue was full |
| `observer.crash` | `observer`, `error`, `traceback` (a bug in keepwatch itself; the observer restarts) |
| `alert.end` | `alert_event`, `command`, `status`, `exit_code`, `reason`, `stdout`, `stderr`, `duration` |

For `hook.end`, `status` is the outcome for a check (`true`, `false`, `unknown`, `timeout`, `error`) and `ok`, `failed` or `timeout` for an action. Captured output keeps the first and last half of `capture_bytes` (default 64 KiB) per stream.

## Logging from a watch

`ctx.log` is a standard `logging.Logger`. Its records become `plugin.log` records tagged with the watch, hook and poll ID, and they appear while the hook runs. Keys passed with `extra=` become JSON fields you can search for:

```python
ctx.log.info("sent %s", path, extra={"file": path, "bytes": size})
ctx.log.exception("upload failed")     # inside an except block: includes the traceback
```

Command hooks log by writing to stdout or stderr; that output lands in the `hook.end` record.

## Reading the log

```sh
keepwatch logs                         # the last 200 records
keepwatch logs psg-export --failed     # failures of one watch
keepwatch logs --poll 3fa9 -v          # one poll, with all captured output
keepwatch logs --since 2h --event hook # every hook.* record of the last two hours
keepwatch logs -f                      # follow new records
keepwatch logs --json                  # the raw records, one JSON object per line
```

`--since`/`--until` take a duration ago (`1h`, `2d`) or a time (`2026-09-30`, `2026-09-30T14:00`). `--failed` selects ERROR-or-worse records, failed hooks, failed polls and failed alerts. `-n` limits the output to the last N matching records (`0` for all).

`keepwatch run` also prints records to its terminal: everything from INFO up on a terminal, only WARNING and up when stdout is not a terminal (for example under systemd, where it goes to the journal), every record with `-v`.
