# keepwatch Plan 4 (Pre-release Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ten defects a whole-branch code review found before the first release: races between the scheduler and the control commands, shutdown leaving hooks behind, zero durations, a service that dies on one I/O error, a lost record when following a rotated log, a non-UTF-8 crash in `ctx.run`, a shim race, and `install` ignoring `--config`.

**Architecture:** Local fixes in `scheduler.py`, `runner.py`, `ctx.py`, `service.py`, `config.py`, `logquery.py`, `systemd.py`, `cli.py` and `output.py`. Every fix starts with a test that reproduces the defect.

**Tech Stack:** as Plan 2.

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md`. Builds on Plans 1–3b (implemented).

## Global Constraints

- All Global Constraints of Plans 1 and 2 apply (explicit `git add`, ruff clean before every commit, tests only in temporary directories).
- Line numbers drift; find edits by the quoted code.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- `keepwatch disable` during a poll must win over the scheduler. Test: Task 1 `test_user_disable_during_a_poll_wins`.
- A hook that ignores SIGTERM must still be killed at shutdown, and no hook may start after shutdown begins. Test: Task 2 `test_close_refuses_new_hooks_and_kill_all_stops_stubborn_ones`.
- One transient I/O error must not stop the service. Test: Task 4 `test_tick_survives_errors`.

---

### Task 1: Scheduler races and offline gating

**Files:**
- Modify: `src/keepwatch/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: `WatchRunner.poll_once` checks the watch directory while holding the watch lock; only a failed poll can take a watch offline; an offline marker written by a user (`by_user`) during a poll is never cleared or overwritten by the scheduler.

- [ ] **Step 1: Write the failing tests.** In `tests/test_scheduler.py` add `import contextlib` to the imports, plus `from keepwatch import scheduler as scheduler_module`, `from keepwatch.state import Outcome, WatchState`. Then append:

```python
def disable_during_poll(runner, xdg):
    real_poll = runner._engine.poll

    def poll(*args, **kwargs):
        write_offline(xdg, runner.name, OfflineMarker(reason="disabled by user", since=iso_time(1.0), by_user=True))
        return real_poll(*args, **kwargs)

    runner._engine.poll = poll


def test_directory_renamed_while_waiting_for_the_lock(xdg, make_watch, runner_for, monkeypatch):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    clock, records = Clock(), []
    runner = runner_for(watch_dir, records, [], clock)
    real_hold_lock = scheduler_module.hold_lock

    @contextlib.contextmanager
    def renaming_lock(path, **kwargs):
        watch_dir.rename(watch_dir.parent / "renamed")
        with real_hold_lock(path, **kwargs):
            yield

    monkeypatch.setattr(scheduler_module, "hold_lock", renaming_lock)
    assert runner.poll_once(clock()) is None
    assert runner.state.failures == 0
    assert not xdg.watch_state_dir("w").exists()


def test_unknown_poll_never_takes_a_watch_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 2\n[hooks]\ncheck = "exit 3"\n[check_exit_codes]\nunknown = [3]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.state = WatchState(False, None, 3)
    report = runner.poll_once(clock())
    assert report.outcome is Outcome.UNKNOWN and not report.failed
    assert read_offline(xdg, "w") is None


def test_user_disable_during_a_poll_wins(xdg, make_watch, runner_for):
    # Going offline: the user's marker is not replaced by an automatic one.
    watch_dir = make_watch("a", config='max_failures = 1\n[hooks]\ncheck = "exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    disable_during_poll(runner, xdg)
    runner.poll_once(clock())
    assert read_offline(xdg, "a").by_user is True
    assert alerts == []
    assert not [r for r in records if r["event"] == "watch.offline" and r["level"] == "CRITICAL"]

    # A successful trial poll does not clear a marker the user wrote during it.
    watch_dir = make_watch("b", config='max_failures = 1\nretry_after = "1m"\n[hooks]\ncheck = "test -f ok || exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert read_offline(xdg, "b").by_user is False
    (watch_dir / "ok").write_text("")
    disable_during_poll(runner, xdg)
    clock.now += 60
    report = runner.poll_once(clock())
    assert report.trial is True and not report.failed
    assert read_offline(xdg, "b").by_user is True
    assert [event for event, _, _ in alerts] == ["offline"]
    assert not [r for r in records if r["event"] == "watch.online"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scheduler.py -q`
