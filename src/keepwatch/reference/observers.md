# Observers: events instead of (or as well as) polling

A watch polls on a schedule. Some things are better learned the moment they happen: a file landing in a directory, a line appearing in a log, a message from a long-running ssh session. An **observer** is an event source the service keeps running for a watch, independently of its polling schedule. Its events are queued and handed to the watch's next poll, and by default an event makes that poll happen at once.

The check still decides. It sees the pending events in `ctx.events`, turns them into its answer and payload, and the actions run as usual. Everything else (state, retries, backoff, offline, logging) works as for any watch.

## Declaring observers

One `[observe.<name>]` table per observer in the watch's config.toml. Names use letters, digits, `_` and `-`. Every key is listed in `keepwatch docs config`.

```toml
[observe.inbox]
kind = "files"              # a local directory
path = 'C:/data/inbox'      # relative paths are relative to the watch directory
pattern = "*.tar.gz"
settle = "10s"

[observe.feed]
kind = "command"            # a long-running program
command = ["./feed.py"]     # see "Python programs" below
heartbeat_timeout = "2m"
wake = true                 # the default: poll as soon as an event arrives
```

### Python programs

Write `command = ["./feed.py"]`, not `["python", "feed.py"]`. A relative `.py` program runs with the Python that runs keepwatch: on Windows keepwatch starts it with that interpreter, and on POSIX the script's `#!` line does (make it executable). A bare `python` is unreliable: on Windows it may be the Microsoft Store placeholder, and elsewhere it may be missing or a different version. If the program needs third-party packages, declare them in the script itself (PEP 723 inline metadata) and run it with `command = ["uv", "run", "--script", "./feed.py"]`. uv then picks an interpreter and builds a cached environment on every OS.

### kind = "command"

`command` runs as a long-lived child process: working directory the watch directory, in its own process group (POSIX) or Job Object (Windows), with the hooks' environment minus the per-poll variables (`KEEPWATCH_WATCH`, `KEEPWATCH_OBSERVER`, `KEEPWATCH_WATCH_DIR`, `KEEPWATCH_DATA_DIR` are set; `PYTHONUNBUFFERED=1` too). With `stdin`, that text is written to its standard input once, then stdin is closed; otherwise stdin is empty.

- **Each line it prints on stdout is one event.** A line holding a JSON object is used as-is; any other line becomes `{"line": "<text>"}`. Lines longer than 1 MiB are dropped.
- **Flush stdout after every line.** Programs writing to a pipe often buffer output in blocks, so events would arrive late or in bursts (`print(..., flush=True)`, `stdbuf -oL`, `sys.stdout.flush()`).
- `{"event": "heartbeat"}` lines are not delivered; they only prove the program is alive. With `heartbeat_timeout`, a program that prints nothing (heartbeats included) for that long is stopped and restarted.
- stderr lines are logged as `observer.output` records (at most 20 a minute; the rest are counted). The last 5 are also on the `observer.stopped` record.
- When the program exits (or is stopped for silence) it is restarted after 5s, then 10s, 20s … up to 5 minutes; a run that lasted 5 minutes resets the delay to 5s.

### kind = "files"

Observes a local directory (`recursive = true` for subdirectories too) through the operating system's notifications (inotify, ReadDirectoryChangesW, FSEvents), rescanning on every notification and at least every 30 seconds. When notifications are unavailable (some network shares), it rescans every 2 seconds and says so on `observer.started` (`native = false`).

A file is reported once it has **settled**: its size and modification time have not changed for `settle` (default 10s) and it was last modified at least `settle` ago. The first condition catches copies that preserve an old modification time (`cp -p`, `scp -p`, tar). Names must match `pattern` and none of `ignore` (default `.*`, `*.tmp`, `*.part`, `*~`: hidden files and files still being written by tools that rename when done).

Each settled file is reported once per (path, size, modification time) while the service runs:

```json
{"event": "file", "path": "/data/inbox/a.tar.gz", "name": "a.tar.gz", "size": 1048576, "mtime": 1790000000.5, "observer": "inbox", "received": "2026-10-01T14:00:03.120+02:00"}
```

A missing directory is retried with the same backoff as a command, so an observer may be configured before its directory exists.

## What hooks receive

- **Python:** `ctx.events`, a list of dicts, oldest first. Empty when there are none.
- **Commands:** `KEEPWATCH_EVENTS_FILE`, a JSON file holding the array (`[]` when there are none).

Every event carries `"observer"` (the observer's name) and `"received"` (when keepwatch got it), which replace keys of the same name. All hooks of a poll (check and actions) see the same events; the check normally passes what the actions need on in its payload.

```python
def check(ctx):
    pulled = ctx.ledger("done")
    new = [e for e in ctx.events if e.get("event") == "file" and ctx.file_key(e["path"]) not in pulled]
    return bool(new), [e["path"] for e in new]


def on_true(ctx):
    done = ctx.ledger("done")
    for path in ctx.payload:
        ctx.run(["process", path])
        done.add(ctx.file_key(path))
```

## Delivery rules

- Events wait in a per-watch queue (at most 10 000; when full the oldest are dropped with an `observer.dropped` WARNING).
- **Events are acknowledged only by a successful poll:** one that did not fail and answered TRUE or FALSE. After a failed or unknown poll the same events (plus newer ones) come again: delivery is **at-least-once**. Record what you handled in a ledger and skip it next time, as above.
- With `wake = true` an event makes the watch poll at once, unless it is backing off after failures or is offline (then it waits for its schedule). With `wake = false` events just wait for the next scheduled poll.
- Observers start with their watch and stop when it is removed, parked (`enabled = false`), disabled or offline; any change to the watch's config restarts them. Events still queued when a watch goes offline are delivered to its trial polls.
- A **files** event can be stale by the time the hook runs (the file was moved or deleted); check that it still exists.

## Developing observers

- `keepwatch observe <watch> [<observer>] --for 30s` runs the observers in the foreground and prints each event as one JSON line on stdout, exactly as hooks receive it; observer records go to stderr. Agents: always pass `--for` or `--count`.
- `keepwatch poll <watch> --events '[{"event": "file", "path": "/tmp/a.gz", "name": "a.gz"}]'` hands events to a manual poll (manual polls run no observers, so `ctx.events` is otherwise empty).
- `keepwatch status --json` shows each watch's `pending_events` and its `observers` (running, restarts, last event).
- Records: `observer.started`, `observer.event` (DEBUG, every event), `observer.output`, `observer.stopped` (`reason`, `exit_code`, `stderr_tail`), `observer.restarting` (`delay`), `observer.dropped`, `observer.crash` (a bug in keepwatch). `keepwatch logs <watch> --event observer.stopped` shows why an observer keeps restarting.
