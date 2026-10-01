# keepwatch Plan 1b (Code Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the defects a code review found in Plan 1 before Plan 2 builds on it.

**Architecture:** Small, local changes to existing modules. Every fix starts with a test that reproduces the defect.

**Tech Stack:** Python ≥ 3.12, uv, pytest (as Plan 1).

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md`; builds on `docs/superpowers/plans/2026-09-29-keepwatch-plan-1-core.md` (already implemented).

## Global Constraints

- All of Plan 1's Global Constraints still apply (stdlib-only plugin modules, 4-space indentation and never tabs, error text format `<file>:<line>: <problem>; see: keepwatch docs <topic>`).
- Line numbers below are from the current code and may drift; locate edits by the quoted code.
- Run the whole suite (`uv run pytest -q`) before every commit; it must stay green.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A TOML date in `[settings]` must not crash `poll` or `validate` (reproduced). Test: Task 2 `test_dates_in_settings_do_not_crash`.
- A hook written as an import, assignment, `async def` or nested definition must be reported by `validate`, not silently skipped. Test: Task 6 `test_hooks_keepwatch_cannot_see_are_reported`.
- No hook-level failure (disk full, unwritable run dir, unexpected exception) may escape `Runner.run` and kill a poll. Tests: Task 3.
- A plugin that logs gigabytes must not exhaust the service's memory. Test: Task 3 `test_huge_log_volume_is_capped`.
- `validate NAME` must not say ok when NAME is a skipped duplicate. Test: Task 6 `test_named_duplicate_is_reported`.

---

### Task 1: Reject nan and infinity as durations

**Files:**
- Modify: `src/keepwatch/durations.py`
- Test: `tests/test_durations.py`, `tests/test_config_watch.py`

- [ ] **Step 1: Write the failing tests.** In `tests/test_durations.py`, add two cases to the `test_parse_duration_rejects` parametrize list:

```python
        (float("nan"), "must be a finite number"),
        (float("inf"), "must be a finite number"),
```

Append to `tests/test_config_watch.py`:

```python
def test_infinite_timeout_is_rejected(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "check_timeout = inf\n")
    assert problem.line == 1
    assert "must be a finite number" in problem.message
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_durations.py tests/test_config_watch.py -q`
Expected: 3 FAIL (`DID NOT RAISE` / `ConfigError` not raised)

- [ ] **Step 3: Implement.** In `src/keepwatch/durations.py` add `import math` after `from __future__ import annotations` (with the other imports), and in `parse_duration` replace

```python
    if isinstance(value, (int, float)):
        if value < 0:
```

with

```python
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise DurationError(f"duration must be a finite number, got {value!r}")
        if value < 0:
```

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "Reject nan and infinite durations"`

---

### Task 2: Make `[settings]` JSON-safe when config is loaded

**Files:**
- Modify: `src/keepwatch/config.py`
- Test: `tests/test_config_watch.py`, `tests/test_cli_validate.py`