Expected: 3 FAIL (a poll runs against the renamed directory; the unknown poll goes offline; the user marker is overwritten/cleared)

- [ ] **Step 3: Implement.** In `src/keepwatch/scheduler.py`, replace `poll_once` with:

```python
    def poll_once(self, now: float) -> PollReport | None:
        """Poll if due (a trial poll when offline with retry_after). Returns None when not due."""
        self._apply_pending()
        self._refresh_offline(now)
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
        config = self._config
        with hold_lock(self._paths.watch_lock(self.name)):
            # Checked under the lock: `keepwatch rename` moves the directory while holding it.
            if not config.watch_dir.is_dir():
                return None
            trial = self.offline is not None
            if trial:
                self.last_trial = now
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
        current = read_offline(self._paths, self.name)
        if current is not None and current.by_user:
            # `keepwatch disable` ran during this poll: the user's marker wins, and the next
            # _refresh_offline picks it up.
            self.next_due = finished + next_delay(config.interval, self.state.failures)
            return report
        if trial:
            if not report.failed and report.outcome in ANSWERS:
                clear_offline(self._paths, self.name)
                self.offline = None
                self.last_trial = None
                self.next_due = finished + config.interval
                self._went_online("trial poll succeeded")
            return report
        if report.failed and should_go_offline(self.state.failures, config.max_failures):
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
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/scheduler.py tests/test_scheduler.py
git commit -m "Scheduler: check the watch dir under the lock, respect user markers, go offline only on failure"
```

---

### Task 2: Clean shutdown, shim race, non-UTF-8 output in ctx.run

**Files:**
- Modify: `src/keepwatch/runner.py`, `src/keepwatch/ctx.py`, `src/keepwatch/service.py`
- Test: `tests/test_runner_command.py`, `tests/test_runner_python.py`, `tests/test_ctx.py`

**Interfaces:**
- Produces:
  - `Runner.close()` — refuse new hook calls (they return status `error`/`failed` with reason `"keepwatch is shutting down"`) and SIGTERM running ones; `Runner.kill_all()` — SIGKILL every hook group still running. A hook that starts during `close()` is signalled as soon as it is tracked.
  - `Service.stop()` — `close()`, wait up to `KILL_GRACE` for polls, `kill_all()`, wait up to `DRAIN_GRACE + 3` more. `SHUTDOWN_GRACE` is removed.
  - `make_shim` replaces the link atomically (no race between threads; a dangling link is repaired).
  - `Ctx.run` decodes output with `errors="replace"`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_runner_command.py`:

```python
def test_close_refuses_new_hooks_and_kill_all_stops_stubborn_ones(make_watch, call_for):
    watch_dir = make_watch("cmd", config="[hooks]\ncheck = 'trap \"\" TERM; sleep 30'\n")
    runner = Runner(kill_grace=30)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(0.5)
    runner.close()
    thread.join(1.0)
    assert thread.is_alive()
    runner.kill_all()
    thread.join(10)
    assert results[0].signal == 9
    refused = runner.run(call_for(watch_dir))
    assert (refused.status, refused.reason) == ("error", "keepwatch is shutting down")
