# keepwatch TUI: a live terminal monitor — design

Date: 2026-10-06
Status: design approved in conversation; written spec awaiting review
Release target: 2026.10.6
Builds on: `2026-09-29-keepwatch-design.md` (the service, `status.json`, the log); reuses `statusview`,
`logquery` and `output` as they are. Nothing in the service changes.

## 1. Purpose

The user wants to *watch* keepwatch work: every watch, how long until its next poll, and the stream of actions
each one takes with their results — "something I can watch, to make sure my watchers are doing what I expect
them to be doing". Today that means tailing `keepwatch logs` in one pane and re-running `keepwatch status` in
another; neither is live, and neither keeps a watch's history next to its state.

`keepwatch tui` is a read-only, human-facing monitor. It shows the same truth `keepwatch status` and
`keepwatch logs` already expose, live, in one screen.

Facts from the user (2026-10-06):

- It must run on **Windows 11** (Alacritty, WezTerm, Windows Terminal), on **Linux**, over **SSH**, and inside
  **tmux/screen**. Legacy `cmd.exe`/conhost is not required, but nothing may require a modern-terminal feature
  that those three Windows terminals lack.
- **ConPTY is not a factor**: it is the layer a terminal uses to host a console program, not something this app
  talks to. The app writes VT sequences and reads keystrokes; Alacritty/WezTerm host it through ConPTY.
- Dependencies are not a concern ("blank check"); **Textual** is wanted as a **hard dependency**.
- A browser front-end was considered and deferred (section 10). The data layer is kept view-agnostic so it stays
  cheap later.

**Non-goals for this release:** control actions (enable/disable/poll now), notifications, filtering, browsing
history beyond what fits in the pane (`keepwatch logs` stays the history tool), any config key, any web server,
any IPC with the service.

## 2. The command

```text
keepwatch tui
```

- Takes **no options** in this release.
- **Lazy import:** the `tui` command imports `keepwatch.tui` inside the command body, so `keepwatch poll`, hooks
  and every other command keep their current startup time and never import Textual.
- **Refuses to start outside a terminal:** if stdin or stdout is not a tty, it fails with the usual `_fail`
  convention and a message pointing at the machine-readable alternatives, e.g.
  `keepwatch tui needs a terminal; use keepwatch status or keepwatch logs --json`.
- **Exit status:** 0 after quitting (`q`, Ctrl-C); 1 when refused (no terminal) or the app cannot start;
  2 for bad usage.
- `Textual >= 8` is added to `[project].dependencies`. `rich` (already imported directly by `cli.py` and
  `output.py`, currently only transitive through rich-click) is declared as a direct dependency in the same
  change.

## 3. Architecture

```text
src/keepwatch/tui/
  __init__.py   run(paths, config_path) -> int     # imports app.py; the only place Textual is imported
  monitor.py    the data layer: the standard library plus existing keepwatch modules, no Textual
  app.py        the Textual App: widgets, bindings, CSS
```

Data flow is one-way and read-only:

```text
status.json ─┐
config dirs ─┼─► tui/monitor.py (Monitor) ─► tui/app.py (Textual App) ─► the screen
JSONL log ───┘
```

- The Monitor **never writes** anything: not state, not logs, not config. Several monitors may run at once, and
  one may run beside the service with no coordination.
- The App renders only Monitor data; it holds no derivation rules of its own.
- The App takes a **Monitor instance** (injected), so tests build one over a temporary tree and drive refreshes
  directly instead of waiting on timers.
- Status is re-read on a **1 s** timer; the record view is refreshed on a **0.25 s** timer, but only redraws when
  new records arrived (the Monitor keeps a version counter); config discovery is re-read every **5 s** and on
  demand with `r`.

## 4. The data layer (`tui/monitor.py`)

```python
class Monitor:
    def __init__(self, paths: Paths, config_path: Path, *, backfill: int = 200,
                 per_watch: int = 300, history: int = 300) -> None
    def start(self) -> None          # backfill, then follow the log in one daemon thread
    def stop(self) -> None
    def refresh(self) -> None        # re-read status.json and (when due) config discovery
    def status(self) -> Status       # service, watches, problems, stale_since
    def records(self, watch: str | None = None, *, verbose: bool = False) -> list[str]
    def in_flight(self) -> dict[str, InFlight]    # watch -> (poll_id, started)
    def version(self) -> int         # bumped on every appended record; the App redraws only when it changes
```

**4.1 Status.** Built from the existing `collect_status(paths, discovery, [])`, then two additions:

- **Last-known merge:** `collect_status` drops the saved status when the service is not running (watches become
  `idle`). The Monitor merges the saved (stale) per-watch entries back in and records `stale_since`, so the
  screen can show the last known picture *labelled with its age* instead of nothing. `stale_since` is set when
  the service is not running, or when `status.updated` is more than 5 s old while it is.
- **Rows for broken things:** `invalid_watches` (broken `config.toml`) and orphaned state directories become
  rows too — a monitor must not hide the thing that is wrong.

A discovery failure (broken *global* config) keeps the last good discovery, and the banner carries the error's
first line (the same way the service reports a global config error while running the previous config).

