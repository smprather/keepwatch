# keepwatch Plan 8 (TUI: the terminal monitor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `keepwatch tui` — a read-only, live terminal monitor: every watch with its state and next-poll
countdown, and the stream of actions with their results.

**Architecture:** A new `src/keepwatch/tui/` package: `monitor.py` (the view-agnostic data layer over
`status.json`, config discovery and the JSONL log — no Textual) and `app.py` (the Textual App that renders it).
`cli.py` gains a `tui` command whose Textual import is lazy. Textual becomes a hard dependency; `rich`, which
`cli.py` and `output.py` already import directly, is declared directly too.

**Tech Stack:** Python 3.12+, Textual >= 8 (its `run_test()` pilots for the app tests), the existing
`statusview`, `logquery` and `output` modules unchanged.

**Spec:** `docs/superpowers/specs/2026-10-06-keepwatch-tui-design.md`. Branch: `keepwatch-tui`.

## Global Constraints

- **Read-only.** The TUI writes nothing: no state, no log, no config. No service change, no config key.
- `monitor.py` imports no Textual; a structural test asserts it (parses the source and checks the imports).
- Tests first for each task; before every commit: `uv run python -m pytest -q` (the whole suite, from the repo
  root) and `uv run ruff check src tests` must pass, and `git diff --stat` must show only the listed files.
- Commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, as in this repository's commits.
- Do not push before the release task. Stop and report a plan defect instead of improvising around it.

## Review Focus

- **The in-flight rule** is the only real inference in the feature (spec 4.3): an unmatched `poll.start` in the
  log is a poll in flight, *believed* only while the service runs with fresh status, and cleared when the service
  stops, when its pid changes, when a newer poll for that watch starts or ends, **or when the service's own
  `last_poll.at` is newer than the open poll's start** (a poll the service already reported cannot still be
  running — this is what stops a killed manual `keepwatch poll` from lying forever).
- **Stale data is labelled, never counted down** (spec 5): when the service is stopped or the status is older
  than 5 s, `NEXT` is `—` and the banner carries the age.

---

### Task 1: The data layer

**Files:**
- Create: `src/keepwatch/tui/__init__.py` (empty for now; `run()` arrives in Task 3), `src/keepwatch/tui/monitor.py`
- Create: `tests/test_tui_monitor.py`

**Interfaces** (used by Task 2):

```python
class Monitor:
    def __init__(self, paths: Paths, config_path: Path, *, backfill: int = 200,
                 per_watch: int = 300, history: int = 300) -> None
    def start(self) -> None            # backfill once, then follow the log in one daemon thread
    def stop(self) -> None
    def refresh(self) -> None          # status.json + service_running; discovery every 5 s or when forced
    def rescan(self) -> None           # rediscover now
    def status(self) -> Status         # service, watches, problems, stale_since, follower_error
    def records(self, watch: str | None = None, *, verbose: bool = False) -> list[str]
    def in_flight(self) -> dict[str, InFlight]
    def version(self) -> int
```

`Status(service: dict, watches: list[WatchRow], problems: list[str], stale_since: float | None,
follower_error: str | None)`; `WatchRow(name, state, condition, failures, last_poll, next_poll, observers,
pending_events, note, description, watch_dir, interval, offline, config_error)`; `InFlight(poll_id, started)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_tui_monitor.py`), over the existing `xdg` fixture with
  `status.json` written through `paths.status_file` and log records through `LogWriter`:

  - running service: rows for every discovered watch, states `online`/`parked`/`offline`, `stale_since is None`
  - stopped service: rows present and `idle`, `stale_since` set, `NEXT` not offered (the App reads state, not text)
  - status older than 5 s while the service runs: stale
  - state precedence: offline beats parked; parked beats online; `invalid_watches` and orphaned state dirs get
    rows (`invalid` / `orphaned`) with their notes
  - notes: `config error: …`, `missing directory`, `+N events`, `offline by user`
  - in-flight: `poll.start` without `poll.end` → `in_flight()` and state `polling`; `poll.end` closes it; a newer
    `poll.start` replaces it; cleared when the service stops, when the pid changes, and when `last_poll.at` is
    newer than the open poll's start
  - backfill: at most 200 records, bucketed by `watch`, records without a `watch` field in the global deque only
  - bounds: per-watch 300, overall 300
  - `records(verbose=True)` shows captured output that `records()` hides
  - `version()` changes exactly when a record is appended
  - structural: `monitor.py` imports no Textual

