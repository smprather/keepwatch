# keepwatch Plan 2b (Service Fixes From a Manual Run) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two problems found by running the Plan 2 service by hand: a renamed or deleted watch is polled once more against a missing directory, and several record types print as raw JSON.

**Architecture:** `WatchRunner.poll_once` skips polling when the watch directory no longer exists (the next master tick removes the runner). `output.format_record` gains readable formatters for the service, config, watch-lifecycle and alert events.

**Tech Stack:** as Plan 2.

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md`. Builds on Plans 1, 1b and 2 (implemented).

## Global Constraints

- All Global Constraints of Plans 1 and 2 apply (explicit `git add`, ruff clean, tests only in temporary directories).
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- Renaming a watch while the service runs must not record a failed poll for the old name or recreate its state directory. Test: Task 1 `test_missing_watch_dir_is_not_polled`.
- `keepwatch logs --failed` must print `watch.offline` readably. Test: Task 2 `test_lifecycle_events_are_readable`.

---

### Task 1: Do not poll a watch whose directory is gone

**Files:**
- Modify: `src/keepwatch/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_scheduler.py`:

```python
def test_missing_watch_dir_is_not_polled(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    clock, records = Clock(), []
    runner = runner_for(watch_dir, records, [], clock)
    watch_dir.rename(watch_dir.parent / "renamed")
    assert runner.poll_once(clock()) is None
    assert runner.state.failures == 0
    assert not xdg.watch_state_dir("w").exists()
    assert records == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_scheduler.py -q`
Expected: FAIL (a poll runs and fails with "cannot start command")

- [ ] **Step 3: Implement.** In `WatchRunner.poll_once`, replace

```python
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
```

with

```python
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
        if not self._config.watch_dir.is_dir():
            # Renamed or deleted since the last master tick; the next tick removes this runner.
            return None
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/scheduler.py tests/test_scheduler.py
git commit -m "Skip polls of a watch whose directory is gone"
```

---

### Task 2: Readable service, config, watch and alert records

**Files:**
- Modify: `src/keepwatch/output.py`
- Test: `tests/test_output.py`

**Interfaces:**
- Produces: `format_record` formatters for `service.start`, `service.stop`, `config.loaded`, `config.error`, `watch.added`, `watch.changed`, `watch.removed`, `watch.offline`, `watch.online`, `watch.crash` and `alert.end`, with exactly the text the tests below expect.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_output.py`:

```python
def test_lifecycle_events_are_readable():
    service = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "INFO"}
    watch = {**service, "watch": "psg"}

    def fmt(record, verbose=False):
        return format_record(record, verbose=verbose)

    assert fmt({**service, "event": "service.start", "version": "1", "config": "/c.toml"}) == (
        "10:11:12 service started (version 1, config /c.toml)"
    )
    assert fmt({**service, "event": "service.stop"}) == "10:11:12 service stopped"
    assert fmt({**service, "event": "config.loaded", "path": "/c.toml"}) == "10:11:12 config reloaded: /c.toml"
    assert fmt({**watch, "event": "config.error", "error": "a.toml:2: bad\nb.toml:3: worse",
                "running_previous": True}) == (
        "10:11:12 psg config error (still running the previous config): a.toml:2: bad\n"
        "  error:\n    a.toml:2: bad\n    b.toml:3: worse"
    )
    assert fmt({**service, "event": "config.error", "error": "x: y"}) == "10:11:12 config error: x: y"
    assert fmt({**watch, "event": "watch.added", "enabled": False}) == "10:11:12 psg watch added (parked: enabled = false)"
    assert fmt({**watch, "event": "watch.added", "enabled": True}) == "10:11:12 psg watch added"
    assert fmt({**watch, "event": "watch.changed"}) == "10:11:12 psg config changed; applies from the next poll"
    assert fmt({**watch, "event": "watch.removed"}) == "10:11:12 psg watch removed"
    assert fmt({**watch, "event": "watch.offline", "reason": "5 consecutive failed polls",
                "last_failure": "on_true failed: boom"}) == (
        "10:11:12 psg OFFLINE: 5 consecutive failed polls; last failure: on_true failed: boom"
    )
    assert fmt({**watch, "event": "watch.online", "reason": "enabled by user"}) == (
        "10:11:12 psg back online: enabled by user"
    )
    assert fmt({**watch, "event": "watch.crash", "error": "KeyError: 'x'", "traceback": "Traceback\n  boom"}) == (
        "10:11:12 psg keepwatch internal error: KeyError: 'x'\n  traceback:\n    Traceback\n      boom"
    )
    assert fmt({**watch, "event": "alert.end", "alert_event": "offline", "status": "ok", "duration": 0.5}) == (
        "10:11:12 psg alert_command (offline) → ok (0.50s)"
    )
    assert fmt({**watch, "event": "alert.end", "alert_event": "online", "status": "failed", "duration": 0.1,
                "reason": "exit code 3", "stderr": "nope\n"}) == (
        "10:11:12 psg alert_command (online) → failed (0.10s): exit code 3\n  stderr:\n    nope"
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_output.py -q`
Expected: FAIL (records print as `event {json}`)

- [ ] **Step 3: Implement.** In `src/keepwatch/output.py`, add these functions after `_poll_end` (before `_SKIP`):

```python
def _service_start(record: dict[str, Any], verbose: bool) -> str:
    return f"service started (version {record.get('version')}, config {record.get('config')})"


def _service_stop(record: dict[str, Any], verbose: bool) -> str:
    return "service stopped"


def _config_loaded(record: dict[str, Any], verbose: bool) -> str:
    return f"config reloaded: {record.get('path')}"


def _config_error(record: dict[str, Any], verbose: bool) -> str:
    error = str(record.get("error") or "")
    lines = error.splitlines() or [""]
    note = " (still running the previous config)" if record.get("running_previous") else ""
    line = f"config error{note}: {lines[0]}"
    if len(lines) > 1:
        line += _block("error", error)
    return line


def _watch_added(record: dict[str, Any], verbose: bool) -> str:
    return "watch added" + (" (parked: enabled = false)" if record.get("enabled") is False else "")


def _watch_changed(record: dict[str, Any], verbose: bool) -> str:
    return "config changed; applies from the next poll"


def _watch_removed(record: dict[str, Any], verbose: bool) -> str:
    return "watch removed"


def _watch_offline(record: dict[str, Any], verbose: bool) -> str:
    line = f"OFFLINE: {record.get('reason')}"
    if record.get("last_failure"):
        line += f"; last failure: {record['last_failure']}"
    return line


def _watch_online(record: dict[str, Any], verbose: bool) -> str:
    return f"back online: {record.get('reason')}"


def _watch_crash(record: dict[str, Any], verbose: bool) -> str:
    line = f"keepwatch internal error: {record.get('error')}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _alert_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"alert_command ({record.get('alert_event')}) → {record.get('status')} ({record.get('duration', 0):.2f}s)"
    if record.get("reason"):
        line += f": {record['reason']}"
    if verbose or record.get("status") in FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line
```

and add these entries to the `_FORMATTERS` dictionary:

```python
    "service.start": _service_start,
    "service.stop": _service_stop,
    "config.loaded": _config_loaded,
    "config.error": _config_error,
    "watch.added": _watch_added,
    "watch.changed": _watch_changed,
    "watch.removed": _watch_removed,
    "watch.offline": _watch_offline,
    "watch.online": _watch_online,
    "watch.crash": _watch_crash,
    "alert.end": _alert_end,
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/output.py tests/test_output.py
git commit -m "Print service, config, watch and alert records readably"
```