**4.2 Watch states.** A watch the service knows has one label, precedence: `offline`, `parked`, `polling`,
`idle`, `online`. `offline` before `parked` matches `keepwatch status` exactly: `enabled = false` is parked only
when there is no offline marker, and `known_to_service = false` is idle. `invalid` (broken `config.toml`) and
`orphaned` (state of a watch that no longer exists) are their own rows, built in 4.1, and are never `polling`.

**4.3 In-flight polls.** `status.json` cannot say "polling now": `last_poll.at` is the poll's *end*, and
`next_poll` is null only for parked/offline watches. The log can: a `poll.start` record with no matching
`poll.end` (same `poll_id`) is a poll in flight.

- Tracked per watch from the follower plus the backfill; the state is `polling`.
- Believed only while the service is running and the status is fresh; cleared when the service stops or its
  **pid changes** (a new service means the old open poll is stale).
- A poll that has been running for hours is reported as such — that is true and useful.
- There is no `hook.start` record, so the pane shows only what is certain: the poll id, how long it has run,
  and which hooks of that poll have already finished.

**4.4 Records.** The last **200** records are read once at start with the same code path as
`keepwatch logs -n 200` (`select_records`), then `logquery.follow_log` streams new ones. Records are held as raw
dicts in bounded deques — **300 per watch** and **300 overall** — and formatted on read with the existing
`output.format_record`, so the verbose toggle applies retroactively and nothing is duplicated. Backfill buckets
each record by its `watch` field; records without one go to the global deque only.

**4.5 Threading.** One daemon thread runs `follow_log` and appends under a lock; `stop()` sets the stop event
and joins briefly. Nothing else in the Monitor blocks: `refresh()` is a small atomic JSON read plus a directory
listing when due.

## 5. The screen

```text
 keepwatch  service running (pid 4711, 2026.10.6) · status 1s ago
──────────────────────────────────────────────────────────────────────────────
  WATCH             STATE    COND    FAIL  LAST            NEXT      OBS   NOTE
▸ relay-pull        polling  FALSE      0  ok 12s ago      due       2/2
  backup-nightly    online   TRUE       0  ok 1m ago       2m30s     1/1
  feed-files        offline  FALSE      3  FAILED 5m ago   retry 1m  —
  old-thing         parked   —          0  —               —         —     by user
  broken            invalid  —          —  —               —         —     invalid: line 4 …
──────────────────────────────────────────────────────────────────────────────
 relay-pull — every 30s · 2 observers · /home/me/.config/keepwatch/watches/relay-pull
 10:11:12 relay-pull poll p1 start: condition FALSE
 10:11:14 relay-pull on_rise → ok (0.31s) watch.py:on_rise
 10:11:12 relay-pull poll p1 running 4s — 1 hook done (on_rise ok)
 10:11:16 relay-pull   $ scp -O … → exit 0 (1.2s)
──────────────────────────────────────────────────────────────────────────────
 q quit   ↑↓ select   a all watches   v verbose   space pause   r rescan
```

- **Banner:** service state, pid and version, and the age of the status it comes from. When stale: `service NOT
  running · last status 2h 13m ago (stale)`. A global config error appends its first line.
- **Stale data is labelled, and never counts down:** while the status is stale, `NEXT` shows `—` (a countdown
  only means something with a live service) and the table is dimmed; `COND`, `LAST` and `FAIL` keep their
  last-known values, with the banner carrying the age.
- **Table:** one row per watch; columns as above. `LAST` is the outcome word plus age; `NEXT` is a live
  countdown, `due` when the deadline has passed, `retry …` for an offline watch with `retry_after`, `—` when the
  watch will not poll by itself. `NOTE` carries the first applicable of: `invalid: …`, `config error: …`,
  `missing directory`, `orphaned state`, `+N events`, `offline by user`. Below 80 columns `NOTE` is dropped
  first, then `OBS`; the screen is designed for at least 80×24.
- **Pane:** the selected watch's records (or every watch's, with `a`), newest at the bottom, with a header line
  naming the watch, its interval, its observers and its watch directory. While a poll is in flight the pane
  shows the extra line from 4.3. Failures and `OFFLINE` lines are emphasised; levels use the same styles as
  `output.LEVEL_STYLES`.
- **Keys:** `q`/Ctrl-C quit · `↑`/`↓`/`j`/`k` select · `a` all watches vs the selection · `v` verbose (captured
  stdout/stderr and payloads) · `space` pause the pane (records keep buffering; the header shows `paused · +N
  new`) · `r` rescan config now. Mouse: click selects, wheel scrolls. The footer is Textual's binding footer.
- The screen is a pure view: no key writes anything, ever.

## 6. Behaviour rules