- [ ] **Step 2: Implement `monitor.py`** using `collect_status` for the live document, `read_status` +
  `service_running` for the staleness merge, `discover_watches` for rows, `select_records` + `follow_log` for
  records, `format_record` for `records()`. Derive `state` and `note` per spec 4.2/5. One lock guards the deques,
  the open polls and the version counter; `follow_log` runs in a daemon thread whose exception is stored in
  `follower_error` (never raised into the UI).

- [ ] **Step 3: Run** `uv run python -m pytest tests/test_tui_monitor.py -q` and then the whole suite.

---

### Task 2: The Textual app

**Files:**
- Create: `src/keepwatch/tui/app.py`
- Create: `tests/test_tui_app.py`

- [ ] **Step 1: Write the failing pilot tests** with an injected `Monitor` over the `xdg` fixture (refreshes
  driven by calling `app.update_status()` / `app.update_records()` directly; `await pilot.pause()` to settle):

  - the table lists every watch; the banner shows the running service
  - a stopped service shows the stale banner, and `NEXT` cells are `—`
  - selecting a row shows that watch's records; `a` shows all watches' records
  - `v` toggles verbose; `space` freezes the pane while the version grows (`paused · +N new`)
  - `r` re-reads discovery
  - offline, parked, invalid and orphaned rows carry their labels and notes
  - `q` exits (exit code 0)

  Wrap each test body in `asyncio.run(...)` — no new dev dependency.

- [ ] **Step 2: Implement `app.py`**: `KeepwatchApp(App)` with a banner `Static`, a `DataTable` (row key = watch
  name, cursor row = selection), a `RichLog` pane, Textual's `Footer`, and the bindings from spec 5. Timers:
  1 s status, 0.25 s records (redraw only when `monitor.version()` changed), 5 s discovery. No derivation logic.

- [ ] **Step 3: Run** the pilot tests, then the whole suite.

---

### Task 3: The command and the dependency

**Files:**
- Modify: `pyproject.toml` (add `textual>=8`, `rich>=14`), `uv.lock` (via `uv lock`)
- Modify: `src/keepwatch/cli.py` (the `tui` command), `src/keepwatch/tui/__init__.py` (`run(paths, config_path)`)
- Create: `tests/test_cli_tui.py`

- [ ] **Step 1: Write the failing test**: `CliRunner` (never a tty) → exit 1 and a message naming
  `keepwatch status` / `keepwatch logs --json`; nothing imports Textual to get there.

- [ ] **Step 2: Implement**: the CLI command checks `sys.stdin.isatty()` and `sys.stdout.isatty()`, fails through
  `_fail`, then imports `keepwatch.tui` lazily and calls `run(...)`, which builds the Monitor, starts it, runs the
  app, and stops the Monitor in a `finally`. Add the dependency, run `uv lock`.

- [ ] **Step 3: Run** the CLI test and the whole suite; `uv run python -m keepwatch tui` typed in a real terminal
  is the manual check (documented in the report, not part of CI).

---

### Task 4: Documentation

**Files:**
- Create: `src/keepwatch/reference/tui.md`
- Modify: `src/keepwatch/reference.py` (`TOPICS`), `tests/test_docs.py` (`KEY_FACTS["tui"]`), `README.md`

- [ ] **Step 1: Write the topic** (what it shows, the keys, read-only and safe beside the service,
  `status`/`logs --json` stay the machine interface) and register it in `TOPICS` after `logs`.
- [ ] **Step 2: Add the key-facts entry** so the topic stays honest, and the README line(s).
- [ ] **Step 3: Run** `tests/test_docs.py` and the whole suite.

---

### Task 5: Verification and release

- [ ] **Step 1: Full gate**: `uv run python -m pytest -q` (whole suite) and `uv run ruff check src tests` clean;
  `git diff --stat` shows only the files above.
- [ ] **Step 2: Commits** on `keepwatch-tui`, one per task, explicit `git add` per file list.
- [ ] **Step 3: Merge** into `main` (fast-forward), bump the version to `2026.10.6` in `pyproject.toml`,
  `src/keepwatch/__init__.py` and `uv.lock`, commit the release, tag `v2026.10.6`, push `main`, the branch and
  the tag.
- [ ] **Step 4: Confirm CI** green on both operating systems and both Python versions.

## After the last task

- Report: the spec's section 10 seams are in place (`monitor.py` imports no Textual; the App only renders).
- Report the issue triage: #3 is the next release (it needs service-side `in_flight` and transfer-side progress,
  neither of which belongs in this release); #7 is a small independent Windows fix; #4/#5/#6 are their own
  releases.
- Manual checks for the user: `keepwatch tui` in Alacritty/WezTerm/tmux, `q` leaves the terminal clean, and the
  screen during a real poll.
