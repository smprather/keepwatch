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

### kind = "remote_files"

Watches a directory on another host through **one long-lived outbound ssh connection**. Nothing is installed there: keepwatch sends its remote watcher (one Python file) over ssh's standard input to the remote host's Python 3.6 or newer, which runs it from memory.

```toml
[observe.remote]
kind = "remote_files"
remote = "me@linux1"           # or a Host alias from ~/.ssh/config
dir = "/data/out"              # remote path; ~ and relative paths start at the remote home
pattern = "*.tar.gz"
settle = "30s"
```

- **What it reports:** the same file events as `files`, plus `"sha256"` (computed remotely; `checksum = false` turns it off) and `"remote"`. Every settled file is reported once per connection, **including everything already there when it connects**, so after a reconnect it catches up; handled files must be recorded in a ledger.
- **How it decides:** the same settle rules as `files`. Where the host has inotify (used directly, without inotify-tools) it rescans at once on every change and every 30s as a safety net; without inotify it rescans every `interval` (2s). Files whose names are not valid UTF-8 are skipped, and files the remote user cannot read are retried every 30s; both are noted once in an `observer.output` record.
- **Liveness:** the watcher prints a heartbeat after `heartbeat` (30s) without output; a connection that delivers nothing for `heartbeat_timeout` (default 3 × `heartbeat`) is closed and reopened. ssh also runs with `ServerAliveInterval=15` and `ConnectTimeout=15`, and every disconnect is retried with the observer backoff (5s up to 5 minutes).
- **The ssh command:** `ssh [ssh_options] -T -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ControlMaster=no -o ControlPath=none [-p port] [-i identity] -- <remote> <python> -u -`. Connection sharing is off on purpose (a `ControlPersist` master in `~/.ssh/config` would outlive the connection keepwatch stops). `observer.started` shows the command in full.
- **Which Python:** `remote_python = "auto"` runs `/usr/bin/python3` when it exists (the system interpreter, which on EL8 is 3.6), else `python3` from the remote PATH. Name another one when needed: `remote_python = "/opt/python3.11/bin/python3"`. Python 2 does not work.
- **Login scripts:** any shell works (sh, bash, csh, tcsh): the watcher's options travel inside the program text, not through shell quoting. Lines a login script prints on stdout are logged as `observer.output`, never delivered.

**Requirements on the keepwatch host.** BatchMode means ssh never asks anything, so:

1. **Key authentication.** A key without a passphrase (`identity = '~/.ssh/id_relay'`), or a key held by ssh-agent. On Windows the OpenSSH Authentication Agent service works for the service too; on Linux the service only sees an agent through `SSH_AUTH_SOCK` in `[environment]` (see `keepwatch docs environment`). With `identity`, ssh offers only that key (IdentitiesOnly=yes), so a crowded agent cannot use up the server's login attempts.
2. **A known host key.** Connect once by hand (`ssh me@linux1 true`) to put the key in `known_hosts`, or the connection fails with "Host key verification failed".
3. **Check it the way the service will:** `ssh -o BatchMode=yes me@linux1 true` must succeed without a prompt, then `keepwatch observe <watch> remote --for 1m` must print the files.

When it does not connect, `keepwatch logs <watch> --event observer.stopped` shows ssh's last error lines (`stderr_tail`); `observer.connected` records each successful connection with the remote Python version and whether inotify is used.

## What hooks receive

- **Python:** `ctx.events`, a list of dicts, oldest first. Empty when there are none.
- **Commands:** `KEEPWATCH_EVENTS_FILE`, a JSON file holding the array (`[]` when there are none).

Every event carries `"observer"` (the observer's name) and `"received"` (when keepwatch got it), which replace keys of the same name. All hooks of a poll (check and actions) see the same events; the check normally passes what the actions need on in its payload.

```python
def check(ctx):
    done = ctx.ledger("done")
    new = []
    for event in ctx.events:
        if event.get("event") != "file":
            continue
        try:
            key = ctx.file_key(event["path"])
        except FileNotFoundError:
            continue  # moved or deleted since it was reported: nothing to do
        if key not in done:
            new.append(event["path"])
    return bool(new), new


def on_true(ctx):
    done = ctx.ledger("done")
    for path in ctx.payload:
        ctx.run(["process", path])
        done.add(ctx.file_key(path))
```

## Delivery rules

- Events wait in a per-watch queue (at most 10 000 events and 64 MiB; when full the oldest are dropped with an `observer.dropped` WARNING).
- **Events are acknowledged only by a successful poll:** one that did not fail and answered TRUE or FALSE. After a failed or unknown poll the same events (plus newer ones) come again: delivery is **at-least-once**. Record what you handled in a ledger and skip it next time, as above.
- With `wake = true` an event makes the watch poll at once, but no sooner than 1 second after the previous poll ended, so a busy source is handled in batches; not at all while the watch is backing off after failures or is offline (then it waits for its schedule). With `wake = false` events just wait for the next scheduled poll.
- Observers start with their watch and stop when it is removed, parked (`enabled = false`), disabled or offline. A change to the `[observe.*]` tables, `shell`, `[environment]`, `[settings]` or the global `[environment]` restarts them; other changes (`interval`, hooks, timeouts) do not restart them. Events still queued when a watch goes offline are delivered to its trial polls.
- **An event that makes a hook fail is delivered again on every poll** (it is never dropped), so the watch keeps failing until the hook is fixed and goes offline after `max_failures`. Skip events you cannot act on instead of raising, as the example above does for files that have disappeared.
- A **files** event can be stale by the time the hook runs (the file was moved or deleted); check that it still exists.

## Developing observers

- `keepwatch observe <watch> [<observer>] --for 30s` runs the observers in the foreground and prints each event as one JSON line on stdout, exactly as hooks receive it; observer records go to stderr. Agents: always pass `--for` or `--count`.
- `keepwatch poll <watch> --events '[{"event": "file", "path": "/tmp/a.gz", "name": "a.gz"}]'` hands events to a manual poll (manual polls run no observers, so `ctx.events` is otherwise empty).
- `keepwatch status --json` shows each watch's `pending_events` and its `observers` (running, restarts, last event).
- Records: `observer.started`, `observer.event` (DEBUG, every event), `observer.output`, `observer.stopped` (`reason`, `exit_code`, `stderr_tail`), `observer.restarting` (`delay`), `observer.dropped`, `observer.crash` (a bug in keepwatch). `keepwatch logs <watch> --event observer.stopped` shows why an observer keeps restarting.
