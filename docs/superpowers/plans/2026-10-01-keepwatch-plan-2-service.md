# keepwatch Plan 2 (The Service) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Plan 1 engine into the always-running service: a scheduler per watch with backoff, offline and trial polls, live config reload, a status file, and the `run`, `status`, `logs`, `enable`, `disable` and `rename` commands.

**Architecture:** `keepwatch run` starts a `Service`. Its master tick (every `reload_interval`) reloads changed config files, starts/updates/stops one `WatchRunner` thread per watch, and writes `status.json`. Each `WatchRunner` decides when its watch is due, runs one poll through the shared `PollEngine`, applies backoff, and takes the watch offline (persisted in `offline.json`) or back online. Every record flows through one `QueueSink` thread to the log file and the terminal. The other commands read `status.json`, `offline.json` and the log; they never talk to the service directly.

**Tech Stack:** Python ≥ 3.12, uv, rich-click, pytest, ruff (added in Task 1).

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md` (sections 3, 7, 8, 11, 12, 13). Builds on Plans 1 and 1b (implemented).

## Global Constraints

- All of Plan 1's Global Constraints still apply.
- Commit with explicit `git add <files>`; never `git commit -a` (the working tree has unrelated changes that must not be committed).
- From Task 1 on, `uv run ruff check src tests` must pass before every commit (Task 1 adds a test that enforces it). If ruff reports only import-order (I001) or whitespace (W29x) findings, apply `uv run ruff check --fix src tests`; any other finding means stop and report.
- Test fixtures write only inside pytest's temporary directories; nothing may touch the real `~/.config`, `~/.local/state` or `/run/user` trees.
- Line numbers drift; find edits by the quoted code.
- Simplifications accepted for this plan: `status.json` is written every master tick (not also after every poll), so it is at most `reload_interval` stale; log rotation settings (`[log]`) are read when the service starts.
- A poll interrupted by shutdown never changes a watch's offline state.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- Stopping the service (SIGTERM) while a hook runs must not count as a failure that takes the watch offline. Test: Task 5 `test_poll_interrupted_by_shutdown_does_not_go_offline`.
- A watch taken offline must stay offline across a service restart. Test: Task 5 `test_offline_survives_restart`.
- Saving a half-edited config.toml while the service runs must keep the last good config running and report the error once. Test: Task 6 `test_broken_edit_keeps_last_good_config`.
- Starting a second service while one runs must be refused, and SIGTERM must stop the running one cleanly. Test: Task 7 `test_run_polls_and_stops_cleanly`.
- `logs -f` must keep following across a log rotation. Test: Task 9 `test_follow_survives_rotation`.

---

## File Structure

```
pyproject.toml                   + ruff and pyright configuration, ruff dev dependency (Task 1)
src/keepwatch/runner.py          + FAILED_STATUSES, live message streaming, terminate_all (Tasks 2, 3, 6)
src/keepwatch/ctx.py             Ledger lookups without full scans (Task 2)
src/keepwatch/output.py          use FAILED_STATUSES (Task 2)
src/keepwatch/pollengine.py      FAILED_STATUSES, streaming, trial flag, set_global_config (Tasks 2, 3, 5)
src/keepwatch/paths.py           + write_json_atomic (Task 4)
src/keepwatch/offline.py         OfflineMarker, read/write/clear offline.json, iso_time (Task 4)
src/keepwatch/alerts.py          run_alert (Task 4)
src/keepwatch/scheduler.py       WatchRunner: due times, backoff, offline, trial polls, thread loop (Task 5)
src/keepwatch/service.py         Service: master tick, reload, runners, status.json, shutdown (Task 6)
src/keepwatch/logstore.py        + LEVELS, level_filter, QueueSink (Task 7)
src/keepwatch/statusview.py      collect_status, format_status (Task 8)
src/keepwatch/logquery.py        LogFilter, parse_when, select_records, follow_log (Task 9)
src/keepwatch/control.py         enable/disable/rename (Task 10)
src/keepwatch/config.py          + is_valid_watch_name (Task 10)
src/keepwatch/cli.py             + run, status, logs, enable, disable, rename; command groups (Tasks 7-10)
tests/test_*.py                  matching test files
```

---

### Task 1: Lint and type-checker configuration

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_lint.py`
- Modify (mechanical, by `ruff --fix` only): files under `src/` and `tests/`

**Interfaces:**
- Produces: `uv run ruff check src tests` passes; pyright (as used by editors) resolves imports from `.venv`.

- [ ] **Step 1: Write the failing test** — `tests/test_lint.py`

```python
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ruff_is_clean():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_lint.py -q`
Expected: FAIL with `No module named ruff`

- [ ] **Step 3: Add the tooling.** Run `uv add --dev "ruff>=0.6"`. Then append to `pyproject.toml`:

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I"]
ignore = ["E501"]

[tool.pyright]
venvPath = "."
venv = ".venv"
pythonVersion = "3.12"
include = ["src", "tests"]
```

- [ ] **Step 4: Apply the safe fixes.** Run `uv run ruff check --fix src tests`, then `uv run ruff check src tests`. Expected: `All checks passed!`. The fixes must only reorder imports (I001) or strip whitespace (W291/W293). If any other rule remains, stop and report it instead of editing code by hand.

- [ ] **Step 5: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock tests/test_lint.py src tests
git commit -m "Configure ruff and pyright; sort imports"
```

---

### Task 2: Shared failure statuses and fast ledger lookups

**Files:**
- Modify: `src/keepwatch/runner.py`, `src/keepwatch/pollengine.py`, `src/keepwatch/output.py`, `src/keepwatch/ctx.py`
- Test: `tests/test_output.py`, `tests/test_ctx.py`

**Interfaces:**
- Produces: `runner.FAILED_STATUSES = frozenset({"error", "failed", "timeout"})`, used by `pollengine` and `output` (their private copies are removed). `Ledger.__contains__` and `Ledger.added_at` look up one key without scanning the ledger.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_output.py`:

```python
def test_failed_statuses_are_defined_once():
    from keepwatch import output, pollengine, runner

    assert runner.FAILED_STATUSES == frozenset({"error", "failed", "timeout"})
    assert not hasattr(output, "_FAILED_STATUSES")
    assert not hasattr(pollengine, "_FAILED")
```

Append to `tests/test_ctx.py`:

```python
def test_ledger_lookup_does_not_scan_the_whole_ledger(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "x.json", expire=3600)
    ledger.add("k")

    def scanned(self):
        raise AssertionError("the whole ledger was scanned")

    monkeypatch.setattr(Ledger, "_live", scanned)
    assert "k" in ledger
    assert "other" not in ledger
    assert ledger.added_at("k") is not None
    assert ledger.added_at("other") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_output.py tests/test_ctx.py -q`
Expected: 2 FAIL (`AttributeError: ... FAILED_STATUSES`; `AssertionError: the whole ledger was scanned`)

- [ ] **Step 3: Implement.**

In `src/keepwatch/runner.py`, below `RESULT_LIMIT = ...` add:

```python
FAILED_STATUSES = frozenset({"error", "failed", "timeout"})
```

In `src/keepwatch/pollengine.py`: delete the line `_FAILED = ("error", "failed", "timeout")`, change the runner import to `from keepwatch.runner import FAILED_STATUSES, HookCall, HookResult, Runner`, and replace `result.status in _FAILED` with `result.status in FAILED_STATUSES`.

In `src/keepwatch/output.py`: delete the line `_FAILED_STATUSES = ("error", "failed", "timeout")`, add `from keepwatch.runner import FAILED_STATUSES` to the imports, and replace both uses of `_FAILED_STATUSES` with `FAILED_STATUSES`.

In `src/keepwatch/ctx.py`, in `class Ledger`, replace `_live`, `__contains__` and `added_at` with:

```python
    def _fresh(self, stamp: float, now: float) -> bool:
        return self.expire is None or stamp >= now - self.expire

    def _live(self) -> dict[str, float]:
        if self.expire is None:
            return self._entries
        now = time.time()
        return {key: stamp for key, stamp in self._entries.items() if self._fresh(stamp, now)}

    def __contains__(self, key: object) -> bool:
        stamp = self._entries.get(key)  # type: ignore[call-overload]
        return stamp is not None and self._fresh(stamp, time.time())
```

and

```python
    def added_at(self, key: str) -> datetime | None:
        """When key was added (UTC), or None if it is absent or expired."""
        stamp = self._entries.get(key)
        if stamp is None or not self._fresh(stamp, time.time()):
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/runner.py src/keepwatch/pollengine.py src/keepwatch/output.py src/keepwatch/ctx.py tests/test_output.py tests/test_ctx.py
git commit -m "Share FAILED_STATUSES and look up ledger keys without scanning"
```

---

### Task 3: Stream worker messages live

**Files:**
- Modify: `src/keepwatch/runner.py`, `src/keepwatch/pollengine.py`
- Test: `tests/test_runner_python.py`

**Interfaces:**
- Consumes: `decode_lines` (Plan 1).
- Produces: `HookCall.on_message: Callable[[dict], None] | None = None` (excluded from equality). When set, `log` and `command` messages from a Python worker are delivered to it as they arrive (on a reader thread) and `HookResult.messages` holds only messages that could not be delivered plus keepwatch's own notes. At most `RESULT_LIMIT` bytes of log/command messages are delivered per call; the rest are counted and dropped, and a single oversized line is dropped without being buffered whole. `PollEngine` passes an `on_message` that emits records immediately, so `keepwatch poll` shows plugin logs live.

- [ ] **Step 1: Write the failing tests.** In `tests/test_runner_python.py` add `import dataclasses` at the top, and replace the whole `test_huge_log_volume_is_capped` test (from Plan 1b) with:

```python
def test_huge_log_volume_is_capped(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "RESULT_LIMIT", 100_000)
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            for _ in range(2000):
                ctx.log.info("x" * 200)
            return True
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    notes = [m["message"] for m in result.messages if m.get("logger") == "keepwatch.worker"]
    assert any("beyond the 100000-byte limit" in note for note in notes)
    assert sum(1 for m in result.messages if m.get("type") == "log") < 1000


def test_one_giant_message_is_dropped_not_buffered(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "RESULT_LIMIT", 100_000)
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            ctx.log.info("y" * 300_000)
            ctx.log.info("after")
            return True
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    logs = [m["message"] for m in result.messages if m.get("type") == "log"]
    assert "after" in logs
    assert any("beyond the 100000-byte limit" in message for message in logs)


