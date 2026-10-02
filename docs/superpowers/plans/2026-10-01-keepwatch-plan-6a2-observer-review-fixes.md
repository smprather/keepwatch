# keepwatch Plan 6a2 (Observer Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what the code review of Plan 6a found: polls driven back to back by a busy observer, an event queue without a byte limit, full event copies in the log, recursive files observers descending into ignored directories, events delivered by an observer that is being stopped, `keepwatch observe` printing every event twice, three copies of the observer shutdown code, and docs that mislead (an example check that fails forever on a vanished file; a wrong restart rule). Also a flaky alert test seen on Windows CI.

**Architecture:** Small changes in `observers.py` (byte-capped `EventQueue.put` returning how many events were dropped, clipped `observer.event` records, `stop_all`, recursive pruning), `scheduler.py` (a woken poll starts at least `WAKE_GAP` after the previous poll ended and never during the crash hold), `cli.py` (`observe` filters records to INFO and uses `stop_all`), `output.py`, and the observers/logging docs.

**Tech Stack:** as Plan 6a.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 2). Builds on Plan 6a (implemented, commits up to `1ad0664`).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, do not push, portable tests, stop and report on plan defects, attribution trailer).
- **Do not run a code formatter** (`ruff format`, black, or an editor's format-on-save/deferred-format) on any file: this project is not formatted by one, and a formatter rewrites untouched lines. If your harness formatted files after Plan 6a, those edits were discarded on purpose; leave them discarded. Before each commit, `git diff --stat` must show only the files the task names.

## Review Focus

- A source that prints several lines a second must not make the watch poll continuously. Test: Task 1 `test_a_busy_observer_cannot_drive_polls_back_to_back`.
- After a keepwatch internal error (`watch.crash`), events must not cut the 60s crash pause short. Test: Task 1 `test_events_do_not_cut_the_crash_pause_short`.
- A few very large events must not be able to exhaust memory. Test: Task 2 `test_event_queue_is_capped_by_bytes`.
- A recursive observer on a folder with `.git` or sync-tool temp directories must not report their contents. Test: Task 3 `test_recursive_skips_ignored_directories`.
- `keepwatch observe … 2>&1` must show each event once. Test: Task 4 `test_observe_prints_each_event_once`.

---

### Task 1: Pace woken polls

**Files:**
- Modify: `src/keepwatch/scheduler.py`
- Modify: `tests/test_scheduler_observers.py`

**Interfaces:**
- Produces: `scheduler.WAKE_GAP = 1.0`; `WatchRunner._last_end: float | None` (clock time the last poll ended), `WatchRunner._hold_until: float` (no woken poll before this; set by a crash). A woken watch is due at `min(next_due, max(_last_end + WAKE_GAP, _hold_until))`.

- [ ] **Step 1: Update and add tests** — in `tests/test_scheduler_observers.py`:

(a) In `test_a_waking_event_makes_the_watch_due`, replace the lines

```python
    runner.add_event({"n": 2})
    assert runner.seconds_until_due(clock()) == 0
    assert runner.poll_once(clock()).payload == [1, 2]
```

with

```python
    runner.add_event({"n": 2})
    assert runner.seconds_until_due(clock()) == 1.0  # WAKE_GAP after the previous poll ended
    clock.now += 1
    assert runner.seconds_until_due(clock()) == 0
    assert runner.poll_once(clock()).payload == [1, 2]
```

(b) In `test_event_arriving_during_a_poll_is_kept_and_wakes`, change the last line to

```python
    assert runner.seconds_until_due(clock()) == 1.0
```

(c) Append:

```python
def test_a_woken_first_poll_is_due_at_once(make_watch, runner_for):
    watch_dir = make_watch("w", config="interval = '1h'\n", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.next_due = clock() + 3600
    runner.add_event({"n": 1})
    assert runner.seconds_until_due(clock()) == 0


def test_a_busy_observer_cannot_drive_polls_back_to_back(make_watch, runner_for):
    watch_dir = make_watch("w", config="interval = '1h'\n", files={"watch.py": RECORDING_WATCH})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            runner.add_event({"n": 1})
            time.sleep(0.05)
    finally:
        runner.stop()
        assert runner.join(20)
    # One poll at start, then at most one woken poll per WAKE_GAP (1s): never one per event (about 60).
    assert len(kinds(records, "poll.start")) <= 5


def test_events_do_not_cut_the_crash_pause_short(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    records = []
    runner = runner_for(watch_dir, records, time.time)

    def broken(*args, **kwargs):
        raise RuntimeError("keepwatch bug")

    runner._engine.poll = broken
    runner.start()
    try:
        assert wait_for(lambda: kinds(records, "watch.crash"))
        runner.add_event({"n": 1})
        time.sleep(2.5)
    finally:
        runner.stop()
        assert runner.join(20)
    assert len(kinds(records, "watch.crash")) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_scheduler_observers.py -q`
Expected: FAIL — the changed assertions get `0` instead of `1.0`, the busy test counts dozens of polls, the crash test counts several crashes.

- [ ] **Step 3: Implement** — in `src/keepwatch/scheduler.py`:

(a) After `DROP_REPORT_EVERY = 1000` add:

```python
WAKE_GAP = 1.0  # a woken poll starts at least this long after the previous poll ended: busy sources are batched
```

(b) In `WatchRunner.__init__`, after `self._wake_requested = False`, add:

```python
        self._last_end: float | None = None
        self._hold_until = 0.0
```

(c) In `seconds_until_due`, replace

```python
        if self._wake_requested and self.state.failures == 0:
            return 0.0
```

with

```python
        if self._wake_requested and self.state.failures == 0:
            earliest = now if self._last_end is None else self._last_end + WAKE_GAP
            return max(min(self.next_due, max(earliest, self._hold_until)) - now, 0.0)
```

(d) In `poll_once`, right after `finished = self._clock()`, add `self._last_end = finished`.

(e) In `_loop`, in the `except Exception as exc:` block, replace `self.next_due = self._clock() + CRASH_RETRY` with:

```python
                    self.next_due = self._hold_until = self._clock() + CRASH_RETRY
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_scheduler_observers.py tests/test_scheduler.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/scheduler.py tests/test_scheduler_observers.py
git commit -m "Woken polls start at least 1s after the previous poll and never during the crash pause"
```

---

### Task 2: Byte-capped queue, clipped event records

**Files:**
- Modify: `src/keepwatch/observers.py`, `src/keepwatch/scheduler.py` (`add_event`), `src/keepwatch/output.py` (`_observer_event`)
- Modify: `tests/test_observers.py`, `tests/test_scheduler_observers.py`

**Interfaces:**
- Produces:
  - `observers.QUEUE_BYTES = 64 * 1024 * 1024`, `observers.EVENT_LOG_BYTES = 4096`
  - `EventQueue(cap=QUEUE_CAP, max_bytes=QUEUE_BYTES)`; `put(event) -> int` (how many old events were dropped; the newest event is always kept); attributes `cap`, `max_bytes`, `bytes`, `dropped`
  - `observer.event` records carry `data` (the event) when its JSON is at most `EVENT_LOG_BYTES`, else `data_clipped` (its JSON, head and tail kept)
  - `observer.dropped` gains `max_bytes`; it is logged on the first drop and each time the total crosses a multiple of 1000
  - `Observer.emit` delivers nothing once `stop()` was called (the last lines of a source being stopped are dropped)

- [ ] **Step 1: Update and add tests**

(a) `tests/test_observers.py`, `test_event_queue_drops_the_oldest`: replace `assert queue.put({"n": 1}) is False` with `assert queue.put({"n": 1}) == 0` and `assert queue.put({"n": 3}) is True` with `assert queue.put({"n": 3}) == 1`. Then append:

```python
def test_event_queue_is_capped_by_bytes():
    queue = EventQueue(cap=100, max_bytes=250)
    big = {"line": "x" * 100}
    assert [queue.put(dict(big, n=n)) for n in range(4)] == [0, 0, 1, 1]
    assert [event["n"] for event in queue.pending()[1]] == [2, 3]
    assert queue.dropped == 2 and queue.bytes <= 250
    mark, _ = queue.pending()
    queue.ack(mark)
    assert len(queue) == 0 and queue.bytes == 0


def test_event_queue_keeps_an_oversized_newest_event():
    queue = EventQueue(cap=100, max_bytes=10)
    queue.put({"n": 1})
    assert queue.put({"line": "x" * 100}) == 1
    assert queue.pending()[1] == [{"line": "x" * 100}]


def test_large_events_are_logged_clipped(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "EVENT_LOG_BYTES", 50)
    script = 'import json, time\nprint(json.dumps({"n": 1}))\nprint(json.dumps({"line2": "y" * 200}), flush=True)\ntime.sleep(60)\n'
    observer, events, records = command_observer(make_watch, xdg, script)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 2)
    finally:
        stop(observer)
    small, large = kinds(records, "observer.event")
    assert small["data"] == {"n": 1} and "data_clipped" not in small
    assert "data" not in large and len(large["data_clipped"]) < 120
    assert events[1]["line2"] == "y" * 200


def test_a_stopped_observer_delivers_nothing(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SILENT)
    observer.stop()
    observer.emit({"n": 1})
    assert events == [] and not kinds(records, "observer.event")
```

(b) `tests/test_scheduler_observers.py`, `test_a_full_queue_drops_the_oldest_with_a_warning`: after `[dropped] = kinds(records, "observer.dropped")` change the last assertion to:

```python
    assert (dropped["level"], dropped["observer"], dropped["dropped"], dropped["cap"]) == ("WARNING", "feed", 1, 2)
    assert dropped["max_bytes"] == runner.events.max_bytes
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_observers.py tests/test_scheduler_observers.py -q`
Expected: FAIL — `EventQueue() got an unexpected keyword argument 'max_bytes'`, `put` returns booleans, no `data_clipped`, the stopped observer delivers.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/observers.py`: add `from keepwatch.protocol import clip` to the imports. After `QUEUE_CAP = 10_000` add:

```python
QUEUE_BYTES = 64 * 1024 * 1024
EVENT_LOG_BYTES = 4096  # observer.event records keep larger events only as clipped JSON text
```

Replace the whole `EventQueue` class with:

```python
def _event_size(event: dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))


