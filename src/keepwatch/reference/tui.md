# The terminal monitor (`keepwatch tui`)

`keepwatch tui` is a live, read-only view of what the service is doing: every watch with its state and how long
until its next poll, and the stream of actions with their results. It reads `status.json` once a second and
follows the log file. It never writes anything, so it is safe beside a running service, and several monitors may
run at once.

## What it shows

- **The banner**: whether the service is running, its pid and version, and how old the status is. When the
  service is not running — or its status is more than a few seconds old — the age is marked stale, `NEXT` shows
  `—` instead of a countdown, and the rest is the last known status.
- **The table**: one row per watch. `WATCH`; `STATE` (`online`, `polling`, `offline`, `parked`, `idle`,
  `invalid`, `orphaned`); `COND`; `FAIL`; `LAST` (outcome and age); `NEXT` (a countdown, `due`, or `retry …` for
  an offline watch); `OBS` (running observers); and `NOTE`, the first thing that is wrong: an invalid config, a
  config error, a missing directory, orphaned state, queued events, a user-disabled watch.
- **The pane**: the selected watch's recent records, oldest first — the same lines `keepwatch logs` prints.
  While a poll is in flight the pane header names it and how long it has been running.

Below 80 columns `NOTE` is dropped, and then `OBS`.

## Keys

| Key | What it does |
| --- | --- |
| `q`, `Ctrl-C` | quit |
| `↑`/`↓`, `j`/`k` | select a watch |
| `a` | every watch's records instead of the selected one's |
| `v` | verbose: captured stdout/stderr and payloads |
| `space` | pause the pane (records keep arriving; the header counts them) |
| `r` | re-read the config now (new and removed watches) |

The mouse works too: a click selects a row, and the wheel scrolls the pane.

## What it is not

It cannot change anything: no enable, no disable, no poll now. It keeps the last few hundred records in memory
and does not browse history (use `keepwatch logs` for that), and it needs a terminal: scripts and agents use
`keepwatch status --json` and `keepwatch logs --json`, which are the machine-readable interface.

## When a poll is in flight

An action that runs for a long time (a large transfer, say) writes no log record until it ends, so the monitor
marks the watch `polling` from the poll's own `poll.start` record, shows how long it has been running and which
of its hooks have already finished. It believes that only while the service runs with a fresh status: when the
service stops or restarts, the mark goes away.