def test_messages_are_delivered_while_the_hook_runs(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        import time

        def check(ctx):
            ctx.log.info("early")
            time.sleep(2)
            return True
    '''})
    arrivals = []
    call = dataclasses.replace(call_for(watch_dir), on_message=lambda m: arrivals.append((time.monotonic(), m)))
    result = Runner().run(call)
    finished = time.monotonic()
    assert result.status == "true"
    assert [message["message"] for _, message in arrivals] == ["early"]
    assert finished - arrivals[0][0] >= 1.5
    assert result.messages == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_runner_python.py -q`
Expected: 3 FAIL (old "first and last" note; `TypeError: ... unexpected keyword argument 'on_message'`; the giant line keeps no `after` message)

- [ ] **Step 3: Implement** in `src/keepwatch/runner.py`.

Change the `collections.abc` import to `from collections.abc import Callable, Mapping, Sequence`. Add a field at the end of `HookCall`:

```python
    on_message: Callable[[dict[str, Any]], None] | None = field(default=None, compare=False)
```

Add this class right after `class _Reader`:

```python
class _MessageReader(threading.Thread):
    """Reads the worker's JSON-line messages as they arrive.

    hello and result are kept. log and command messages are delivered (to
    on_message, or kept in .messages without one) until `limit` bytes of them
    have been delivered; later ones are counted in dropped_bytes. A line longer
    than `limit` is skipped without being buffered whole.
    """

    def __init__(self, stream: IO[bytes], limit: int, on_message: Callable[[dict[str, Any]], None] | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._on_message = on_message
        self.hello: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.messages: list[dict[str, Any]] = []
        self.bad = 0
        self.delivered_bytes = 0
        self.dropped_bytes = 0

    def run(self) -> None:
        fd = self._stream.fileno()
        buffer = b""
        skipping = False
        try:
            while chunk := os.read(fd, 65536):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if skipping:
                        self.dropped_bytes += len(line)
                        skipping = False
                        continue
                    self._line(line)
                if len(buffer) > self._limit:
                    self.dropped_bytes += len(buffer)
                    buffer = b""
                    skipping = True
            if buffer and not skipping:
                self._line(buffer)
        finally:
            self._stream.close()

    def _line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        messages, bad = decode_lines(raw + b"\n")
        self.bad += len(bad)
        for message in messages:
            kind = message.get("type")
            if kind == "hello":
                self.hello = message
            elif kind == "result":
                self.result = message
            elif kind in ("log", "command"):
                if self.delivered_bytes + len(raw) > self._limit:
                    self.dropped_bytes += len(raw)
                    continue
                self.delivered_bytes += len(raw)
                self.deliver(message)
            else:
                self.bad += 1

    def deliver(self, message: dict[str, Any]) -> None:
        if self._on_message is None:
            self.messages.append(message)
            return
        try:
            self._on_message(message)
        except Exception:
            self.messages.append(message)
```

In `_run_python`, replace everything from `results = _Reader(os.fdopen(res_r, "rb"), RESULT_LIMIT)` down to (and including) the two lines

```python
        hello = next((m for m in messages if m.get("type") == "hello"), None)
        result = next((m for m in messages if m.get("type") == "result"), None)
```

with:

```python
        results = _MessageReader(os.fdopen(res_r, "rb"), RESULT_LIMIT, call.on_message)
        for reader in (out, err, results):
            reader.start()
        returncode, timed_out = self._supervise(proc, (out, err, results), call.timeout)
        base = self._captured(out, err, returncode, started)
        if results.bad:
            results.deliver(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": f"ignored {results.bad} malformed message(s) from the worker",
                    "fields": {},
                }
            )
        if results.dropped_bytes:
            results.deliver(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": (
                        f"dropped {results.dropped_bytes} bytes of worker messages "
                        f"beyond the {RESULT_LIMIT}-byte limit"
                    ),
                    "fields": {},
                }
            )
        base["messages"] = results.messages
        common = {"hook": call.hook, "kind": "python", "target": target, **base}
        failure = self._failure_status(call)
        if timed_out:
            return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
        hello = results.hello
        result = results.result
```

(`_supervise` is typed with `Sequence[_Reader]`; change its annotation to `Sequence[threading.Thread]`.)

In `src/keepwatch/pollengine.py`, add `import functools` to the imports, and restructure `PollEngine._run` so the message conversion is a method used both live and afterwards:

```python
    def _emit_message(self, tag: dict[str, Any], message: dict[str, Any]) -> None:
        if message.get("type") == "command":
            fields = {key: message.get(key) for key in _COMMAND_FIELDS}
            level = "INFO" if message.get("exit_code") == 0 else "WARNING"
            self._sink(make_record("command", level=level, **tag, **fields))
            return
        level = message.get("level") if message.get("level") in _PLUGIN_LEVELS else "INFO"
        self._sink(
            make_record(
                "plugin.log",
                level=level,
                **tag,
                logger=message.get("logger"),
                message=message.get("message"),
                fields=message.get("fields") or {},
                traceback=message.get("traceback"),
            )
        )

    def _run(
        self,
        watch: WatchConfig,
        hook: str,
        poll_id: str,
        condition: bool,
        payload: Any,
        timeout: float,
    ) -> HookResult:
        tag = {"watch": watch.name, "poll_id": poll_id, "hook": hook}
        call = HookCall(
            watch=watch,
            hook=hook,
            poll_id=poll_id,
            condition=condition,
            payload=payload,
            data_dir=self._paths.watch_data_dir(watch.name),
            run_dir=self._paths.run_dir(self._pid, watch.name),
            timeout=timeout,
            capture_bytes=self._global.log.capture_bytes,
            environment=self._global.environment,
            on_message=functools.partial(self._emit_message, tag),
        )
        result = self._runner.run(call)
        for message in result.messages:
            self._emit_message(tag, message)
        level = "ERROR" if result.status in FAILED_STATUSES else "INFO"
        self._sink(make_record("hook.end", level=level, watch=watch.name, poll_id=poll_id, **result.to_record()))
        return result
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS (including Plan 1's `test_psg_style_watch_sends_once`, whose record order is unchanged)

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/runner.py src/keepwatch/pollengine.py tests/test_runner_python.py
git commit -m "Stream plugin log and command messages live, with a bounded reader"
```

---

### Task 4: Offline markers, alerts and atomic JSON writes

**Files:**
- Modify: `src/keepwatch/paths.py`
- Create: `src/keepwatch/offline.py`, `src/keepwatch/alerts.py`
- Test: `tests/test_offline.py`, `tests/test_alerts.py`

**Interfaces:**
- Produces:
  - `paths.write_json_atomic(path: Path, document: Any) -> None` (mkdir 0700, temp file + `os.replace`)
  - `offline.iso_time(epoch: float) -> str` (local time, RFC 3339 with offset, milliseconds)
  - `OfflineMarker(reason: str, since: str, by_user: bool = False, last_failure: str | None = None)` (frozen) with `since_epoch() -> float` and `to_dict() -> dict`
  - `read_offline(paths, watch) -> OfflineMarker | None` — an unreadable file counts as offline by user, with a reason naming the file
  - `write_offline(paths, watch, marker) -> None`; `clear_offline(paths, watch) -> bool` (True if a marker was removed)
  - `alerts.run_alert(command: Command | None, *, event: str, watch: str, reason: str, environment: Mapping[str, str], timeout: float, sink: Sink, capture_bytes: int = 65_536) -> None` — does nothing when `command` is None; otherwise runs it with `KEEPWATCH_ALERT_EVENT`, `KEEPWATCH_WATCH`, `KEEPWATCH_ALERT_REASON` and emits one `alert.end` record (`watch, alert_event, command, status ("ok"|"failed"|"timeout"), exit_code, reason, stdout, stderr, duration`); never raises

- [ ] **Step 1: Write the failing tests** — `tests/test_offline.py`

```python
import json

from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import write_json_atomic


def test_write_json_atomic(tmp_path):
    target = tmp_path / "a" / "b.json"
    write_json_atomic(target, {"x": 1})
    assert json.loads(target.read_text()) == {"x": 1}
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_iso_time_round_trips():
    marker = OfflineMarker(reason="r", since=iso_time(1_000_000.5))
    assert abs(marker.since_epoch() - 1_000_000.5) < 0.001


def test_offline_marker_round_trip(xdg):
    assert read_offline(xdg, "w") is None
    marker = OfflineMarker(reason="5 consecutive failed polls", since=iso_time(1_000_000), last_failure="on_true failed: x")
    write_offline(xdg, "w", marker)
    assert read_offline(xdg, "w") == marker
    assert json.loads(xdg.offline_file("w").read_text())["by_user"] is False
    assert clear_offline(xdg, "w") is True
    assert clear_offline(xdg, "w") is False
    assert read_offline(xdg, "w") is None


def test_unreadable_marker_counts_as_offline_by_user(xdg):
    path = xdg.offline_file("w")
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    marker = read_offline(xdg, "w")
    assert marker.by_user is True
    assert str(path) in marker.reason and "keepwatch enable w" in marker.reason
```

`tests/test_alerts.py`:

```python
from keepwatch.alerts import run_alert
from keepwatch.config import Command


def test_no_command_does_nothing():
    records = []
    run_alert(None, event="offline", watch="w", reason="r", environment={}, timeout=5, sink=records.append)
    assert records == []


def test_alert_receives_environment(tmp_path):
    out = tmp_path / "alert.txt"
    records = []
    command = Command(shell='echo "$KEEPWATCH_ALERT_EVENT|$KEEPWATCH_WATCH|$KEEPWATCH_ALERT_REASON" > "$OUT"')
    run_alert(command, event="offline", watch="psg", reason="5 failures", environment={"OUT": str(out)},
              timeout=5, sink=records.append)
    assert out.read_text() == "offline|psg|5 failures\n"
    (record,) = records
    assert (record["event"], record["status"], record["alert_event"], record["level"]) == ("alert.end", "ok", "offline", "INFO")


def test_failing_and_slow_alerts_are_recorded_not_raised(tmp_path):
    records = []
    run_alert(Command(shell="echo bad >&2; exit 3"), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    run_alert(Command(argv=("sleep", "5")), event="online", watch="w", reason="r", environment={},
              timeout=0.5, sink=records.append)
    run_alert(Command(argv=("/no/such/program",)), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    assert [(r["status"], r["level"]) for r in records] == [("failed", "WARNING"), ("timeout", "WARNING"), ("failed", "WARNING")]
    assert records[0]["stderr"] == "bad\n" and records[0]["exit_code"] == 3
    assert records[2]["reason"].startswith("cannot start alert_command")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_offline.py tests/test_alerts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.offline'`

- [ ] **Step 3: Implement.** Append to `src/keepwatch/paths.py` (add `import json`, `import tempfile` and `from typing import Any` to its imports):

```python
def write_json_atomic(path: Path, document: Any) -> None:
    """Write JSON so readers see either the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, default=str)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
```

`src/keepwatch/offline.py`:

```python
"""offline.json: a watch stopped by the failure limit or by `keepwatch disable` (spec 8.3)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from keepwatch.paths import Paths, write_json_atomic


def iso_time(epoch: float) -> str:
    """Local time as RFC 3339 with offset and milliseconds."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class OfflineMarker:
    reason: str
    since: str
    by_user: bool = False
    last_failure: str | None = None

    def since_epoch(self) -> float:
        try:
            return datetime.fromisoformat(self.since).timestamp()
        except ValueError:
            return 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_offline(paths: Paths, watch: str) -> OfflineMarker | None:
    path = paths.offline_file(watch)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return OfflineMarker(
            reason=str(data["reason"]),
            since=str(data["since"]),
            by_user=bool(data.get("by_user", False)),
            last_failure=data.get("last_failure"),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return OfflineMarker(
            reason=f"{path} is unreadable; run: keepwatch enable {watch}",
            since=iso_time(time.time()),
            by_user=True,
        )


def write_offline(paths: Paths, watch: str, marker: OfflineMarker) -> None:
    write_json_atomic(paths.offline_file(watch), marker.to_dict())


def clear_offline(paths: Paths, watch: str) -> bool:
    try:
        paths.offline_file(watch).unlink()
    except FileNotFoundError:
        return False
    return True
```

`src/keepwatch/alerts.py`:

```python
"""alert_command: run when a watch goes offline or comes back online (spec 8.4)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from keepwatch.config import Command
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.protocol import clip


def run_alert(
    command: Command | None,
    *,
    event: str,
    watch: str,
    reason: str,
    environment: Mapping[str, str],
    timeout: float,
    sink: Sink,
    capture_bytes: int = 65_536,
) -> None:
    """Run alert_command and log one alert.end record. Never raises."""
    if command is None:
        return
    env = {
        **os.environ,
        **environment,
        "KEEPWATCH_ALERT_EVENT": event,
        "KEEPWATCH_WATCH": watch,
        "KEEPWATCH_ALERT_REASON": reason,
    }
    fields: dict[str, Any] = {
        "watch": watch,
        "alert_event": event,
        "command": command.display(),
        "exit_code": None,
        "reason": None,
        "stdout": "",
        "stderr": "",
    }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command.to_argv(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        status = "timeout"
        fields["reason"] = f"timed out after {format_duration(timeout)}"
    except OSError as exc:
        status = "failed"
        fields["reason"] = f"cannot start alert_command: {exc.strerror or exc}"
    else:
        status = "ok" if completed.returncode == 0 else "failed"
        fields["exit_code"] = completed.returncode
        fields["stdout"] = clip(completed.stdout, capture_bytes)[0]
        fields["stderr"] = clip(completed.stderr, capture_bytes)[0]
        if status == "failed":
            fields["reason"] = f"exit code {completed.returncode}"
    sink(
        make_record(
            "alert.end",
            level="INFO" if status == "ok" else "WARNING",
            status=status,
            duration=round(time.monotonic() - started, 3),
            **fields,
        )
    )
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/paths.py src/keepwatch/offline.py src/keepwatch/alerts.py tests/test_offline.py tests/test_alerts.py
git commit -m "Add offline markers, alert_command and atomic JSON writes"
```

---

### Task 5: The per-watch scheduler

**Files:**
- Modify: `src/keepwatch/pollengine.py`
- Create: `src/keepwatch/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `PollEngine`, `PollReport` (Plan 1); `next_delay`, `should_go_offline`, `initial_state`, `ANSWERS` (Plan 1); `hold_lock` (Plan 1); Task 4's offline functions.
- Produces:
  - `PollEngine.set_global_config(global_config: GlobalConfig) -> None`; `PollEngine.poll(..., trial: bool = False)` — `poll.start` records carry `trial`; `PollReport.trial: bool = False` and `to_dict()["trial"]`
  - `scheduler.Alert = Callable[[str, str, str], None]` — called as `alert(event, watch, reason)` with event `"offline"` or `"online"`
  - `LastPoll(poll_id, at, outcome, reason, failed, trial, results)` (frozen)
  - `WatchRunner(config: WatchConfig, *, engine: PollEngine, paths: Paths, sink: Sink, alert: Alert, clock: Callable[[], float] = time.time)` with attributes `name`, `state`, `next_due`, `offline`, `last_trial`, `last_poll`, `config_error`, property `config`, and methods `update_config(config)`, `seconds_until_due(now) -> float | None`, `poll_once(now) -> PollReport | None`, `snapshot(now) -> dict`, `start()`, `stop()`, `join(timeout=None) -> bool`
  - Records: `watch.offline` (`reason, by_user, last_failure?`; CRITICAL from the failure limit, WARNING when disabled by a user), `watch.online` (`reason`), `watch.crash` (`error, traceback`)

- [ ] **Step 1: Write the failing tests** — `tests/test_scheduler.py`

```python
import os
import time

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.scheduler import WatchRunner


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def runner_for(xdg):
    def make(watch_dir, records, alerts, clock):
        global_config = load_global_config(xdg.config_file, xdg)
        engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())
        return WatchRunner(
            load_watch_config(watch_dir),
            engine=engine,
            paths=xdg,
            sink=records.append,
            alert=lambda event, watch, reason: alerts.append((event, watch, reason)),
            clock=clock,
        )

    return make


def events(records, name):
    return [r for r in records if r["event"] == name]


def test_polls_immediately_then_after_the_interval(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.poll_once(clock()) is not None
    clock.now += 59
    assert runner.poll_once(clock()) is None
    clock.now += 1
    report = runner.poll_once(clock())
    assert report is not None and report.before.condition is True


def test_backoff_then_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "10s"\nmax_failures = 2\n[hooks]\ncheck = "exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert runner.state.failures == 1
    assert runner.seconds_until_due(clock()) == 60
    clock.now += 60
    runner.poll_once(clock())
    marker = read_offline(xdg, "w")
    assert marker.reason == "2 consecutive failed polls" and marker.by_user is False
    assert marker.last_failure.startswith("check error")
    (offline,) = events(records, "watch.offline")
    assert offline["level"] == "CRITICAL"
    assert alerts == [("offline", "w", f"2 consecutive failed polls; last: {marker.last_failure}")]
    clock.now += 100_000
    assert runner.poll_once(clock()) is None


def test_trial_poll_brings_the_watch_back(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 1\nretry_after = "1h"\n[hooks]\ncheck = "test -f ok || exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert read_offline(xdg, "w") is not None
    clock.now += 3599
    assert runner.poll_once(clock()) is None
    clock.now += 1
    failed_trial = runner.poll_once(clock())
    assert failed_trial.trial is True and read_offline(xdg, "w") is not None
    (watch_dir / "ok").write_text("")
    clock.now += 3600
    trial = runner.poll_once(clock())
    assert trial.trial is True and not trial.failed
    assert read_offline(xdg, "w") is None and runner.offline is None
    assert events(records, "watch.online")[0]["reason"] == "trial poll succeeded"
    assert alerts[-1] == ("online", "w", "trial poll succeeded")
    assert events(records, "poll.start")[-1]["trial"] is True


def test_disable_and_enable_through_marker_files(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='retry_after = "1m"\n[hooks]\ncheck = ["true"]\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    write_offline(xdg, "w", OfflineMarker(reason="disabled by user", since=iso_time(clock()), by_user=True))
    clock.now += 10_000
    assert runner.poll_once(clock()) is None
    assert events(records, "watch.offline")[0]["level"] == "WARNING"
    clear_offline(xdg, "w")
    assert runner.poll_once(clock()) is not None
    assert events(records, "watch.online")[0]["reason"] == "enabled by user"
    assert alerts == [("online", "w", "enabled by user")]


def test_offline_survives_restart(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    write_offline(xdg, "w", OfflineMarker(reason="5 consecutive failed polls", since=iso_time(1.0)))
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.offline is not None
    assert runner.poll_once(clock()) is None


def test_poll_interrupted_by_shutdown_does_not_go_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 1\n[hooks]\ncheck = "exit 9"\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.stop()
    runner.poll_once(clock())
    assert read_offline(xdg, "w") is None


def test_config_update_applies_next_poll_and_keeps_state(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.poll_once(clock())
    (watch_dir / "config.toml").write_text('interval = "5m"\n[hooks]\ncheck = ["true"]\n')
    runner.update_config(load_watch_config(watch_dir))
    clock.now += 60
    report = runner.poll_once(clock())
    assert report.before.condition is True
    assert runner.config.interval == 300
    assert runner.seconds_until_due(clock()) == 300


def test_parked_watch_never_polls(make_watch, runner_for):
    watch_dir = make_watch("w", config='enabled = false\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.poll_once(clock()) is None
    assert runner.snapshot(clock())["next_poll"] is None


def test_snapshot(make_watch, runner_for):
    watch_dir = make_watch("w", config='description = "d"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.poll_once(clock())
    snap = runner.snapshot(clock())
    assert snap["description"] == "d" and snap["condition"] is True and snap["failures"] == 0
    assert snap["last_poll"]["outcome"] == "true" and snap["offline"] is None
    assert snap["next_poll"] == iso_time(clock() + 60)


def test_thread_loop_polls_and_stops(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "1s"\n[hooks]\ncheck = "echo x >> polls"\n')
    runner = runner_for(watch_dir, [], [], time.time)
    runner.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        polls = watch_dir / "polls"
        if polls.exists() and len(polls.read_text().splitlines()) >= 2:
            break
        time.sleep(0.1)
    runner.stop()
    assert runner.join(10) is True
    assert len((watch_dir / "polls").read_text().splitlines()) >= 2
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.scheduler'`

- [ ] **Step 3: Implement.** In `src/keepwatch/pollengine.py`:

- add a field `trial: bool = False` at the end of `PollReport`, and `"trial": self.trial,` to `to_dict()`;
- add to `PollEngine`:

```python
    def set_global_config(self, global_config: GlobalConfig) -> None:
        """Use a reloaded global config from the next hook call on."""
        self._global = global_config
```

- in `poll`, add the keyword parameter `trial: bool = False` after `dry_run`, pass `trial=trial` to the `poll.start` record, and `trial=trial` to the returned `PollReport`.

Create `src/keepwatch/scheduler.py`:

```python
"""One watch's schedule: when it is due, backoff, going offline and coming back (spec sections 7-8)."""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from keepwatch.config import WatchConfig
from keepwatch.locks import hold_lock
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import Paths
from keepwatch.pollengine import PollEngine, PollReport
from keepwatch.state import ANSWERS, initial_state, next_delay, should_go_offline

Alert = Callable[[str, str, str], None]
CRASH_RETRY = 60.0


@dataclass(frozen=True)
class LastPoll:
    poll_id: str
    at: str
    outcome: str
    reason: str | None
    failed: bool
    trial: bool
    results: list[dict[str, Any]]


def _failure_summary(report: PollReport) -> str:
    for result in report.results:
        if not result.succeeded:
            return f"{result.hook} {result.status}: {result.reason}"
    return f"check {report.outcome.value}: {report.reason}"


class WatchRunner:
    """Owns one watch's state. poll_once() is synchronous; start() runs it on a thread."""

    def __init__(
        self,
        config: WatchConfig,
        *,
        engine: PollEngine,
        paths: Paths,
        sink: Sink,
        alert: Alert,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = config.name
        self._config = config
        self._engine = engine
        self._paths = paths
        self._sink = sink
        self._alert = alert
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: WatchConfig | None = None
        self.state = initial_state(config.initial_condition)
        self.next_due = clock()
        self.offline: OfflineMarker | None = read_offline(paths, self.name)
        self.last_trial: float | None = None
        self.last_poll: LastPoll | None = None
        self.config_error: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def config(self) -> WatchConfig:
        return self._config

    def update_config(self, config: WatchConfig) -> None:
        """Use a new config from the next poll on. Safe to call from another thread."""
        with self._lock:
            self._pending = config
        self._wake.set()

    def _apply_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            self._config = pending

    def _went_online(self, reason: str) -> None:
        self._sink(make_record("watch.online", watch=self.name, reason=reason))
        self._alert("online", self.name, reason)

    def _refresh_offline(self, now: float) -> None:
        marker = read_offline(self._paths, self.name)
        if self.offline is not None and marker is None:
            self.offline = None
            self.last_trial = None
            self.state = replace(self.state, failures=0)
            self.next_due = now
            self._went_online("enabled by user")
        elif self.offline is None and marker is not None:
            self.offline = marker
            self._sink(
                make_record(
                    "watch.offline",
                    level="WARNING" if marker.by_user else "CRITICAL",
                    watch=self.name,
                    reason=marker.reason,
                    by_user=marker.by_user,
                )
            )
        else:
            self.offline = marker

    def seconds_until_due(self, now: float) -> float | None:
        """Seconds until the next poll, 0 if due now, None if the watch will not poll by itself."""
        config = self._config
        if not config.enabled:
            return None
        if self.offline is not None:
            if self.offline.by_user or config.retry_after is None:
                return None
            base = self.last_trial if self.last_trial is not None else self.offline.since_epoch()
            return max(base + config.retry_after - now, 0.0)
        return max(self.next_due - now, 0.0)

    def poll_once(self, now: float) -> PollReport | None:
        """Poll if due (a trial poll when offline with retry_after). Returns None when not due."""
        self._apply_pending()
        self._refresh_offline(now)
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
        config = self._config
        trial = self.offline is not None
        if trial:
            self.last_trial = now
        with hold_lock(self._paths.watch_lock(self.name)):
            report = self._engine.poll(config, self.state, trial=trial)
        self.state = report.after
        finished = self._clock()
        self.last_poll = LastPoll(
            poll_id=report.poll_id,
            at=iso_time(finished),
            outcome=report.outcome.value,
            reason=report.reason,
            failed=report.failed,
            trial=trial,
            results=[{"hook": r.hook, "status": r.status, "reason": r.reason} for r in report.results],
        )
        if self._stop.is_set():
            return report
        if trial:
            if not report.failed and report.outcome in ANSWERS:
                clear_offline(self._paths, self.name)
                self.offline = None
                self.last_trial = None
                self.next_due = finished + config.interval
                self._went_online("trial poll succeeded")
            return report
        if should_go_offline(self.state.failures, config.max_failures):
            marker = OfflineMarker(
                reason=f"{self.state.failures} consecutive failed polls",
                since=iso_time(finished),
                last_failure=_failure_summary(report),
            )
            write_offline(self._paths, self.name, marker)
            self.offline = marker
            self._sink(
                make_record(
                    "watch.offline",
                    level="CRITICAL",
                    watch=self.name,
                    reason=marker.reason,
                    by_user=False,
                    last_failure=marker.last_failure,
                )
            )
            self._alert("offline", self.name, f"{marker.reason}; last: {marker.last_failure}")
        self.next_due = finished + next_delay(config.interval, self.state.failures)
        return report

    def snapshot(self, now: float) -> dict[str, Any]:
        """This watch's entry in status.json."""
        config = self._config
        wait = self.seconds_until_due(now)
        return {
            "description": config.description,
            "watch_dir": str(config.watch_dir),
            "enabled": config.enabled,
            "interval": config.interval,
            "condition": self.state.condition,
            "pending_edge": self.state.pending_edge.value if self.state.pending_edge else None,
            "failures": self.state.failures,
            "offline": self.offline.to_dict() if self.offline else None,
            "last_poll": asdict(self.last_poll) if self.last_poll else None,
            "next_poll": None if wait is None else iso_time(now + wait),
            "config_error": self.config_error,
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"keepwatch-{self.name}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                report = self.poll_once(self._clock())
            except Exception as exc:
                self._sink(
                    make_record(
                        "watch.crash",
                        level="ERROR",
                        watch=self.name,
                        error=f"{type(exc).__name__}: {exc}",
                        traceback=traceback.format_exc(),
                    )
                )
                self.next_due = self._clock() + CRASH_RETRY
                report = None
            if report is None:
                wait = self.seconds_until_due(self._clock())
                self._wake.wait(1.0 if wait is None else min(wait, 1.0))
                self._wake.clear()

    def stop(self) -> None:
        """Stop after the current poll. Polls that end after this never change offline state."""
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/pollengine.py src/keepwatch/scheduler.py tests/test_scheduler.py
git commit -m "Add the per-watch scheduler: backoff, offline, trial polls"
```

---

### Task 6: The service

**Files:**
- Modify: `src/keepwatch/runner.py` (track running process groups, `terminate_all`)
- Create: `src/keepwatch/service.py`
- Test: `tests/test_runner_command.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: Tasks 4–5; `load_global_config`, `load_watch_config`, `discover_watches`, `CONFIG_NAME` (Plan 1).
- Produces:
  - `Runner.terminate_all() -> None` — SIGTERM to every hook process group currently running
  - `service.file_signature(path: Path) -> tuple[int, int] | None`
  - `Service(*, paths: Paths, config_path: Path, sink: Sink, only: Collection[str] = (), start_threads: bool = True, clock: Callable[[], float] = time.time, runner: Runner | None = None)` with attributes `global_config`, `engine`, `runners: dict[str, WatchRunner]` and methods `start()` (raises `ConfigError` on a broken global config), `tick()`, `run(stop: threading.Event)`, `stop()`, `write_status(now, *, running=True)`
  - Records: `service.start`, `service.stop`, `config.loaded`, `config.error` (`error`, plus `watch`/`path`/`running_previous` for watch configs), `watch.added`, `watch.changed`, `watch.removed`
  - `status.json`: `{"service": {running, pid, version, started, updated, config, config_error, only}, "watches": {name: WatchRunner.snapshot()}, "invalid_watches": {name: error}, "problems": [str]}`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_runner_command.py` (add `import threading` and `import time` at the top):

```python
def test_terminate_all_stops_running_hooks(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30"\n')
    runner = Runner()
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(0.5)
    runner.terminate_all()
    thread.join(10)
    assert results and results[0].status == "error"
    assert results[0].signal == 15
```

`tests/test_service.py`:

```python
import json
import textwrap

import pytest

from keepwatch.config import ConfigError
from keepwatch.service import Service


def make_service(xdg, records, **kwargs):
    return Service(paths=xdg, config_path=xdg.config_file, sink=records.append, start_threads=False, **kwargs)


def events(records, name):
    return [r for r in records if r["event"] == name]


def write_global(xdg, text):
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(textwrap.dedent(text))


def test_start_loads_watches_and_writes_status(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    assert list(service.runners) == ["a"]
    assert [r["watch"] for r in events(records, "watch.added")] == ["a"]
    status = json.loads(xdg.status_file.read_text())
    assert status["service"]["running"] is True
    assert status["watches"]["a"]["condition"] is False


def test_watches_are_added_and_removed_live(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    service.tick()
    assert sorted(service.runners) == ["a", "b"]
    runner_a = service.runners["a"]
    (xdg.default_watches_dir / "a").rename(xdg.default_watches_dir / "_a")
    service.tick()
    assert list(service.runners) == ["b"]
    assert runner_a._stop.is_set()
    assert [r["watch"] for r in events(records, "watch.removed")] == ["a"]


def test_changed_config_is_handed_to_the_runner(xdg, make_watch):
    watch_dir = make_watch("a", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    (watch_dir / "config.toml").write_text('interval = "300s"\n[hooks]\ncheck = ["true"]\n')
    service.tick()
    assert events(records, "watch.changed")[0]["watch"] == "a"
    service.runners["a"].poll_once(service.runners["a"].next_due)
    assert service.runners["a"].config.interval == 300


def test_broken_edit_keeps_last_good_config(xdg, make_watch):
    watch_dir = make_watch("a", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    (watch_dir / "config.toml").write_text('intervall = "60s"\n[hooks]\ncheck = ["true"]\n')
    service.tick()
    service.tick()
    errors = events(records, "config.error")
    assert len(errors) == 1
    assert errors[0]["watch"] == "a" and errors[0]["running_previous"] is True
    assert "unknown key 'intervall'" in errors[0]["error"]
    assert service.runners["a"].config.interval == 60
    status = json.loads(xdg.status_file.read_text())
    assert "intervall" in status["watches"]["a"]["config_error"]
    (watch_dir / "config.toml").write_text('interval = "60s"\n[hooks]\ncheck = ["true"]\n  \n')
    service.tick()
    assert service.runners["a"].config_error is None


def test_new_invalid_watch_is_not_started(xdg, make_watch):
    make_watch("bad", config="interval = 0\n")
    records = []
    service = make_service(xdg, records)
    service.start()
    assert service.runners == {}
    status = json.loads(xdg.status_file.read_text())
    assert "interval" in status["invalid_watches"]["bad"]


def test_broken_global_config(xdg, make_watch):
    write_global(xdg, "watch_dirs = 5\n")
    with pytest.raises(ConfigError):
        make_service(xdg, []).start()
    write_global(xdg, "reload_interval = \"5s\"\n")
    records = []
    service = make_service(xdg, records)
    service.start()
    write_global(xdg, "watch_dirs = 5 \n")
    service.tick()
    assert len(events(records, "config.error")) == 1
    assert service.global_config.reload_interval == 5


def test_global_defaults_reach_running_watches(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    write_global(xdg, '[defaults]\ninterval = "2m"\n')
    service.tick()
    assert events(records, "config.loaded")
    runner = service.runners["a"]
    runner.poll_once(runner.next_due)
    assert runner.config.interval == 120


def test_only_limits_the_watches(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    service = make_service(xdg, [], only={"b"})
    service.start()
    assert list(service.runners) == ["b"]


def test_stop(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    service.stop()
    assert service.runners["a"]._stop.is_set()
    assert records[-1]["event"] == "service.stop"
    assert json.loads(xdg.status_file.read_text())["service"]["running"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_runner_command.py tests/test_service.py -q`
Expected: FAIL (`AttributeError: 'Runner' object has no attribute 'terminate_all'`; `ModuleNotFoundError: No module named 'keepwatch.service'`)

- [ ] **Step 3: Implement.** In `src/keepwatch/runner.py`, in `Runner.__init__` add:

```python
        self._active: set[int] = set()
        self._active_lock = threading.Lock()
```

and add these methods to `Runner`:

```python
    def terminate_all(self) -> None:
        """Send SIGTERM to every hook process group that is running now (used at shutdown)."""
        with self._active_lock:
            groups = list(self._active)
        for pgid in groups:
            _signal_group(pgid, signal.SIGTERM)

    def _track(self, pid: int) -> None:
        with self._active_lock:
            self._active.add(pid)

    def _untrack(self, pid: int) -> None:
        with self._active_lock:
            self._active.discard(pid)
```

In both `_run_python` and `_run_command`, replace the line

```python
        returncode, timed_out = self._supervise(proc, (out, err, results), call.timeout)
```

(in `_run_command` it is `self._supervise(proc, (out, err), call.timeout)`) with the tracked form:

```python
        self._track(proc.pid)
        try:
            returncode, timed_out = self._supervise(proc, (out, err, results), call.timeout)
        finally:
            self._untrack(proc.pid)
```

(using `(out, err)` in `_run_command`, keeping its indentation).

Create `src/keepwatch/service.py`:

```python
"""The long-running service: master tick, live config, watch runners, status file (spec sections 3, 11)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path

from keepwatch import __version__
from keepwatch.alerts import run_alert
from keepwatch.config import (
    CONFIG_NAME,
    ConfigError,
    GlobalConfig,
    discover_watches,
    load_global_config,
    load_watch_config,
)
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import iso_time
from keepwatch.paths import Paths, write_json_atomic
from keepwatch.pollengine import PollEngine
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, Runner
from keepwatch.scheduler import WatchRunner

Signature = tuple[int, int] | None
SHUTDOWN_GRACE = KILL_GRACE + DRAIN_GRACE + 3.0


def file_signature(path: Path) -> Signature:
    """(mtime_ns, size), or None if the file does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_mtime_ns, info.st_size)


class Service:
    def __init__(
        self,
        *,
        paths: Paths,
        config_path: Path,
        sink: Sink,
        only: Collection[str] = (),
        start_threads: bool = True,
        clock: Callable[[], float] = time.time,
        runner: Runner | None = None,
    ) -> None:
        self.paths = paths
        self.config_path = config_path
        self._sink = sink
        self._only = frozenset(only)
        self._start_threads = start_threads
        self._clock = clock
        self._runner = runner or Runner()
        self.global_config: GlobalConfig | None = None
        self.engine: PollEngine | None = None
        self.runners: dict[str, WatchRunner] = {}
        self._retired: list[WatchRunner] = []
        self._global_signature: Signature = None
        self._global_error: str | None = None
        self._watch_signatures: dict[str, tuple[Path, Signature]] = {}
        self._watch_errors: dict[str, str] = {}
        self._reported_problems: frozenset[str] = frozenset()
        self._started = clock()

    def start(self) -> None:
        """Load the global config (raises ConfigError if broken), log service.start, run the first tick."""
        self._global_signature = file_signature(self.config_path)
        self.global_config = load_global_config(self.config_path, self.paths)
        self.engine = PollEngine(
            runner=self._runner,
            paths=self.paths,
            global_config=self.global_config,
            sink=self._sink,
            pid=os.getpid(),
        )
        self._started = self._clock()
        self._sink(
            make_record(
                "service.start",
                version=__version__,
                config=str(self.config_path),
                watch_dirs=[str(path) for path in self.global_config.watch_dirs],
                only=sorted(self._only),
            )
        )
        self.tick()

    def run(self, stop: threading.Event) -> None:
        """start(), then tick every reload_interval until `stop` is set, then stop()."""
        self.start()
        try:
            while not stop.wait(self.global_config.reload_interval):
                self.tick()
        finally:
            self.stop()

    def tick(self) -> None:
        now = self._clock()
        self._reload_global()
        discovery = discover_watches(self.global_config)
        problems = frozenset(str(problem) for problem in discovery.problems)
        for problem in sorted(problems - self._reported_problems):
            self._sink(make_record("config.error", level="ERROR", error=problem))
        self._reported_problems = problems
        wanted = {
            name: path
            for name, path in discovery.watches.items()
            if not self._only or name in self._only
        }
        for name, path in wanted.items():
            self._sync_watch(name, path)
        for name in sorted(set(self.runners) - set(wanted)):
            runner = self.runners.pop(name)
            runner.stop()
            self._retired.append(runner)
            self._watch_signatures.pop(name, None)
            self._sink(make_record("watch.removed", watch=name))
        for name in sorted(set(self._watch_errors) - set(wanted)):
            del self._watch_errors[name]
            self._watch_signatures.pop(name, None)
        self.write_status(now)

    def _reload_global(self) -> None:
        signature = file_signature(self.config_path)
        if signature == self._global_signature:
            return
        self._global_signature = signature
        try:
            config = load_global_config(self.config_path, self.paths)
        except ConfigError as exc:
            self._global_error = str(exc)
            self._sink(make_record("config.error", level="ERROR", path=str(self.config_path), error=str(exc)))
            return
        self.global_config = config
        self._global_error = None
        self.engine.set_global_config(config)
        self._watch_signatures.clear()
        self._sink(make_record("config.loaded", path=str(self.config_path)))

    def _sync_watch(self, name: str, path: Path) -> None:
        signature = (path, file_signature(path / CONFIG_NAME))
        if self._watch_signatures.get(name) == signature:
            return
        self._watch_signatures[name] = signature
        runner = self.runners.get(name)
        try:
            config = load_watch_config(path, self.global_config.defaults)
        except ConfigError as exc:
            error = str(exc)
            self._watch_errors[name] = error
            if runner is not None:
                runner.config_error = error
            self._sink(
                make_record(
                    "config.error",
                    level="ERROR",
                    watch=name,
                    path=str(path / CONFIG_NAME),
                    error=error,
                    running_previous=runner is not None,
                )
            )
            return
        self._watch_errors.pop(name, None)
        if runner is not None:
            runner.config_error = None
            if runner.config != config:
                runner.update_config(config)
                self._sink(make_record("watch.changed", watch=name))
            return
        runner = WatchRunner(
            config,
            engine=self.engine,
            paths=self.paths,
            sink=self._sink,
            alert=self._alert,
            clock=self._clock,
        )
        self.runners[name] = runner
        if self._start_threads:
            runner.start()
        self._sink(make_record("watch.added", watch=name, enabled=config.enabled))

    def _alert(self, event: str, watch: str, reason: str) -> None:
        config = self.global_config
        run_alert(
            config.alert_command,
            event=event,
            watch=watch,
            reason=reason,
            environment=config.environment,
            timeout=float(config.defaults.get("action_timeout", 60.0)),
            sink=self._sink,
            capture_bytes=config.log.capture_bytes,
        )

    def stop(self) -> None:
        """Stop every runner, terminate running hooks, wait for polls to end, write the final status."""
        runners = [*self.runners.values(), *self._retired]
        for runner in runners:
            runner.stop()
        self._runner.terminate_all()
        deadline = time.monotonic() + SHUTDOWN_GRACE
        for runner in runners:
            runner.join(max(deadline - time.monotonic(), 0.0))
        self._sink(make_record("service.stop"))
        self.write_status(self._clock(), running=False)

    def write_status(self, now: float, *, running: bool = True) -> None:
        document = {
            "service": {
                "running": running,
                "pid": os.getpid(),
                "version": __version__,
                "started": iso_time(self._started),
                "updated": iso_time(now),
                "config": str(self.config_path),
                "config_error": self._global_error,
                "only": sorted(self._only),
            },
            "watches": {name: runner.snapshot(now) for name, runner in sorted(self.runners.items())},
            "invalid_watches": {
                name: error for name, error in sorted(self._watch_errors.items()) if name not in self.runners
            },
            "problems": sorted(self._reported_problems),
        }
        write_json_atomic(self.paths.status_file, document)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/runner.py src/keepwatch/service.py tests/test_runner_command.py tests/test_service.py
git commit -m "Add the service: master tick, live reload, runners and status file"
```

---

### Task 7: `keepwatch run`

**Files:**
- Modify: `src/keepwatch/logstore.py`, `src/keepwatch/cli.py`
- Test: `tests/test_logstore.py`, `tests/test_cli_run.py`

**Interfaces:**
- Consumes: `Service` (Task 6).
- Produces:
  - `logstore.LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}`; `level_filter(sink: Sink, minimum: str) -> Sink`; `QueueSink(sink)` — callable as a `Sink` from any thread, delivers in order on one thread, `close(timeout=10.0)` drains and stops
  - `cli.COMMAND_GROUPS` with panels Run (`run`), Develop (`validate`, `poll`), Inspect (`status`, `logs`), Control (`enable`, `disable`, `rename`)
  - `keepwatch run [--watch NAME ...] [-v] [-q]` — exit 0 after SIGTERM/SIGINT, 1 if another service holds the lock or the global config is broken

- [ ] **Step 1: Write the failing tests.** In `tests/test_logstore.py`, add `import time as _time` to the imports at the top and change the logstore import to `from keepwatch.logstore import LogWriter, QueueSink, fan_out, level_filter, make_record` (imports must stay at the top of the file; ruff rejects them anywhere else). Then append:

```python
def test_level_filter():
    seen = []
    emit = level_filter(seen.append, "WARNING")
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "BOGUS"):
        emit({"level": level})
    assert [r["level"] for r in seen] == ["WARNING", "ERROR", "CRITICAL"]


def test_queue_sink_delivers_in_order_on_one_thread():
    seen = []

    def slow(record):
        _time.sleep(0.001)
        seen.append((record["n"], threading.current_thread().name))

    sink = QueueSink(slow)
    threads = [threading.Thread(target=lambda base=b: [sink({"n": base + i}) for i in range(20)]) for b in (0, 100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close()
    assert sorted(n for n, _ in seen) == sorted([*range(20), *range(100, 120)])
    assert {name for _, name in seen} == {"keepwatch-log"}


def test_queue_sink_survives_a_failing_sink():
    seen = []

    def flaky(record):
        if record["n"] == 1:
            raise RuntimeError("boom")
        seen.append(record["n"])

    sink = QueueSink(flaky)
    for n in range(3):
        sink({"n": n})
    sink.close()
    assert seen == [0, 2]
```

`tests/test_cli_run.py`:

```python
import json
import os
import signal
import subprocess
import sys
import time

from click.testing import CliRunner

from keepwatch.cli import cli

KEEPWATCH = [sys.executable, "-c", "from keepwatch.cli import main; main()"]

COUNTER = '''
    def check(ctx):
        return True

    def on_true(ctx):
        path = ctx.watch_dir / "count"
        count = int(path.read_text()) if path.exists() else 0
        path.write_text(str(count + 1))
'''


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_run_polls_and_stops_cleanly(xdg, make_watch):
    watch_dir = make_watch("w", config='interval = "1s"\n', files={"watch.py": COUNTER})
    count = watch_dir / "count"
    proc = subprocess.Popen([*KEEPWATCH, "run"], env=dict(os.environ), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert wait_for(lambda: count.exists() and int(count.read_text()) >= 2)
        second = subprocess.run([*KEEPWATCH, "run"], env=dict(os.environ), capture_output=True, text=True, timeout=30)
        assert second.returncode == 1
        assert "already running" in second.stdout + second.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, out + err
    events = [json.loads(line)["event"] for line in xdg.log_file.read_text().splitlines()]
    assert "service.start" in events
    assert events[-1] == "service.stop"
    assert json.loads(xdg.status_file.read_text())["service"]["running"] is False


def test_run_refuses_a_broken_global_config(xdg):
    xdg.config_home.mkdir(parents=True)
    xdg.config_file.write_text("watch_dirs = 5\n")
    result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == 1
    assert "'watch_dirs' must be a non-empty list" in result.output


def test_help_lists_the_run_panel(xdg):
    output = CliRunner().invoke(cli, ["--help"]).output
    assert "Run" in output and "run " in output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_logstore.py tests/test_cli_run.py -q`
Expected: FAIL (`ImportError: cannot import name 'QueueSink'`; `No such command 'run'`)

- [ ] **Step 3: Implement.** Append to `src/keepwatch/logstore.py` (add `import queue` and `import traceback` to its imports):

```python
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_STOP = object()


def level_filter(sink: Sink, minimum: str) -> Sink:
    """Pass on records whose level is at least `minimum` (unknown levels count as INFO)."""
    threshold = LEVELS[minimum]

    def emit(record: dict[str, Any]) -> None:
        if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) >= threshold:
            sink(record)

    return emit


class QueueSink:
    """A sink any thread can call; records are delivered in order on one writer thread."""

    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, name="keepwatch-log", daemon=True)
        self._thread.start()

    def __call__(self, record: dict[str, Any]) -> None:
        self._queue.put(record)

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _STOP:
                return
            try:
                self._sink(record)
            except Exception:
                traceback.print_exc()

    def close(self, timeout: float = 10.0) -> None:
        """Deliver everything queued so far, then stop the writer thread."""
        self._queue.put(_STOP)
        self._thread.join(timeout)
```

In `src/keepwatch/cli.py`:

Replace `COMMAND_GROUPS` with:

```python
COMMAND_GROUPS = {
    "keepwatch": [
        {"name": "Run", "commands": ["run"]},
        {"name": "Develop", "commands": ["validate", "poll"]},
        {"name": "Inspect", "commands": ["status", "logs"]},
        {"name": "Control", "commands": ["enable", "disable", "rename"]},
    ]
}
```

Add imports: `import signal`, `import threading`, `from contextlib import ExitStack` (next to the existing `contextmanager` import), change `from keepwatch.locks import hold_lock` to `from keepwatch.locks import LockBusy, hold_lock`, change the logstore import to `from keepwatch.logstore import LogWriter, QueueSink, fan_out, level_filter`, and add `from keepwatch.service import Service`.

Add the command before `def main()`:

```python
@cli.command(name="run")
@click.option("--watch", "only", multiple=True, metavar="NAME", help="Run only this watch (repeatable). For development.")
@click.option("-v", "--verbose", is_flag=True, help="Print every record, including captured output.")
@click.option("-q", "--quiet", is_flag=True, help="Print only warnings and errors.")
@click.pass_obj
def run_service(app: App, only: tuple[str, ...], verbose: bool, quiet: bool) -> None:
    """Run the service in the foreground: every watch, polled forever, with config changes picked up live.

    This is what the login service runs. Stop it with Ctrl-C or SIGTERM: running hooks are terminated and the
    service exits once their polls end. Only one service runs at a time.

    Terminal output: on a terminal, one line per event (only warnings and errors with -q; captured output too
    with -v). When stdout is not a terminal (for example under systemd), only warnings and errors are printed
    unless -v is given. The log file always gets every record; read it with `keepwatch logs`.

    Exit status: 0 after a clean stop, 1 if another service is running or the global config is broken.
    """
    paths = app.paths
    try:
        ensure_private_dir(paths.runtime)
        remove_stale_process_dirs(paths)
    except PathError as exc:
        _fail(str(exc))
    with ExitStack() as stack:
        try:
            stack.enter_context(hold_lock(paths.service_lock, blocking=False))
        except LockBusy:
            _fail("another keepwatch service is already running; see: keepwatch status")
        try:
            global_config = app.load_global()
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
        if quiet:
            minimum = "WARNING"
        elif verbose:
            minimum = "DEBUG"
        elif plain_output():
            minimum = "WARNING"
        else:
            minimum = "INFO"
        writer = LogWriter(paths.log_file, max_bytes=global_config.log.max_bytes, backups=global_config.log.backups)
        printer = level_filter(ConsolePrinter(make_console(), verbose=verbose), minimum)
        sink = QueueSink(fan_out(writer.write, printer))
        stack.callback(sink.close)
        stack.callback(shutil.rmtree, paths.process_dir(os.getpid()), True)
        stop = threading.Event()

        def request_stop(signum: int, frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        service = Service(paths=paths, config_path=app.config_path, sink=sink, only=set(only))
        try:
            service.run(stop)
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
    raise SystemExit(0)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Try it by hand** (in a throwaway XDG tree, never your real `~/.config`):

```bash
T=$(mktemp -d) && mkdir -m 700 "$T/rt" && export XDG_CONFIG_HOME=$T/c XDG_STATE_HOME=$T/s XDG_RUNTIME_DIR=$T/rt
mkdir -p $T/c/keepwatch/watches/hello && printf 'interval = "2s"\n[hooks]\ncheck = ["true"]\non_true = "date"\n' > $T/c/keepwatch/watches/hello/config.toml
timeout -s TERM 7 uv run keepwatch run; echo "exit $?"
```

Expected: a few poll lines, then exit 0 (124 from `timeout` is also fine: it reports the signal it sent).

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/logstore.py src/keepwatch/cli.py tests/test_logstore.py tests/test_cli_run.py
git commit -m "Add keepwatch run with a single log writer thread and clean shutdown"
```

---

### Task 8: `keepwatch status`

**Files:**
- Create: `src/keepwatch/statusview.py`
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_cli_status.py`

**Interfaces:**
- Consumes: Tasks 4, 6; `Discovery` (Plan 1).
- Produces:
  - `service_running(paths) -> bool` (true when another process holds the service lock)
  - `read_status(paths) -> dict | None`; `orphaned_state(paths, discovery) -> list[str]`
  - `collect_status(paths, discovery, names: list[str]) -> dict` — `{"service": {"running", ...}, "watches": {name: entry}, "invalid_watches", "problems", "orphaned_state"}`; each entry is the service's snapshot when it is running (else `{"known_to_service": False}`), plus `offline` read from disk and `exists`
  - `format_status(document: dict, now: float) -> list[str]`
  - `keepwatch status [NAME] [--json]` — exit 0 (1 only for an unknown NAME)

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_status.py`

```python
import json
import re
import time

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.locks import hold_lock
from keepwatch.offline import OfflineMarker, write_offline
from keepwatch.paths import ensure_private_dir
from keepwatch.service import Service


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_status_without_a_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    write_offline(xdg, "b", OfflineMarker(reason="disabled by user", since="2026-09-30T10:00:00.000-05:00", by_user=True))
    (xdg.state_home / "watches" / "gone").mkdir(parents=True)
    result = run("status")
    assert result.exit_code == 0
    assert "service: not running" in result.output
    assert re.search(r"^a\s+idle", result.output, re.M)
    assert re.search(r"^b\s+offline", result.output, re.M)
    assert "offline since 2026-09-30T10:00:00.000-05:00: disabled by user" in result.output
    assert "orphaned state: gone" in result.output
    data = json.loads(run("status", "--json").output)
    assert data["service"]["running"] is False
    assert data["orphaned_state"] == ["gone"]
    assert data["watches"]["b"]["offline"]["by_user"] is True
    assert data["watches"]["a"]["known_to_service"] is False


def test_status_with_a_running_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    ensure_private_dir(xdg.runtime)
    service = Service(paths=xdg, config_path=xdg.config_file, sink=lambda record: None, start_threads=False)
    service.start()
    service.runners["a"].poll_once(time.time())
    service.tick()
    with hold_lock(xdg.service_lock):
        data = json.loads(run("status", "--json").output)
        text = run("status", "a").output
    assert data["service"]["running"] is True
    entry = data["watches"]["a"]
    assert entry["condition"] is True
    assert entry["last_poll"]["outcome"] == "true"
    assert re.search(r"^a\s+online\s+condition TRUE\s+failures 0\s+last true", text, re.M)
    assert "service: running" in text


def test_status_for_an_unknown_watch(xdg, make_watch):
    make_watch("psg-export", config='[hooks]\ncheck = ["true"]\n')
    result = run("status", "psg-exprot")
    assert result.exit_code == 1
    assert "did you mean 'psg-export'" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_status.py -q`
Expected: FAIL with `No such command 'status'`

- [ ] **Step 3: Implement** — `src/keepwatch/statusview.py`

```python
"""keepwatch status: combine status.json, offline markers and the config on disk."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from keepwatch.config import Discovery
from keepwatch.durations import format_duration
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.offline import read_offline
from keepwatch.paths import Paths


def service_running(paths: Paths) -> bool:
    if not paths.runtime.is_dir():
        return False
    try:
        with hold_lock(paths.service_lock, blocking=False):
            return False
    except LockBusy:
        return True


def read_status(paths: Paths) -> dict[str, Any] | None:
    try:
        data = json.loads(paths.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def orphaned_state(paths: Paths, discovery: Discovery) -> list[str]:
    root = paths.state_home / "watches"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir() and entry.name not in discovery.watches)


def collect_status(paths: Paths, discovery: Discovery, names: list[str]) -> dict[str, Any]:
    running = service_running(paths)
    saved = (read_status(paths) or {}) if running else {}
    saved_watches = saved.get("watches", {})
    watches = {}
    for name in names or list(discovery.watches):
        entry = dict(saved_watches.get(name) or {"known_to_service": False})
        marker = read_offline(paths, name)
        entry["offline"] = marker.to_dict() if marker else None
        entry["exists"] = name in discovery.watches
        watches[name] = entry
    service: dict[str, Any] = {"running": running}
    if running:
        saved_service = saved.get("service", {})
        service.update({key: saved_service.get(key) for key in ("pid", "version", "started", "updated", "config_error")})
    return {
        "service": service,
        "watches": watches,
        "invalid_watches": saved.get("invalid_watches", {}),
        "problems": [str(problem) for problem in discovery.problems],
        "orphaned_state": orphaned_state(paths, discovery),
    }


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _ago(value: Any, now: float) -> str:
    stamp = _epoch(value)
    return "?" if stamp is None else format_duration(max(int(now - stamp), 0))


def _until(value: Any, now: float) -> str:
    stamp = _epoch(value)
    return "?" if stamp is None else format_duration(max(int(stamp - now), 0))


def _watch_line(name: str, entry: dict[str, Any], now: float) -> str:
    if entry.get("offline"):
        label = "offline"
    elif entry.get("enabled") is False:
        label = "parked"
    elif entry.get("known_to_service") is False:
        label = "idle"
    else:
        label = "online"
    parts = [f"{name:<20} {label:<8}"]
    if "condition" in entry:
        parts.append(f"condition {'TRUE' if entry['condition'] else 'FALSE'}")
    if "failures" in entry:
        parts.append(f"failures {entry['failures']}")
    last = entry.get("last_poll")
    if last:
        parts.append(f"last {last['outcome']}{' FAILED' if last['failed'] else ''} {_ago(last['at'], now)} ago")
    if entry.get("next_poll"):
        parts.append(f"next in {_until(entry['next_poll'], now)}")
    return "  ".join(parts)


def format_status(document: dict[str, Any], now: float) -> list[str]:
    service = document["service"]
    if service["running"]:
        lines = [
            f"service: running (pid {service.get('pid')}, version {service.get('version')}, "
            f"status updated {_ago(service.get('updated'), now)} ago)"
        ]
        if service.get("config_error"):
            lines.append(f"    global config error (running the previous config): {service['config_error']}")
    else:
        lines = ["service: not running (start it with: keepwatch run)"]
    for name, entry in document["watches"].items():
        lines.append(_watch_line(name, entry, now))
        offline = entry.get("offline")
        if offline:
            lines.append(f"    offline since {offline['since']}: {offline['reason']}")
            if offline.get("last_failure"):
                lines.append(f"    last failure: {offline['last_failure']}")
        if entry.get("config_error"):
            lines.append(f"    config error (running the previous config): {entry['config_error'].splitlines()[0]}")
        if not entry.get("exists", True):
            lines.append("    the watch directory no longer exists")
    for name, error in document.get("invalid_watches", {}).items():
        lines.append(f"{name:<20} invalid  not started: {error.splitlines()[0]}")
    for problem in document.get("problems", []):
        lines.append(f"problem: {problem}")
    for name in document.get("orphaned_state", []):
        lines.append(f"orphaned state: {name} (state of a watch that no longer exists; to rename a watch use keepwatch rename)")
    return lines
```

In `src/keepwatch/cli.py`, add `import time` and `from keepwatch.statusview import collect_status, format_status` to the imports, and add the command before `def main()`:

```python
@cli.command()
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the status as one JSON document.")
@click.pass_obj
def status(app: App, name: str | None, as_json: bool) -> None:
    """Show whether the service runs and each watch's state.

    Per watch: online, offline (with the reason and last failure), parked (enabled = false) or idle (the
    service is not running), the TRUE/FALSE condition, consecutive failures, the last poll and the next one.
    Also lists config problems and orphaned state directories (state of a watch that no longer exists).
    The service refreshes this every reload_interval.

    Exit status: 0, or 1 if NAME is not a known watch.
    """
    try:
        discovery = discover_watches(app.load_global())
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    names = []
    if name is not None:
        if name not in discovery.watches and not app.paths.watch_state_dir(name).is_dir():
            try:
                find_watch(discovery, name)
            except WatchNotFound as exc:
                _fail(str(exc))
        names = [name]
    document = collect_status(app.paths, discovery, names)
    if as_json:
        click.echo(json.dumps(document, indent=2, default=str))
        return
    console = make_console()
    for line in format_status(document, time.time()):
        console.print(line)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/statusview.py src/keepwatch/cli.py tests/test_cli_status.py
git commit -m "Add keepwatch status"
```

---

### Task 9: `keepwatch logs`

**Files:**
- Create: `src/keepwatch/logquery.py`
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_logquery.py`, `tests/test_cli_logs.py`

**Interfaces:**
- Consumes: `LEVELS` (Task 7), `FAILED_STATUSES` (Task 2), `parse_duration` (Plan 1).
- Produces:
  - `log_files(path: Path) -> list[Path]` (oldest rotated file first, current file last); `read_records(path) -> Iterator[dict]` (skips lines that are not JSON objects)
  - `parse_when(text: str, now: datetime | None = None) -> datetime` — a duration ago or an ISO date/time (naive means local); `ValueError` otherwise
  - `is_failure(record) -> bool`; `LogFilter(watch=None, since=None, until=None, min_level=None, events=(), failed=False, poll_id=None)` with `matches(record) -> bool` (`events` match exactly or as a family: `hook` matches `hook.end`; `poll_id` matches by prefix)
  - `select_records(path, log_filter, limit: int) -> list[dict]` (the last `limit` matches; 0 means all)
  - `follow_log(path, emit, *, stop: Callable[[], bool], interval: float = 0.5) -> None` — emits records appended after it starts; survives rotation
  - `keepwatch logs [NAME] [--since WHEN] [--until WHEN] [--level L] [--event E ...] [--failed] [--poll ID] [-n N] [-f] [-v] [--json]`

- [ ] **Step 1: Write the failing tests** — `tests/test_logquery.py`

```python
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from keepwatch.logquery import LogFilter, follow_log, log_files, parse_when, read_records, select_records


def write_lines(path, records):
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_rotated_files_are_read_oldest_first(tmp_path):
    current = tmp_path / "keepwatch.jsonl"
    write_lines(tmp_path / "keepwatch.jsonl.2", [{"n": 1}])
    write_lines(tmp_path / "keepwatch.jsonl.1", [{"n": 2}])
    write_lines(current, [{"n": 3}])
    with open(current, "a") as handle:
        handle.write("not json\n[1]\n")
    assert [p.name for p in log_files(current)] == ["keepwatch.jsonl.2", "keepwatch.jsonl.1", "keepwatch.jsonl"]
    assert [r["n"] for r in read_records(current)] == [1, 2, 3]


def test_parse_when():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert parse_when("1h", now) == now - timedelta(hours=1)
    assert parse_when("2026-09-30T10:00:00+00:00", now) == datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    assert parse_when("2026-09-30", now).tzinfo is not None
    with pytest.raises(ValueError, match="neither a duration"):
        parse_when("soon", now)


def test_filters_and_limit(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [
        {"ts": "2026-09-30T10:00:00+00:00", "level": "INFO", "event": "poll.start", "watch": "a", "poll_id": "aaa1"},
        {"ts": "2026-09-30T10:00:01+00:00", "level": "ERROR", "event": "hook.end", "watch": "a", "poll_id": "aaa1",
         "status": "failed"},
        {"ts": "2026-09-30T11:00:00+00:00", "level": "INFO", "event": "poll.start", "watch": "b", "poll_id": "bbb2"},
        {"ts": "2026-09-30T11:00:01+00:00", "level": "WARNING", "event": "poll.end", "watch": "b", "poll_id": "bbb2",
         "failed": True},
    ])

    def pick(**kwargs):
        return [(r["watch"], r["event"]) for r in select_records(path, LogFilter(**kwargs), 0)]

    assert pick(watch="a") == [("a", "poll.start"), ("a", "hook.end")]
    assert pick(failed=True) == [("a", "hook.end"), ("b", "poll.end")]
    assert pick(events=("poll",)) == [("a", "poll.start"), ("b", "poll.start"), ("b", "poll.end")]
    assert pick(poll_id="bbb") == [("b", "poll.start"), ("b", "poll.end")]
    assert pick(min_level="WARNING") == [("a", "hook.end"), ("b", "poll.end")]
    since = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
    assert pick(since=since) == [("b", "poll.start"), ("b", "poll.end")]
    assert [r["event"] for r in select_records(path, LogFilter(), 1)] == ["poll.end"]


def follow_until(path, count, action):
    seen = []
    thread = threading.Thread(
        target=follow_log,
        args=(path, seen.append),
        kwargs={"stop": lambda: len(seen) >= count, "interval": 0.05},
    )
    thread.start()
    time.sleep(0.3)
    action()
    thread.join(10)
    return seen


def test_follow_emits_new_records_only(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [{"n": 0}])
    seen = follow_until(path, 2, lambda: write_lines(path, [{"n": 1}, {"n": 2}]))
    assert [r["n"] for r in seen] == [1, 2]


def test_follow_survives_rotation(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [{"n": 0}])

    def rotate():
        write_lines(path, [{"n": 1}])
        time.sleep(0.3)
        path.rename(tmp_path / "keepwatch.jsonl.1")
        write_lines(path, [{"n": 2}])

    seen = follow_until(path, 2, rotate)
    assert [r["n"] for r in seen] == [1, 2]
```

`tests/test_cli_logs.py`:

```python
import json

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.logstore import LogWriter

RECORDS = [
    {"ts": "2026-09-30T10:00:00.000+00:00", "level": "INFO", "event": "poll.start", "watch": "a",
     "poll_id": "aaa111", "condition": False, "faked": False, "dry_run": False},
    {"ts": "2026-09-30T10:00:01.000+00:00", "level": "ERROR", "event": "hook.end", "watch": "a",
     "poll_id": "aaa111", "hook": "on_true", "status": "failed", "duration": 1.0, "target": "watch.py:on_true",
     "reason": "boom"},
    {"ts": "2026-09-30T11:00:00.000+00:00", "level": "WARNING", "event": "poll.end", "watch": "b",
     "poll_id": "bbb222", "failed": True, "failures": 1, "dry_run": False},
]


def run(*args):
    return CliRunner().invoke(cli, list(args))


def seed(xdg):
    writer = LogWriter(xdg.log_file, max_bytes=10_000_000, backups=2)
    for record in RECORDS:
        writer.write(record)


def test_logs_json_and_filters(xdg):
    seed(xdg)

    def lines(*args):
        result = run("logs", "--json", *args)
        assert result.exit_code == 0, result.output
        return [json.loads(line) for line in result.output.splitlines()]

    assert len(lines()) == 3
    assert [r["event"] for r in lines("a")] == ["poll.start", "hook.end"]
    assert [r["event"] for r in lines("--failed")] == ["hook.end", "poll.end"]
    assert [r["poll_id"] for r in lines("--poll", "bbb")] == ["bbb222"]
    assert [r["event"] for r in lines("--event", "hook")] == ["hook.end"]
    assert [r["event"] for r in lines("-n", "1")] == ["poll.end"]
    assert [r["event"] for r in lines("--since", "2026-09-30T10:30:00+00:00")] == ["poll.end"]
    assert [r["event"] for r in lines("--level", "error")] == ["hook.end"]


def test_logs_human_output(xdg):
    seed(xdg)
    result = run("logs", "a")
    assert "on_true → failed (1.00s) watch.py:on_true: boom" in result.output


def test_logs_rejects_a_bad_since(xdg):
    result = run("logs", "--since", "soon")
    assert result.exit_code == 2
    assert "neither a duration" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_logquery.py tests/test_cli_logs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.logquery'`

- [ ] **Step 3: Implement** — `src/keepwatch/logquery.py`

```python
"""Reading the JSON-lines log: rotated files, filters, following (keepwatch logs)."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from keepwatch.durations import DurationError, parse_duration
from keepwatch.logstore import LEVELS
from keepwatch.runner import FAILED_STATUSES


def log_files(path: Path) -> list[Path]:
    """Rotated files oldest first (highest number first), then the current file."""
    rotated = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name[len(path.name) + 1:]
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    files = [candidate for _, candidate in sorted(rotated, reverse=True)]
    if path.exists():
        files.append(path)
    return files


def _parse_line(line: bytes | str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    for file in log_files(path):
        try:
            handle = open(file, "rb")
        except FileNotFoundError:
            continue
        with handle:
            for line in handle:
                record = _parse_line(line)
                if record is not None:
                    yield record


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """A duration ago ("1h", "2d") or an ISO date/time ("2026-09-30", "2026-09-30T14:00"); naive means local."""
    now = now or datetime.now().astimezone()
    try:
        return now - timedelta(seconds=parse_duration(text))
    except DurationError:
        pass
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f'{text!r} is neither a duration ("1h", "2d") nor a time ("2026-09-30", "2026-09-30T14:00")'
        ) from None
    return value if value.tzinfo is not None else value.astimezone()


def _stamp(record: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(record.get("ts")))
    except ValueError:
        return None


def is_failure(record: dict[str, Any]) -> bool:
    if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) >= LEVELS["ERROR"]:
        return True
    if record.get("event") == "poll.end" and record.get("failed"):
        return True
    return record.get("event") in ("hook.end", "alert.end") and record.get("status") in FAILED_STATUSES


@dataclass(frozen=True)
class LogFilter:
    watch: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    min_level: str | None = None
    events: tuple[str, ...] = ()
    failed: bool = False
    poll_id: str | None = None

    def matches(self, record: dict[str, Any]) -> bool:
        if self.watch is not None and record.get("watch") != self.watch:
            return False
        if self.poll_id is not None and not str(record.get("poll_id", "")).startswith(self.poll_id):
            return False
        event = str(record.get("event", ""))
        if self.events and not any(event == name or event.startswith(f"{name}.") for name in self.events):
            return False
        if self.min_level is not None:
            if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) < LEVELS[self.min_level]:
                return False
        if self.failed and not is_failure(record):
            return False
        if self.since is not None or self.until is not None:
            stamp = _stamp(record)
            if stamp is None:
                return False
            if self.since is not None and stamp < self.since:
                return False
            if self.until is not None and stamp > self.until:
                return False
        return True


def select_records(path: Path, log_filter: LogFilter, limit: int) -> list[dict[str, Any]]:
    """The last `limit` matching records (all of them when limit is 0), oldest first."""
    matches: deque[dict[str, Any]] = deque(maxlen=limit or None)
    for record in read_records(path):
        if log_filter.matches(record):
            matches.append(record)
    return list(matches)


def follow_log(
    path: Path,
    emit: Callable[[dict[str, Any]], None],
    *,
    stop: Callable[[], bool],
    interval: float = 0.5,
) -> None:
    """Emit records appended to the log after this starts, across rotations, until stop() is true."""

    def open_current() -> tuple[Any, int | None]:
        try:
            handle = open(path, "rb")
        except FileNotFoundError:
            return None, None
        return handle, os.fstat(handle.fileno()).st_ino

    handle, inode = open_current()
    if handle is not None:
        handle.seek(0, os.SEEK_END)
    buffer = b""
    try:
        while not stop():
            if handle is None:
                handle, inode = open_current()
                buffer = b""
            if handle is not None:
                chunk = handle.read()
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        record = _parse_line(line)
                        if record is not None:
                            emit(record)
                    continue
                try:
                    current = os.stat(path).st_ino
                except FileNotFoundError:
                    current = None
                if current != inode:
                    handle.close()
                    handle, inode = open_current()
                    buffer = b""
                    continue
            time.sleep(interval)
    finally:
        if handle is not None:
            handle.close()
```

In `src/keepwatch/cli.py`, add `from keepwatch.logquery import LogFilter, follow_log, parse_when, select_records` and add the command before `def main()`:

```python
@cli.command()
@click.argument("name", required=False)
@click.option("--since", metavar="WHEN", help='Records at or after WHEN: a duration ago ("1h", "2d") or a time ("2026-09-30", "2026-09-30T14:00").')
@click.option("--until", metavar="WHEN", help="Records at or before WHEN (same forms as --since).")
@click.option("--level", type=click.Choice(["debug", "info", "warning", "error", "critical"], case_sensitive=False),
              help="Minimum level.")
@click.option("--event", "events", multiple=True, metavar="EVENT",
              help="Only this event or event family (repeatable): hook.end, or hook for every hook.* event.")
@click.option("--failed", is_flag=True, help="Only failures: ERROR or worse, failed hooks, failed polls and failed alerts.")
@click.option("--poll", "poll_id", metavar="ID", help="Only records of this poll (a prefix of the poll ID is enough).")
@click.option("-n", "--limit", type=int, default=200, show_default=True, help="Show at most the last N matching records; 0 shows all.")
@click.option("-f", "--follow", is_flag=True, help="Then keep printing new matching records until interrupted.")
@click.option("-v", "--verbose", is_flag=True, help="Show captured output and payloads for every record.")
@click.option("--json", "as_json", is_flag=True, help="Print each record as one JSON line (the log file's own format).")
@click.pass_obj
def logs(
    app: App,
    name: str | None,
    since: str | None,
    until: str | None,
    level: str | None,
    events: tuple[str, ...],
    failed: bool,
    poll_id: str | None,
    limit: int,
    follow: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Search the log (including rotated files), optionally restricted to watch NAME.

    Every poll's records share a poll ID: `keepwatch logs --poll <id> -v` shows one poll from start to finish,
    with every command's output. Records are printed oldest first.

    Exit status: 0, or 2 for bad usage.
    """
    try:
        since_time = parse_when(since) if since else None
        until_time = parse_when(until) if until else None
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    log_filter = LogFilter(
        watch=name,
        since=since_time,
        until=until_time,
        min_level=level.upper() if level else None,
        events=events,
        failed=failed,
        poll_id=poll_id,
    )
    printer = ConsolePrinter(make_console(), verbose=verbose)

    def show(record: dict) -> None:
        if as_json:
            click.echo(json.dumps(record, ensure_ascii=False, default=str))
        else:
            printer(record)

    for record in select_records(app.paths.log_file, log_filter, limit):
        show(record)
    if follow:
        try:
            follow_log(app.paths.log_file, lambda r: show(r) if log_filter.matches(r) else None, stop=lambda: False)
        except KeyboardInterrupt:
            pass
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/logquery.py src/keepwatch/cli.py tests/test_logquery.py tests/test_cli_logs.py
git commit -m "Add keepwatch logs: filters, rotated files and following"
```

---

### Task 10: `enable`, `disable` and `rename`

**Files:**
- Modify: `src/keepwatch/config.py`, `src/keepwatch/cli.py`
- Create: `src/keepwatch/control.py`
- Test: `tests/test_cli_control.py`

**Interfaces:**
- Consumes: Task 4's offline functions; `hold_lock`, `LockBusy` (Plan 1).
- Produces:
  - `config.is_valid_watch_name(name: str) -> bool`
  - `control.ControlError(Exception)`; `known_watch(paths, discovery, name) -> bool`; `enable_watch(paths, name) -> bool` (True if it was offline); `disable_watch(paths, name, now: float) -> bool` (False if already disabled by a user); `rename_watch(paths, discovery, old, new) -> tuple[Path, Path]` (new watch dir, new state dir); raises `ControlError` with an actionable message
  - `keepwatch enable NAME`, `keepwatch disable NAME`, `keepwatch rename OLD NEW` — exit 0 on success (and for enable/disable when nothing needed doing), 1 on an unknown name or a refused rename

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_control.py`

```python
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.locks import hold_lock
from keepwatch.offline import read_offline
from keepwatch.paths import ensure_private_dir


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_disable_then_enable(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    result = run("disable", "a")
    assert result.exit_code == 0 and "a disabled" in result.output
    marker = read_offline(xdg, "a")
    assert marker.by_user is True and marker.reason == "disabled by user"
    assert "already disabled" in run("disable", "a").output
    result = run("enable", "a")
    assert result.exit_code == 0 and "a enabled" in result.output
    assert read_offline(xdg, "a") is None
    result = run("enable", "a")
    assert result.exit_code == 0 and "was not offline" in result.output


def test_unknown_names(xdg, make_watch):
    make_watch("psg-export", config='[hooks]\ncheck = ["true"]\n')
    for command in ("enable", "disable"):
        result = run(command, "psg-exprot")
        assert result.exit_code == 1 and "did you mean 'psg-export'" in result.output


def test_rename_moves_the_watch_and_its_state(xdg, make_watch):
    old_dir = make_watch("old", config='[hooks]\ncheck = ["true"]\n')
    ledger = xdg.watch_data_dir("old") / "ledgers" / "sent.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"version": 1, "entries": {}}')
    result = run("rename", "old", "new")
    assert result.exit_code == 0, result.output
    assert not old_dir.exists() and (old_dir.parent / "new" / "config.toml").exists()
    assert (xdg.watch_data_dir("new") / "ledgers" / "sent.json").exists()
    assert not xdg.watch_state_dir("old").exists()


def test_rename_refusals(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    result = run("rename", "a", "b")
    assert result.exit_code == 1 and "already exists" in result.output
    result = run("rename", "a", "bad name")
    assert result.exit_code == 1 and "not a valid watch name" in result.output
    xdg.watch_state_dir("c").mkdir(parents=True)
    result = run("rename", "a", "c")
    assert result.exit_code == 1 and "state left by an earlier watch" in result.output
    ensure_private_dir(xdg.runtime)
    with hold_lock(xdg.watch_lock("a")):
        result = run("rename", "a", "d")
    assert result.exit_code == 1 and "being polled" in result.output


def test_help_shows_every_panel(xdg):
    output = run("--help").output
    for panel in ("Run", "Develop", "Inspect", "Control"):
        assert panel in output
    for command in ("run", "validate", "poll", "status", "logs", "enable", "disable", "rename"):
        assert f" {command} " in output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_control.py -q`
Expected: FAIL with `No such command 'disable'`

- [ ] **Step 3: Implement.** In `src/keepwatch/config.py`, add after `_WATCH_NAME = ...`:

```python
def is_valid_watch_name(name: str) -> bool:
    """Letters, digits, '_', '.' and '-', starting with a letter or digit."""
    return bool(_WATCH_NAME.fullmatch(name))
```

`src/keepwatch/control.py`:

```python
"""keepwatch enable / disable / rename: control through files the service reads."""

from __future__ import annotations

import os
from pathlib import Path

from keepwatch.config import Discovery, WatchNotFound, find_watch, is_valid_watch_name
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import Paths


class ControlError(Exception):
    """A control command was refused; the message says why and what to do."""


def known_watch(paths: Paths, discovery: Discovery, name: str) -> bool:
    return name in discovery.watches or paths.watch_state_dir(name).is_dir()


def require_known(paths: Paths, discovery: Discovery, name: str) -> None:
    if known_watch(paths, discovery, name):
        return
    try:
        find_watch(discovery, name)
    except WatchNotFound as exc:
        raise ControlError(str(exc)) from None


def enable_watch(paths: Paths, name: str) -> bool:
    """Delete offline.json. True if the watch was offline."""
    return clear_offline(paths, name)


def disable_watch(paths: Paths, name: str, now: float) -> bool:
    """Take the watch offline until `keepwatch enable`. False if a user had already disabled it."""
    existing = read_offline(paths, name)
    if existing is not None and existing.by_user:
        return False
    write_offline(paths, name, OfflineMarker(reason="disabled by user", since=iso_time(now), by_user=True))
    return True


def rename_watch(paths: Paths, discovery: Discovery, old: str, new: str) -> tuple[Path, Path]:
    """Rename a watch directory and its state directory together."""
    try:
        old_dir = find_watch(discovery, old)
    except WatchNotFound as exc:
        raise ControlError(str(exc)) from None
    if not is_valid_watch_name(new):
        raise ControlError(f"'{new}' is not a valid watch name: use letters, digits, '_', '.' and '-'")
    if new in discovery.watches:
        raise ControlError(f"a watch named '{new}' already exists: {discovery.watches[new]}")
    new_dir = old_dir.parent / new
    if new_dir.exists():
        raise ControlError(f"{new_dir} already exists")
    old_state = paths.watch_state_dir(old)
    new_state = paths.watch_state_dir(new)
    if new_state.exists():
        raise ControlError(
            f"{new_state} already exists (state left by an earlier watch named '{new}'); "
            "delete it or choose another name"
        )
    try:
        with hold_lock(paths.watch_lock(old), blocking=False):
            os.rename(old_dir, new_dir)
            if old_state.exists():
                new_state.parent.mkdir(parents=True, exist_ok=True)
                os.rename(old_state, new_state)
    except LockBusy:
        raise ControlError(f"'{old}' is being polled right now; try again in a moment") from None
    return new_dir, new_state
```

In `src/keepwatch/cli.py`, add `from keepwatch.control import ControlError, disable_watch, enable_watch, rename_watch, require_known` and these helpers and commands before `def main()`:

```python
def _control_setup(app: App) -> Discovery:
    try:
        ensure_private_dir(app.paths.runtime)
        return discover_watches(app.load_global())
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    except PathError as exc:
        _fail(str(exc))


@cli.command()
@click.argument("name")
@click.pass_obj
def enable(app: App, name: str) -> None:
    """Bring an offline watch back online (deletes its offline.json).

    A running service notices within a second, resets the failure count and polls the watch at once.

    Exit status: 0, or 1 if NAME is unknown.
    """
    discovery = _control_setup(app)
    try:
        require_known(app.paths, discovery, name)
    except ControlError as exc:
        _fail(str(exc))
    if enable_watch(app.paths, name):
        click.echo(f"{name} enabled; a running service polls it within a second")
    else:
        click.echo(f"{name} was not offline; nothing to do")


@cli.command()
@click.argument("name")
@click.pass_obj
def disable(app: App, name: str) -> None:
    """Take a watch offline until `keepwatch enable` (writes offline.json, survives restarts).

    A running service stops polling it within a second; a poll already running finishes first.

    Exit status: 0, or 1 if NAME is unknown.
    """
    discovery = _control_setup(app)
    try:
        require_known(app.paths, discovery, name)
    except ControlError as exc:
        _fail(str(exc))
    if disable_watch(app.paths, name, time.time()):
        click.echo(f"{name} disabled; run `keepwatch enable {name}` to bring it back")
    else:
        click.echo(f"{name} is already disabled")


@cli.command()
@click.argument("old")
@click.argument("new")
@click.pass_obj
def rename(app: App, old: str, new: str) -> None:
    """Rename watch OLD to NEW, moving its persistent state (ledgers, offline marker) with it.

    Renaming the directory by hand would leave the state behind, and the watch would start with empty data
    (a watch that sends files would send them all again). A running service sees OLD disappear and NEW appear
    at its next tick; NEW starts from its initial_condition.

    Exit status: 0 on success, 1 if refused (unknown OLD, NEW taken or invalid, or OLD being polled).
    """
    discovery = _control_setup(app)
    try:
        new_dir, new_state = rename_watch(app.paths, discovery, old, new)
    except ControlError as exc:
        _fail(str(exc))
    click.echo(f"renamed {old} to {new}: {new_dir}" + (f" (state: {new_state})" if new_state.exists() else ""))
```

Also add `Discovery` to the `keepwatch.config` import in `cli.py`.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/config.py src/keepwatch/control.py src/keepwatch/cli.py tests/test_cli_control.py
git commit -m "Add keepwatch enable, disable and rename"
```

---

## Roadmap: Plan 3 (written after Plan 2 lands)

Setup, dependencies and documentation (spec sections 10, 13 Setup/Reference, 15, 16): `python_dependencies` via uv with the `PYTHONPATH` shim and offline-first resolution; `new`, `init` (including `AGENTS.md` and `CLAUDE.md`), `install`/`uninstall` (systemd user unit); the `docs` command with generated and narrative topics, tested example watches and docs-coverage tests; the top-level help line pointing agents at `keepwatch docs agent`; README.