class EventQueue:
    """Pending events for one watch, oldest first: at most `cap` events and `max_bytes` of JSON.

    The oldest are dropped to make room; the newest event is always kept. Not thread-safe: the
    WatchRunner guards it with its lock.
    """

    def __init__(self, cap: int = QUEUE_CAP, max_bytes: int = QUEUE_BYTES) -> None:
        self.cap = cap
        self.max_bytes = max_bytes
        self.bytes = 0
        self.dropped = 0
        self._items: deque[tuple[int, int, dict[str, Any]]] = deque()
        self._next = 1

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: dict[str, Any]) -> int:
        """Add an event; returns how many of the oldest were dropped to make room."""
        size = _event_size(event)
        self._items.append((self._next, size, event))
        self._next += 1
        self.bytes += size
        dropped = 0
        while len(self._items) > 1 and (len(self._items) > self.cap or self.bytes > self.max_bytes):
            _, old_size, _ = self._items.popleft()
            self.bytes -= old_size
            dropped += 1
        self.dropped += dropped
        return dropped

    def pending(self) -> tuple[int, list[dict[str, Any]]]:
        """(a mark for ack(), the pending events oldest first)."""
        return self._next - 1, [event for _, _, event in self._items]

    def ack(self, mark: int) -> None:
        """Remove the events up to `mark` (from pending()); later ones stay."""
        while self._items and self._items[0][0] <= mark:
            _, size, _ = self._items.popleft()
            self.bytes -= size