```

Append to `tests/test_runner_python.py` (add `import threading` to its imports):

```python
def test_make_shim_is_safe_across_threads_and_repairs_dangling_links(tmp_path):
    import keepwatch

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "keepwatch").symlink_to(tmp_path / "gone")
    errors = []

    def build():
        try:
            make_shim(lib)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (lib / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent
```

Append to `tests/test_ctx.py`:

```python
def test_run_decodes_non_utf8_output(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted)
    assert ctx.run(["printf", "\\377ok"]).stdout == "�ok"
    assert emitted[0]["stdout"] == "�ok"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_runner_command.py tests/test_runner_python.py tests/test_ctx.py -q`
Expected: 3 FAIL (`AttributeError: 'Runner' object has no attribute 'close'`; `FileExistsError` from the threads or the dangling link left in place; `UnicodeDecodeError`)

- [ ] **Step 3: Implement.**

In `src/keepwatch/ctx.py`, in `Ctx.run`, add `errors="replace",` right after `text=True,` in the `subprocess.run(...)` call.

In `src/keepwatch/runner.py`, replace `make_shim` with:

```python
def make_shim(directory: Path) -> Path:
    """A PYTHONPATH directory exposing only the running keepwatch package (for uv environments)."""
    import keepwatch

    target = Path(keepwatch.__file__).resolve().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = directory / "keepwatch"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return directory
    temp = directory / f".keepwatch-{os.getpid()}-{threading.get_ident()}"
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, link)  # atomic: concurrent callers never see a missing or half-made link
    return directory
```

In `Runner.__init__`, add `self._closing = False` after `self._active_lock = threading.Lock()`. Replace `terminate_all`, `_track` and `_untrack` with:

```python
    def terminate_all(self) -> None:
        """Send SIGTERM to every hook process group that is running now."""
        with self._active_lock:
            groups = list(self._active)
        for pgid in groups:
            _signal_group(pgid, signal.SIGTERM)

    def close(self) -> None:
        """Shutdown, step 1: refuse new hook calls and SIGTERM the running ones."""
        with self._active_lock:
            self._closing = True
        self.terminate_all()

    def kill_all(self) -> None:
        """Shutdown, step 2: SIGKILL every hook process group still running."""
        with self._active_lock:
            groups = list(self._active)
        for pgid in groups:
            _signal_group(pgid, signal.SIGKILL)

    def _track(self, pid: int) -> None:
        with self._active_lock:
            self._active.add(pid)
            closing = self._closing
        if closing:
            # Started while close() ran: stop it right away.
            _signal_group(pid, signal.SIGTERM)

    def _untrack(self, pid: int) -> None:
        with self._active_lock:
            self._active.discard(pid)
```

In `Runner.run`, insert directly after `is_command = call.mode == "call" and call.hook in call.watch.hooks`:

```python
        if self._closing:
            return self._error(call, "command" if is_command else "python", call.hook, started,
                               "keepwatch is shutting down")
```

In `_start_worker`, insert at the very start of the method body (before `req_r, req_w = os.pipe()`):

```python
        if self._closing:
            return "keepwatch is shutting down"
```

In `src/keepwatch/service.py`: delete the line `SHUTDOWN_GRACE = KILL_GRACE + DRAIN_GRACE + 3.0` and replace `Service.stop` with:

```python
    def stop(self) -> None:
        """Stop every runner and its hooks: SIGTERM, wait, SIGKILL, wait; then write the final status."""
        runners = [*self.runners.values(), *self._retired]
        for runner in runners:
            runner.stop()
        self._runner.close()
        deadline = time.monotonic() + KILL_GRACE
        for runner in runners:
            runner.join(max(deadline - time.monotonic(), 0.0))
        self._runner.kill_all()
        deadline = time.monotonic() + DRAIN_GRACE + 3.0
        for runner in runners:
            runner.join(max(deadline - time.monotonic(), 0.0))
        self._sink(make_record("service.stop"))
        self.write_status(self._clock(), running=False)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/runner.py src/keepwatch/ctx.py src/keepwatch/service.py tests/test_runner_command.py tests/test_runner_python.py tests/test_ctx.py