**Interfaces:**
- Produces: `WatchConfig.settings` contains only JSON types. TOML dates, datetimes and times become ISO 8601 strings; `nan`/`inf` anywhere in `[settings]` is a config problem reported on the `[settings]` header line.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_config_watch.py`:

```python
def test_settings_dates_become_iso_strings(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", '''
        [settings]
        since = 2024-01-01
        at = 2024-01-01T10:00:00Z
        clock = 07:30:00
        nested = { when = 2024-02-03 }
    '''))
    assert cfg.settings == {
        "since": "2024-01-01",
        "at": "2024-01-01T10:00:00+00:00",
        "clock": "07:30:00",
        "nested": {"when": "2024-02-03"},
    }


def test_settings_reject_nan(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "[settings]\nx = nan\n")
    assert problem.line == 1
    assert "nan or inf" in problem.message
```

Append to `tests/test_cli_validate.py`:

```python
def test_dates_in_settings_do_not_crash(xdg, make_watch):
    make_watch(
        "dated",
        config="[settings]\nsince = 2024-01-01\n",
        files={"watch.py": "def check(ctx):\n    return ctx.settings['since'] == '2024-01-01'\n"},
    )
    assert run("validate", "dated").exit_code == 0
    result = run("poll", "dated")
    assert result.exit_code == 0, result.output
    assert "check → true" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_config_watch.py tests/test_cli_validate.py -q`
Expected: FAIL — the date test compares `datetime.date` objects to strings; the CLI test fails with `TypeError: Object of type date is not JSON serializable`.

- [ ] **Step 3: Implement.** In `src/keepwatch/config.py` add `import datetime` and `import json` to the stdlib imports. Add these helpers just above `def load_watch_config`:

```python
def _json_default(value: Any) -> Any:
    # datetime.datetime is a subclass of datetime.date, so this covers all three TOML date/time types.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} cannot be passed to hooks")


def _settings_table(collector: _Collector, value: dict[str, Any]) -> dict[str, Any]:
    """[settings] as JSON-safe data: TOML dates and times become ISO 8601 strings."""
    try:
        return json.loads(json.dumps(value, default=_json_default, allow_nan=False))
    except (TypeError, ValueError):
        collector.add(
            "[settings] must not contain nan or inf (settings are passed to hooks as JSON)",
            table="settings",
        )
        return {}
```

In `load_watch_config`, replace

```python
            if isinstance(value, dict):
                settings = value
```

with

```python
            if isinstance(value, dict):
                settings = _settings_table(collector, value)
```

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "Convert TOML dates in [settings] to ISO strings and reject nan"`

---

### Task 3: `Runner.run` never raises; cap worker message volume

**Files:**
- Modify: `src/keepwatch/runner.py`
- Test: `tests/test_runner_python.py`, `tests/test_runner_command.py`

**Interfaces:**
- Produces: `Runner.run` always returns a `HookResult`. Unexpected exceptions become `status` `"error"` (check/describe) or `"failed"` (action) with `reason` `"keepwatch internal error: <Type>: <message>"` and the traceback in `exception`. OSError while preparing a command hook's files gives `reason` starting `"cannot prepare the hook's files in "`. Module constant `RESULT_LIMIT = 16 * 1024 * 1024` caps bytes kept from the worker's message pipe (first and last half kept).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_runner_python.py`:

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
    assert any("only the first and last 50000 bytes were kept" in note for note in notes)
    assert sum(1 for m in result.messages if m.get("type") == "log") < 1000


def test_unexpected_runner_errors_become_failed_results(make_watch, call_for, monkeypatch):
    watch_dir = make_watch("w", config='[hooks]\non_true = "true"\n')

    def boom(self, call, command):
        raise RuntimeError("boom")

    monkeypatch.setattr(Runner, "_run_command", boom)
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason == "keepwatch internal error: RuntimeError: boom"
    assert "RuntimeError" in result.exception["traceback"]
```

Append to `tests/test_runner_command.py`:

```python
def test_unwritable_run_dir_is_a_failed_hook(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = "true"\n')
    call = call_for(watch_dir, hook="on_true")
    call.run_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    call.run_dir.parent.write_text("not a directory")
    result = Runner().run(call)
    assert result.status == "failed"
    assert result.reason.startswith("cannot prepare the hook's files in")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_runner_python.py tests/test_runner_command.py -q`
Expected: 3 FAIL (`AttributeError: ... has no attribute 'RESULT_LIMIT'`; `RuntimeError: boom` escapes; `NotADirectoryError` escapes from `call.run_dir.mkdir`)

- [ ] **Step 3: Implement** in `src/keepwatch/runner.py`.

Add `import traceback` to the stdlib imports, and below `DRAIN_GRACE = 2.0` add:

```python
RESULT_LIMIT = 16 * 1024 * 1024
```

Replace `Runner.run` with:

```python
    def run(self, call: HookCall) -> HookResult:
        """Run one hook call. Never raises: any unexpected failure becomes a failed result."""
        started = time.monotonic()
        is_command = call.mode == "call" and call.hook in call.watch.hooks
        try:
            if is_command:
                return self._run_command(call, call.watch.hooks[call.hook])
            return self._run_python(call)
        except Exception as exc:
            result = self._error(
                call,
                "command" if is_command else "python",
                call.hook,
                started,
                f"keepwatch internal error: {type(exc).__name__}: {exc}",
            )
            result.exception = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            return result
```

In `_run_python`, replace

```python
        results = _Reader(os.fdopen(res_r, "rb"), None)
```

with

```python
        results = _Reader(os.fdopen(res_r, "rb"), RESULT_LIMIT)
```

and directly after the existing `if bad:` block that appends the "ignored … malformed message(s)" warning, add:

```python
        if results.total > RESULT_LIMIT:
            base["messages"].append(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": (
                        f"the worker sent {results.total} bytes of messages; "
                        f"only the first and last {RESULT_LIMIT // 2} bytes were kept"
                    ),
                    "fields": {},
                }
            )
```

In `_run_command`, wrap the file preparation in its own `try`. Replace the block that starts at `call.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)` and ends after `env["KEEPWATCH_PAYLOAD_FILE"] = str(payload_file)` with:

```python
            payload_out: Path | None = None
            try:
                call.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                call.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                stem = f"{call.poll_id}-{call.hook}"
                settings_file = call.run_dir / f"{stem}-settings.json"
                settings_file.write_text(json.dumps(dict(call.watch.settings)), encoding="utf-8")
                temp_files.append(settings_file)
                env["KEEPWATCH_SETTINGS_FILE"] = str(settings_file)
                if call.hook == CHECK:
                    payload_out = call.run_dir / f"{stem}-payload-out.json"
                    payload_out.unlink(missing_ok=True)
                    temp_files.append(payload_out)
                    env["KEEPWATCH_PAYLOAD_OUT"] = str(payload_out)
                elif call.payload is not None:
                    payload_file = call.run_dir / f"{stem}-payload.json"
                    payload_file.write_text(json.dumps(call.payload), encoding="utf-8")
                    temp_files.append(payload_file)
                    env["KEEPWATCH_PAYLOAD_FILE"] = str(payload_file)
            except OSError as exc:
                return self._error(
                    call,
                    "command",
                    target,
                    started,
                    f"cannot prepare the hook's files in {call.run_dir}: {exc.strerror or exc}",
                )
```

(The `finally` that unlinks `temp_files` stays as it is; `missing_ok=True` makes it safe for files never created.)

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "Never let a hook failure escape the runner; cap worker message volume"`

---

### Task 4: Treat worker command messages as untrusted

**Files:**
- Modify: `src/keepwatch/pollengine.py`
- Test: `tests/test_pollengine.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_pollengine.py`:

```python
def test_malformed_command_messages_cannot_crash_a_poll(make_watch, engine_for):
    watch_dir = make_watch("evil", files={"watch.py": '''
        def check(ctx):
            ctx._emit({"type": "command", "hook": "x", "level": "y", "watch": "z", "argv": ["a"], "exit_code": 0})
            return False
    '''})
    records = []
    report = engine_for(records).poll(load_watch_config(watch_dir), WatchState(False))
    assert report.outcome is Outcome.FALSE
    command = next(r for r in records if r["event"] == "command")
    assert (command["watch"], command["hook"], command["argv"], command["level"]) == ("evil", "check", ["a"], "INFO")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pollengine.py -q`
Expected: FAIL with `TypeError: ... got multiple values for keyword argument`

- [ ] **Step 3: Implement.** In `src/keepwatch/pollengine.py`, below `_PLUGIN_LEVELS` add:

```python
_COMMAND_FIELDS = (
    "argv",
    "shell",
    "exit_code",
    "timed_out",
    "duration",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
)
```

In `PollEngine._run`, replace

```python
                fields = {key: value for key, value in message.items() if key != "type"}
```

with

```python
                fields = {key: message.get(key) for key in _COMMAND_FIELDS}
```

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "Accept only known fields from worker command messages"`

---

### Task 5: Name the file when a ledger is corrupt

**Files:**
- Modify: `src/keepwatch/ctx.py`, `src/keepwatch/__init__.py`
- Test: `tests/test_ctx.py`

**Interfaces:**
- Produces: `keepwatch.LedgerCorrupt(Exception)`, raised by `Ledger(...)` when its file exists but is not a valid ledger; the message contains the file path and says to fix or delete it.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_ctx.py` (add `import re` at the top and `LedgerCorrupt` to the `from keepwatch import ...` line):

```python
@pytest.mark.parametrize("content", ["{not json", '{"entries": {"k": "soon"}}', "[]"])
def test_corrupt_ledger_names_the_file(tmp_path, content):
    path = tmp_path / "sent.json"
    path.write_text(content)
    with pytest.raises(LedgerCorrupt, match=re.escape(str(path))):
        Ledger(path)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ctx.py -q`
Expected: FAIL with `ImportError: cannot import name 'LedgerCorrupt'`

- [ ] **Step 3: Implement.** In `src/keepwatch/ctx.py`, add below `class LedgerReadOnly`:

```python
class LedgerCorrupt(Exception):
    """A ledger file exists but cannot be read. The message names the file to fix or delete."""
```

Replace `Ledger._load` with:

```python
    def _load(self) -> dict[str, float]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            entries = json.loads(text)["entries"]
            return {str(key): float(stamp) for key, stamp in entries.items()}
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LedgerCorrupt(
                f"ledger file {self.path} is unreadable ({type(exc).__name__}: {exc}); "
                "fix it, or delete it to start with an empty ledger"
            ) from exc
```

In `src/keepwatch/__init__.py`, add `LedgerCorrupt` to the `from keepwatch.ctx import ...` line and to `__all__` (keep both sorted).

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "Report corrupt ledger files by path"`

---

### Task 6: `validate` reports invisible hooks and named duplicates

**Files:**
- Modify: `src/keepwatch/validation.py`, `src/keepwatch/cli.py`
- Test: `tests/test_cli_validate.py`

**Interfaces:**
- Produces: `validate` adds a problem when the worker finds hook callables that the top-level-`def` scan does not (they would never run). `validate_watches` with names returns the discovery problems whose path's final component is one of the names; `validate` exits 1 whenever any general problem is returned.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_cli_validate.py`:

```python
def test_hooks_keepwatch_cannot_see_are_reported(xdg, make_watch):
    make_watch("hidden", files={
        "helpers.py": "def on_rise(ctx):\n    pass\n",
        "watch.py": "from helpers import on_rise\n\ndef check(ctx):\n    return True\n\nasync def on_true(ctx):\n    pass\n",
    })
    result = run("validate", "--json")
    assert result.exit_code == 1
    (watch,) = json.loads(result.output)["watches"]
    assert "on_rise, on_true" in watch["problems"][0]
    assert "top-level" in watch["problems"][0]


def test_named_duplicate_is_reported(xdg, tmp_path, make_watch):
    other = tmp_path / "other"
    make_watch("backup", config='[hooks]\ncheck = ["true"]\n')
    make_watch("backup", config='[hooks]\ncheck = ["true"]\n', base=other)
    config = tmp_path / "custom.toml"
    config.write_text(f'watch_dirs = ["{xdg.default_watches_dir}", "{other}"]\n')
    result = run("--config", str(config), "validate", "backup")
    assert result.exit_code == 1
    assert "duplicate watch name 'backup'" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_validate.py -q`
Expected: 2 FAIL (both exit 0)

- [ ] **Step 3: Implement.** In `src/keepwatch/validation.py`, change the hooks import to

```python
from keepwatch.hooks import CHECK, NO_CHECK, WATCH_PY, discover_python_hooks, resolve_hooks
```

In `check_watch`, replace

```python
        if result.status != "ok":
            report.problems.append(f"{source}: {result.reason}; see: keepwatch docs python")
```

with

```python
        if result.status != "ok":
            report.problems.append(f"{source}: {result.reason}; see: keepwatch docs python")
        else:
            hidden = sorted(set(result.hooks or []) - discover_python_hooks(watch_dir))
            if hidden:
                report.problems.append(
                    f"{source}: {', '.join(hidden)} would never run: keepwatch only runs hooks written as "
                    "top-level 'def <hook>(ctx):' functions in watch.py, not imported, assigned, async or "
                    "nested ones; see: keepwatch docs python"
                )
```

(`discover_python_hooks` cannot raise here: this branch only runs when `resolve_hooks` reported no problem.)

In `validate_watches`, replace

```python
    general = [str(problem) for problem in discovery.problems]
```

with

```python
    general = [
        str(problem)
        for problem in discovery.problems
        if not names or problem.path.name in names
    ]
```

In `src/keepwatch/cli.py` (`validate` command), replace

```python
    ok = (not general or bool(names)) and all(check.ok for check in checks)
```

with

```python
    ok = not general and all(check.ok for check in checks)
```

- [ ] **Step 4: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit** — `git commit -am "validate: report hooks keepwatch cannot see and duplicates of named watches"`

---

## Deferred to Plan 2

- Streaming plugin log records live during a hook (today they appear when the hook ends).
- Ledger lookups with `expire` re-filter the whole ledger per lookup (O(n)); prune once on load instead.
- Deduplicate the failure-status constants in `pollengine.py` and `output.py`.