```

Replace the body of `Observer.emit` (keep the signature) with:

```python
        """Tag an event with this observer's name and arrival time, log it (DEBUG) and deliver it."""
        if self._stop.is_set():
            return  # a source being stopped: its last lines are not delivered
        now = time.time()
        self.last_event = now
        text = json.dumps(event, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) <= EVENT_LOG_BYTES:
            fields: dict[str, Any] = {"data": event}
        else:
            fields = {"data_clipped": clip(text, EVENT_LOG_BYTES)[0]}
        self._sink(make_record("observer.event", level="DEBUG", **self._tag(), **fields))
        try:
            self._deliver({**event, "observer": self.name, "received": iso_time(now)})
        except Exception as exc:
            self._crash(exc)
```

(b) `src/keepwatch/scheduler.py`, `add_event`: replace

```python
        if dropped and (total == 1 or total % DROP_REPORT_EVERY == 0):
```

with

```python
        if dropped and (total == dropped or total // DROP_REPORT_EVERY > (total - dropped) // DROP_REPORT_EVERY):
```

and add `max_bytes=self.events.max_bytes,` after `cap=self.events.cap,` in that record.

(c) `src/keepwatch/output.py`, `_observer_event`: replace its body with:

```python
    data = record.get("data_clipped") or json.dumps(record.get("data"), ensure_ascii=False, default=str)
    return f"observer {record.get('observer')} event: {data[:2000]}"
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_observers.py tests/test_scheduler_observers.py tests/test_files_observer.py tests/test_cli_observe.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the five files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/scheduler.py src/keepwatch/output.py tests/test_observers.py tests/test_scheduler_observers.py
git commit -m "Cap the event queue by bytes, clip large events in the log, drop events from stopping observers"
```

---

### Task 3: Recursive files observers skip ignored directories

**Files:**
- Modify: `src/keepwatch/observers.py` (`FilesObserver._wanted`, `FilesObserver._scan`)
- Modify: `tests/test_files_observer.py`

**Interfaces:**
- Produces: `FilesObserver._ignored(name: str) -> bool`; with `recursive = true`, directories whose names match `ignore` are not entered; the root is listed once per scan.

- [ ] **Step 1: Write the failing test** — append to `tests/test_files_observer.py`:

```python
def test_recursive_skips_ignored_directories(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    for directory in ("sub", ".git/objects/3f", ".stversions", "partial.tmp"):
        (inbox / directory).mkdir(parents=True)
    for relative in ("sub/a.gz", ".git/objects/3f/a1b2c3", ".stversions/old.gz", "partial.tmp/b.gz"):
        (inbox / relative).write_text("x")
        age(inbox / relative)
    observer, events, records = files_observer(make_watch, xdg, inbox, "recursive = true\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_files_observer.py::test_recursive_skips_ignored_directories -q`
Expected: FAIL — `a1b2c3`, `old.gz` and `b.gz` are reported too.

- [ ] **Step 3: Implement** — in `src/keepwatch/observers.py`, replace `FilesObserver._wanted` and `FilesObserver._scan` with:

```python
    def _ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.config.ignore)

    def _wanted(self, name: str) -> bool:
        return fnmatch.fnmatch(name, self.config.pattern) and not self._ignored(name)

    def _scan(self) -> dict[str, os.stat_result]:
        """Regular files to consider, by absolute path. Raises OSError if the directory is gone."""
        root = self.config.path
        assert root is not None
        if not self.config.recursive:
            listing = [(root, os.listdir(root))]
        else:
            if not root.is_dir():
                raise FileNotFoundError(errno.ENOENT, "directory does not exist", str(root))
            listing = []
            for directory, subdirectories, files in os.walk(root):
                subdirectories[:] = [name for name in subdirectories if not self._ignored(name)]
                listing.append((Path(directory), files))
        found = {}
        for directory, entries in listing:
            for name in entries:
                if not self._wanted(name):
                    continue
                path = directory / name
                try:
                    info = path.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found[str(path)] = info
        return found
```

and add `import errno` to the imports.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_files_observer.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/observers.py tests/test_files_observer.py
git commit -m "Recursive files observers do not enter ignored directories"
```

---

### Task 4: One shutdown helper; `observe` shows each event once

**Files:**
- Modify: `src/keepwatch/observers.py`, `src/keepwatch/scheduler.py`, `src/keepwatch/cli.py`
- Modify: `tests/test_cli_observe.py`

**Interfaces:**
- Produces: `observers.OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0` (moved from scheduler.py); `observers.stop_all(running: Iterable[Observer], timeout: float = OBSERVER_JOIN) -> None` (stop all, then wait with one shared deadline). `keepwatch observe` prints INFO and above on stderr.

- [ ] **Step 1: Write the failing test** — append to `tests/test_cli_observe.py`:

```python
def test_observe_prints_each_event_once(make_watch):
    observed(make_watch, FEED)
    result = run("observe", "w", "--count", "2", "--for", "30s")
    assert result.exit_code == 0, result.output
    assert "event:" not in result.stderr
    assert result.output.count('"n": 1') == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli_observe.py::test_observe_prints_each_event_once -q`
Expected: FAIL — stderr contains `observer feed event: {"n": 1}`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/observers.py`: change `from collections.abc import Callable, Mapping` to `from collections.abc import Callable, Iterable, Mapping`. After `_SUPERVISE_STEP = 0.2` add:

```python
OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0  # how long stopping observers may take
```

and add after the `Observer` class:

```python
def stop_all(running: Iterable[Observer], timeout: float = OBSERVER_JOIN) -> None:
    """Ask every observer to stop, then wait for all of them (they stop in parallel) up to `timeout`."""
    observers = list(running)
    for observer in observers:
        observer.stop()
    deadline = time.monotonic() + timeout
    for observer in observers:
        observer.join(max(deadline - time.monotonic(), 0.0))
```

(b) `src/keepwatch/scheduler.py`: delete the line `OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0` and the import `from keepwatch.runner import DRAIN_GRACE, KILL_GRACE`; change the observers import to `from keepwatch.observers import EventQueue, Observer, build_observer, stop_all`; replace the body of `_stop_observers` with:

```python
        with self._observers_lock:
            running, self._observers = list(self._observers.values()), {}
        stop_all(running)
```

(c) `src/keepwatch/cli.py`: change `from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, Runner` back to `from keepwatch.runner import Runner`; change `from keepwatch.observers import build_observer` to `from keepwatch.observers import build_observer, stop_all`; in `observe`, replace `printer = ConsolePrinter(make_console(stderr=True))` with

```python
    printer = level_filter(ConsolePrinter(make_console(stderr=True)), "INFO")
```

and replace the `finally:` block

```python
    finally:
        for item in running:
            item.stop()
        for item in running:
            item.join(KILL_GRACE + DRAIN_GRACE + 1.0)
```

with

```python
    finally:
        stop_all(running)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_observe.py tests/test_scheduler_observers.py tests/test_observers.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/scheduler.py src/keepwatch/cli.py tests/test_cli_observe.py
git commit -m "One observer shutdown helper; keepwatch observe prints each event once"
```

---

### Task 5: Docs and a flaky test

**Files:**
- Modify: `src/keepwatch/reference/observers.md`, `src/keepwatch/reference/logging.md`
- Modify: `tests/test_docs.py` (`KEY_FACTS`), `tests/test_alerts.py`

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`, append to `KEY_FACTS["observers"]`: `"FileNotFoundError"`, `"64 MiB"`, `"1 second after"`, `"do not restart"`; append `"data_clipped"` to `KEY_FACTS["logging"]`.

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — those facts are missing.

- [ ] **Step 2: Fix the docs**

(a) `src/keepwatch/reference/observers.md`: replace the example code block (the one starting `def check(ctx):` with `pulled = ctx.ledger("done")`) with:

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

(b) Same file, "Delivery rules": replace the line

```markdown
- Events wait in a per-watch queue (at most 10 000; when full the oldest are dropped with an `observer.dropped` WARNING).
```

with

```markdown
- Events wait in a per-watch queue (at most 10 000 events and 64 MiB; when full the oldest are dropped with an `observer.dropped` WARNING).
```

replace

```markdown
- With `wake = true` an event makes the watch poll at once, unless it is backing off after failures or is offline (then it waits for its schedule). With `wake = false` events just wait for the next scheduled poll.
```

with

```markdown
- With `wake = true` an event makes the watch poll at once, but no sooner than 1 second after the previous poll ended, so a busy source is handled in batches; not at all while the watch is backing off after failures or is offline (then it waits for its schedule). With `wake = false` events just wait for the next scheduled poll.
```

replace

```markdown
- Observers start with their watch and stop when it is removed, parked (`enabled = false`), disabled or offline; any change to the watch's config restarts them. Events still queued when a watch goes offline are delivered to its trial polls.
```

with

```markdown
- Observers start with their watch and stop when it is removed, parked (`enabled = false`), disabled or offline. A change to the `[observe.*]` tables, `shell`, `[environment]`, `[settings]` or the global `[environment]` restarts them; other changes (`interval`, hooks, timeouts) do not restart them. Events still queued when a watch goes offline are delivered to its trial polls.
- **An event that makes a hook fail is delivered again on every poll** (it is never dropped), so the watch keeps failing until the hook is fixed and goes offline after `max_failures`. Skip events you cannot act on instead of raising, as the example above does for files that have disappeared.
```

(c) `src/keepwatch/reference/logging.md`: change the `observer.event` row to

```markdown
| `observer.event` | DEBUG: `observer`, `data` (the event as the source produced it), or `data_clipped` (its JSON, shortened) when that is over 4 KiB |
```

and the `observer.dropped` row to

```markdown
| `observer.dropped` | `observer`, `dropped` (total so far), `cap`, `max_bytes`: the event queue was full |
```

- [ ] **Step 3: Fix the flaky test** — in `tests/test_alerts.py`, `test_alert_string_runs_in_the_platform_shell`: change `timeout=10` to `timeout=60` (Windows PowerShell's first start on a CI runner took over 10s).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_docs.py tests/test_alerts.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/reference/observers.md src/keepwatch/reference/logging.md tests/test_docs.py tests/test_alerts.py
git commit -m "Observer docs: vanished files, failing events, pacing, restart rule; a slower-CI alert test"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (it must show only `M .gitignore`), and anything in this plan you had to question. Do not push.