git commit -m "Shut hooks down for sure, make the shim race-free, decode any ctx.run output"
```

---

### Task 3: Timeouts and retry_after are at least 1s

**Files:**
- Modify: `src/keepwatch/config.py`
- Test: `tests/test_config_watch.py`

- [ ] **Step 1: Write the failing test.** Append to `tests/test_config_watch.py`:

```python
@pytest.mark.parametrize("key", ["check_timeout", "action_timeout", "retry_after"])
def test_zero_durations_are_rejected(tmp_path, key):
    (problem,) = problems_of(tmp_path / "w", f'{key} = "0s"\n')
    assert problem.message == f"'{key}' must be at least 1s, got '0s'"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_config_watch.py -q` → 3 FAIL (`ConfigError` not raised)

- [ ] **Step 3: Implement.** In `WATCH_KEYS`, change the kind of `check_timeout`, `action_timeout` and `retry_after` from `"duration"` to `"interval"` (the kind that enforces a 1s minimum). Leave everything else in those `Key(...)` lines unchanged.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS (the generated `config` docs topic now shows "duration ≥ 1s" for these keys)

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/config.py tests/test_config_watch.py
git commit -m "Require at least 1s for timeouts and retry_after"
```

---

### Task 4: A master tick never stops the service

**Files:**
- Modify: `src/keepwatch/service.py`, `src/keepwatch/output.py`
- Test: `tests/test_service.py`, `tests/test_output.py`

**Interfaces:**
- Produces: `Service.tick()` never raises; a failure is logged once as `service.error` (`error`, `traceback`) until a tick succeeds again. `file_signature` returns None for any `OSError`. `format_record` prints `service.error` as `service error: <error>`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_service.py` (add `from keepwatch import service as service_module` to its imports):

```python
def test_tick_survives_errors(xdg, make_watch, monkeypatch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    real_write = service_module.write_json_atomic

    def full_disk(path, document):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service_module, "write_json_atomic", full_disk)
    service.tick()
    service.tick()
    errors = events(records, "service.error")
    assert len(errors) == 1
    assert errors[0]["error"] == "OSError: [Errno 28] No space left on device"
    monkeypatch.setattr(service_module, "write_json_atomic", real_write)
    service.tick()
    monkeypatch.setattr(service_module, "write_json_atomic", full_disk)
    service.tick()
    assert len(events(records, "service.error")) == 2
```

Append to `tests/test_output.py`:

```python
def test_service_error_is_readable():
    record = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "ERROR", "event": "service.error",
              "error": "OSError: disk full", "traceback": "Traceback"}
    assert format_record(record) == "10:11:12 service error: OSError: disk full"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_service.py tests/test_output.py -q` → 2 FAIL (`OSError` escapes `tick`; generic formatting)

- [ ] **Step 3: Implement.** In `src/keepwatch/service.py`: add `import traceback` to the imports; in `file_signature`, change `except FileNotFoundError:` to `except OSError:` and the docstring to `"""(mtime_ns, size), or None if the file does not exist or cannot be read."""`; add `self._tick_error: str | None = None` at the end of `Service.__init__`; rename the existing `def tick(self) -> None:` to `def _tick(self) -> None:` and add above it:

```python
    def tick(self) -> None:
        """One master tick. Never raises: a failure is logged once, and the next tick tries again."""
        try:
            self._tick()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self._tick_error:
                self._sink(make_record("service.error", level="ERROR", error=error, traceback=traceback.format_exc()))
            self._tick_error = error
        else:
            self._tick_error = None
```

In `src/keepwatch/output.py`, add after `_service_stop`:

```python
def _service_error(record: dict[str, Any], verbose: bool) -> str:
    line = f"service error: {record.get('error')}"
    if verbose and record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line
```

and add `"service.error": _service_error,` to `_FORMATTERS`.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/service.py src/keepwatch/output.py tests/test_service.py tests/test_output.py
git commit -m "Keep the service running through master-tick errors"
```

---

### Task 5: Following the log never drops records at rotation

**Files:**
- Modify: `src/keepwatch/logquery.py`
- Test: `tests/test_logquery.py`