| Situation | What the screen does |
| --- | --- |
| Service stopped | Banner says NOT running and the status age; table shows the last snapshot, dimmed, plus offline markers from disk. Rows read `idle` (no live service), never `polling`. |
| Service restarts (new pid) | Open polls are cleared; rows return to `online`/`idle` from the new status. |
| Watch added/removed while running | Rows follow config discovery within 5 s, or immediately with `r`. |
| No watches at all | Empty table; problems (a missing watches directory, a broken global config) appear in the banner. |
| Broken watch config | A row with state `invalid` and the first error line in `NOTE`; it is never `polling`. |
| Broken global config | Previous discovery kept; the banner carries the first error line. |
| Orphaned state directory | A row with state `orphaned`; `NOTE` suggests `keepwatch rename`. |
| Log rotated or a window missed | `follow_log` handles rotation; a gap is a gap in the pane, never an error or a crash. |
| No log file yet | Empty pane; the follower waits for the file to appear. |
| Many records | Bounded deques; the pane is a window on the last 300, never unbounded memory. |
| Not a terminal | Refused in the command (section 2); Textual is never started. |
| Quit, Ctrl-C, app error | Terminal restored; the follower thread stopped; exit 0 for a normal quit. |

## 7. Testing

`tests/test_tui_monitor.py` — the data layer, over a temporary XDG tree, with real log records written through
`LogWriter` and a `status.json` fixture:

- status with a running service, with a stopped service (rows present, `stale_since` set, states `idle`), and
  with status older than 5 s while running (stale).
- state precedence: offline beats parked; parked beats online; invalid and orphaned rows appear with their notes.
- in-flight detection: `poll.start` without `poll.end` → `polling`; `poll.end` closes it; a new `poll.start`
  closes the previous one; cleared when the service stops and when the pid changes.
- backfill: 200 bound respected, bucketed by watch, global deque gets records without a `watch` field.
- `records(verbose=…)` includes captured output only when verbose.
- `version()` changes exactly when a record is appended.
- structural: `monitor.py` imports no Textual (parse its source and assert the import list) — the seam that
  keeps the future web/IPC front-end cheap.

`tests/test_tui_app.py` — Textual `run_test()` pilots, headless, with an injected Monitor over a fixture tree
(refreshes driven directly; no sleeping on timers):

- the table lists every watch; the banner shows the running service; a stopped service shows the stale banner.
- selecting a row shows that watch's records; `a` shows all; `v` toggles verbose; `space` freezes the pane while
  records keep buffering (`+N new` in the header); `r` re-reads discovery.
- offline, parked, invalid and orphaned rows carry their labels.
- `q` exits with code 0.

`tests/test_docs.py` — a key-facts entry for the new `tui` topic. `tests/test_cli_*.py` — `keepwatch tui` refuses
without a terminal, and the command appears in `keepwatch docs cli` (already covered by the existing walk).

CI needs no new dev dependency: Textual is a runtime dependency, so `uv sync` installs it and the pilots run on
both operating systems and both Python versions.

## 8. Documentation

- New narrative topic `src/keepwatch/reference/tui.md`, added to `TOPICS` (after `logs`): what it shows, the
  keys, that it is read-only and safe beside the service, and that `status`/`logs --json` remain the
  machine-readable interface.
- `README.md`: one line in the "Learn more" block and one sentence in the introduction.
- No change to the agent contract: `docs agent` is untouched, and no new config key exists.

## 9. Decisions

- **Files, not IPC (A over B).** The service already publishes everything a monitor needs, atomically, once a
  second, and `follow_log` already handles rotation. An IPC feed would add a protocol, a cross-platform
  transport (Windows named pipes are not sockets), permissions and reconnect rules to the most critical
  component — for the only thing files cannot give: in-flight hook output. A human dashboard does not need it.
- **Textual, hard dependency.** A framework owns raw mode, restore-on-crash, resize, input decoding and the
  Windows drivers; hand-rolling that on Rich was the main risk in this feature. A hard dependency (not an
  extra) keeps `keepwatch tui` working after the documented one-line install.
- **TUI, not a browser.** `textual-serve` would put the same app in a tab through xterm.js — a listening socket
  with no auth, in exchange for watching from elsewhere. Deferred; the pane is the primary experience.
- **Read-only.** Nothing is enabled, disabled or triggered from the screen, so the TUI needs no locks, no
  confirmation rules and no tests for state mutation.
- **No configuration.** No `[tui]` table, no options: the few knobs are keys.

## 10. Future vision (deliberately not built)

If in-flight output, other machines or a phone ever need this, the additions are: a web front-end (HTTP + SSE
over the same data layer, then `textual-serve` for the terminal-in-a-tab version), an IPC-backed live source,
notifications, and control keys. The seams that keep those cheap, and the only debt this design takes on:

1. `monitor.py` is the whole data layer and imports no Textual; `status()`, `records()`, `in_flight()` and the
   version counter are what any other front-end needs.
2. `app.py` only renders Monitor output; no derivation lives in widgets.
3. The TUI writes nothing and adds no protocol, so it can be replaced or joined without touching the service.

## 11. Release

After implementation, tests and review: merge to `main`, bump the version to **2026.10.6** in `pyproject.toml`,
`src/keepwatch/__init__.py` and `uv.lock`, commit the release, tag `v2026.10.6`, push, and confirm CI green on
both operating systems and both Python versions — the same flow as 2026.10.5.
