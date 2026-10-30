# Logging

## The log file

Everything watchglass does is written to `$XDG_STATE_HOME/watchglass/logs/watchglass.jsonl`, one JSON object per line. When the file would grow past `max_bytes` (default 10 MB) it is rotated to `watchglass.jsonl.1`, `.2`, … keeping `backups` (default 10) old files. Rotation settings are read when the service starts.

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
| `plugin.event` | from `ctx.event`: `level`, `message`, `fields` — a plain-language headline |
| `hook.end` | `hook`, `kind`, `target`, `status`, `reason`, `permanent_reason`, `payload`, `exit_code`, `signal`, `exception` (type, message, traceback), `stdout`, `stderr`, `*_truncated`, `duration` |
| `check.outcome` | `outcome`, `reason`, `payload`, `condition_before`, `condition_after`, `pending_edge`, `actions`, `faked` |
| `poll.end` | `failed`, `failures`, `rejections`, `permanent_reason`, `condition`, `pending_edge`, `dry_run` |
| `watch.offline` / `watch.online` | `reason`, `by_user`, `permanent`, `last_failure` |
| `watch.crash` | `error`, `traceback` (a bug in watchglass itself; the watch retries after a minute) |
| `observer.started` | `observer`, `kind`; `argv` and `pid` (command) or `path`, `native`, `native_error` (files) |
| `observer.connected` | `observer`, `remote`, `dir`, `python`, `version`, `inotify`: a remote_files observer's ssh connection is up |
| `observer.event` | DEBUG: `observer`, `data` (the event as the source produced it), or `data_clipped` (its JSON, shortened) when that is over 4 KiB |
| `observer.output` | `observer`, `stream`, `text`; `suppressed` on the record counting lines over the limit |
| `observer.stopped` / `observer.restarting` | `observer`; `reason`, `exit_code`, `duration`, `stderr_tail` / `delay` |
| `observer.rejected` | `observer`, `reason` (`authentication failed` or `host key verification failed`), `stderr_tail`: the source's login was rejected (see `watchglass docs failures`) |
| `observer.dropped` | `observer`, `dropped` (total so far), `cap`, `max_bytes`: the event queue was full |
| `observer.crash` | `observer`, `error`, `traceback` (a bug in watchglass itself; the observer restarts) |
| `alert.end` | `alert_event`, `command`, `status`, `exit_code`, `reason`, `stdout`, `stderr`, `duration` |

For `hook.end`, `status` is the outcome for a check (`true`, `false`, `unknown`, `timeout`, `error`) and `ok`, `failed` or `timeout` for an action. Captured output keeps the first and last half of `capture_bytes` (default 64 KiB) per stream.

## Logging from a watch

`ctx.log` is a standard `logging.Logger`. Its records become `plugin.log` records tagged with the watch, hook and poll ID, and they appear while the hook runs. Keys passed with `extra=` become JSON fields you can search for:

```python
ctx.log.info("sent %s", path, extra={"file": path, "bytes": size})
ctx.log.exception("upload failed")     # inside an except block: includes the traceback
```

Command hooks log by writing to stdout or stderr; that output lands in the `hook.end` record.

## Headlines

`ctx.log` is for detail. A **headline** is the story the operator should read without knowing the watch's
internals: "found export-1.tar.gz (4.1 GB) on relay1", "pulled it in 2m10s", "swept 1 file(s) from relay1".
`ctx.event(message, **fields)` writes one (`plugin.event`):

```python
ctx.event("found %s (%s) on %s" % (name, size, host), name=name, size=size, host=host)
ctx.event("pull failed: %s" % exc, level="WARNING", name=name)
```

Headlines are plain lines in `watchglass tui`'s pane at its normal level. A headline at `WARNING` or above is
shown even at the quiet level, so failures use that level. Keys become JSON fields, searchable with
`watchglass logs --json`. The built-in `pull` and `push` recipes headline their own story points.

## Reading the log

```sh
watchglass logs                         # the last 200 records
watchglass logs psg-export --failed     # failures of one watch
watchglass logs --poll 3fa9 -v          # one poll, with all captured output
watchglass logs --since 2h --event hook # every hook.* record of the last two hours
watchglass logs -f                      # follow new records
watchglass logs --json                  # the raw records, one JSON object per line
```

`--since`/`--until` take a duration ago (`1h`, `2d`) or a time (`2026-09-30`, `2026-09-30T14:00`). `--failed` selects ERROR-or-worse records, failed hooks, failed polls and failed alerts. `-n` limits the output to the last N matching records (`0` for all).

`watchglass run` also prints records to its terminal: everything from INFO up on a terminal, only WARNING and up when stdout is not a terminal (for example under systemd, where it goes to the journal), every record with `-v`.