- [ ] **Step 1: Write the failing test.** In `tests/test_logquery.py` add `from pathlib import Path` and `from keepwatch import logquery` to the imports, and in the existing `follow_until` helper add `daemon=True,` to the `threading.Thread(...)` call (before the fix this test's follower never reaches its stop condition, and a non-daemon thread would keep pytest from exiting). Then append:

```python
def test_follow_reads_the_rest_of_a_rotated_file(tmp_path, monkeypatch):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [{"n": 0}])
    real_stat = logquery.os.stat
    rotated = []

    def stat(target, *args, **kwargs):
        if not rotated and Path(target) == path:
            rotated.append(True)
            write_lines(path, [{"n": 1}])                 # appended after the last read...
            path.rename(tmp_path / "keepwatch.jsonl.1")   # ...and rotated before the next one
            write_lines(path, [{"n": 2}])
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(logquery.os, "stat", stat)
    seen = follow_until(path, 2, lambda: None)
    assert [r["n"] for r in seen] == [1, 2]
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_logquery.py -q` → FAIL after about 10 seconds (`[2] != [1, 2]`)

- [ ] **Step 3: Implement.** In `follow_log`, replace

```python
                if current != inode:
                    handle.close()
                    handle, inode = open_current()
                    buffer = b""
                    continue
```

with

```python
                if current != inode:
                    # Rotated: finish the old file first, including a last line without a newline.
                    rest = buffer + handle.read()
                    handle.close()
                    for line in rest.split(b"\n"):
                        record = _parse_line(line) if line.strip() else None
                        if record is not None:
                            emit(record)
                    handle, inode = open_current()
                    buffer = b""
                    continue
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/logquery.py tests/test_logquery.py
git commit -m "logs -f: read the rest of a rotated file before switching"
```

---

### Task 6: `install` keeps the running keepwatch and its config

**Files:**
- Modify: `src/keepwatch/systemd.py`, `src/keepwatch/cli.py`
- Test: `tests/test_cli_install.py`

**Interfaces:**
- Produces: `find_executable()` prefers the `keepwatch` script that is running (`sys.argv[0]`, absolute, symlinks kept), then the one on PATH; `unit_text(executable, path_env, config_path: Path | None = None)` adds `--config "<path>"` before `run` when given; `install` passes the config path when it differs from the default.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_cli_install.py` (add `import sys` and `from keepwatch import systemd` to its imports):

```python
def test_install_keeps_a_custom_config(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch)
    custom = tmp_path / "custom.toml"
    custom.write_text("")
    assert run("--config", str(custom), "install").exit_code == 0
    assert f'--config "{custom.resolve()}" run' in unit_file(xdg).read_text()
    assert run("install").exit_code == 0
    assert "--config" not in unit_file(xdg).read_text()


def test_find_executable_prefers_the_running_script(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "keepwatch"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(script), "install"])
    assert systemd.find_executable() == str(script)
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_cli_install.py -q` → 2 FAIL

- [ ] **Step 3: Implement.** In `src/keepwatch/systemd.py`, replace `find_executable` and `unit_text` with:

```python
def find_executable() -> str:
    """Absolute path of the keepwatch command that is running now (else the one on PATH)."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "keepwatch" and argv0.is_file():
        return os.path.abspath(argv0)
    found = shutil.which("keepwatch")
    if found:
        return os.path.abspath(found)
    return os.path.abspath(sys.argv[0])


def unit_text(executable: str, path_env: str, config_path: Path | None = None) -> str:
    config = f' --config "{config_path}"' if config_path is not None else ""
    return (
        "[Unit]\n"
        "Description=keepwatch: poll conditions and run actions\n"
        "Documentation=https://github.com/smprather/keepwatch\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'ExecStart="{executable}"{config} run\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f'Environment="PATH={path_env}"\n'
        'Environment="PYTHONUNBUFFERED=1"\n'
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
```

In `src/keepwatch/cli.py`, in `install`, replace

```python
    text = systemd.unit_text(systemd.find_executable(), os.environ.get("PATH", ""))
```

with

```python
    custom_config = app.config_path.resolve() if app.config_path != app.paths.config_file else None
    text = systemd.unit_text(systemd.find_executable(), os.environ.get("PATH", ""), custom_config)
```

and add this sentence to the end of the `install` docstring's first paragraph: `A --config given to install is passed on to the service.`

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/systemd.py src/keepwatch/cli.py tests/test_cli_install.py
git commit -m "install: run the same keepwatch with the same config"
```
