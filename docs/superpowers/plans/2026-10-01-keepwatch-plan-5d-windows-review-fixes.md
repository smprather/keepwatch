# keepwatch Plan 5d (Windows Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ten findings of the code review of the Windows port before releasing 2026.10.2.

**Architecture:** Small changes in `platform.py` (public `absolute_path`, `NO_WINDOW`, `shell_argv` with UTF-8 output for Windows PowerShell, checked resume), `ctx.py`/`worker.py`/`runner.py` (ctx.run behaves like command hooks), `alerts.py` (no console window, relative programs), `logstore.py` (rotation that never half-runs), `cli.py` (log start failures without a console, install/start split, duplicate block removed), `service.py` (stop requests older than the lock are stale), `winsched.py`, templates and examples.

**Tech Stack:** as Plan 5a.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-windows-design.md`. Builds on Plans 5a–5c (implemented).

## Global Constraints

- All Global Constraints of Plans 5a and 5c apply (branch `keepwatch-windows`, explicit `git add`, Linux suite green before every commit — checked without a pipe —, do not push, Windows-only code copied exactly).
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A failed rotation must leave every backup as it was. Test: Task 4 `test_failed_rotation_leaves_backups_alone`.
- A `keepwatch stop` written after the service took its lock must stop it. Test: Task 5 `test_stale_stop_requests_are_ignored`.
- ctx.run must treat strings and lists exactly like command hooks. Test: Task 2.

---

### Task 1: Platform helpers

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/paths.py`, `src/keepwatch/config.py`, `tests/conftest.py`
- Test: `tests/test_platform.py`, `tests/test_config_watch.py`, `tests/test_portable_hooks.py`

**Interfaces:**
- Produces: `platform.absolute_path(value: str | None) -> Path | None` (replaces the private `_absolute` in both `platform.py` and `paths.py`; `paths.py` imports `APP` and `absolute_path` from `platform`); `platform.NO_WINDOW: int` (`subprocess.CREATE_NO_WINDOW` on Windows, `0` elsewhere); `platform.shell_argv(text: str, shell: Sequence[str] | None = None) -> list[str]` (the given shell, else `/bin/sh -c`, else on Windows Windows PowerShell with a prelude that switches its output to UTF-8); `Command.to_argv` uses `shell_argv`. `start_process` raises `OSError` if `NtResumeProcess` fails. `make_watch` writes files as UTF-8.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_platform.py`:

```python
def test_shell_argv(tmp_path):
    assert platform.shell_argv("exit 3", ["pwsh", "-Command"]) == ["pwsh", "-Command", "exit 3"]
    argv = platform.shell_argv("exit 3")
    if platform.IS_WINDOWS:
        assert argv[0] == "powershell.exe" and argv[-1].endswith("exit 3") and "UTF8Encoding" in argv[-1]
    else:
        assert argv == ["/bin/sh", "-c", "exit 3"]
    assert platform.absolute_path("relative") is None
    assert platform.absolute_path(str(tmp_path)) == tmp_path
```

Append to `tests/test_portable_hooks.py`:

```python
@pytest.mark.windows_only
def test_string_hook_output_is_utf8_on_windows(make_watch, call_for):
    watch_dir = make_watch("s", config="[hooks]\ncheck = \"Write-Output 'café'\"\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout.strip() == "café"
```

In `tests/test_config_watch.py`, replace `assert cfg.hooks["check"].to_argv() == [*platform.default_shell(), "./check.sh"]` with `assert cfg.hooks["check"].to_argv() == platform.shell_argv("./check.sh")`.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_platform.py tests/test_config_watch.py -q` → FAIL (`AttributeError: ... 'shell_argv'`)

- [ ] **Step 3: Implement.** In `src/keepwatch/platform.py`:
- rename `_absolute` to `absolute_path` (and its uses in `default_app_dirs`);
- add after the `TERMINATED_EXIT` line: `NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # no console window for children`;
- replace `_ntdll.NtResumeProcess(int(popen._handle))` in `start_process` with:

```python
    status = _ntdll.NtResumeProcess(int(popen._handle))
    if status != 0:
        popen.kill()
        _kernel32.CloseHandle(job)
        raise OSError(f"cannot resume the new process (NTSTATUS 0x{status & 0xFFFFFFFF:08x})")
```

- add after `default_shell`:

```python
# Windows PowerShell writes redirected output in the console code page; keepwatch reads UTF-8.
_UTF8_PRELUDE = (
    "try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8 } catch { }; "
)


def shell_argv(text: str, shell: Sequence[str] | None = None) -> list[str]:
    """How to run a string command: `shell` if given, else the platform shell (UTF-8 output on Windows)."""
    if shell:
        return [*shell, text]
    if IS_WINDOWS:
        return [*_POWERSHELL, "-Command", _UTF8_PRELUDE + text]
    return ["/bin/sh", "-c", text]
```

In `src/keepwatch/paths.py`: delete its own `APP` constant and `_absolute` function; import `APP` and `absolute_path` from `keepwatch.platform` and use `absolute_path` where `_absolute` was used.

In `src/keepwatch/config.py`, replace the string branch of `Command.to_argv` with `return platform.shell_argv(self.shell, shell)`.

In `tests/conftest.py` (`make_watch`), write files with `encoding="utf-8"`: `(watch_dir / "config.toml").write_text(textwrap.dedent(config), encoding="utf-8")` and `path.write_text(text, encoding="utf-8")`.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/paths.py src/keepwatch/config.py tests/conftest.py tests/test_platform.py tests/test_config_watch.py tests/test_portable_hooks.py
git commit -m "platform: shell_argv with UTF-8 PowerShell output, NO_WINDOW, checked resume, one absolute_path"
```

---

### Task 2: `ctx.run` behaves like command hooks

**Files:**
- Modify: `src/keepwatch/ctx.py`, `src/keepwatch/worker.py`, `src/keepwatch/runner.py`
- Test: `tests/test_ctx.py`, `tests/test_portable_hooks.py`

**Interfaces:**
- Produces: `Ctx(..., shell: Sequence[str] | None = None)`; the worker request carries `"shell"` (the watch's `shell` key as a list, or `None`); `ctx.run` runs strings with `platform.shell_argv(text, shell)`, resolves list programs with `platform.command_argv(argv, cwd or watch_dir)`, and starts commands with `creationflags=platform.NO_WINDOW`. The logged `argv` stays what the hook passed.

- [ ] **Step 1: Write the failing tests.** In `tests/test_ctx.py`, add a keyword parameter `shell=None` to `make_ctx` and pass `shell=shell` to `Ctx(...)`. Append:

```python
def test_run_uses_the_watch_shell(tmp_path):
    ctx = make_ctx(tmp_path, shell=[sys.executable, "-c"])
    assert ctx.run("import sys; sys.exit(5)", check=False).returncode == 5


def test_run_resolves_relative_programs_like_hooks(tmp_path):
    ctx = make_ctx(tmp_path)
    helper = ctx.watch_dir / "helper.py"
    helper.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(int(sys.argv[1]))\n")
    helper.chmod(0o755)
    assert ctx.run(["./helper.py", "4"], check=False).returncode == 4
```

Append to `tests/test_portable_hooks.py`:

```python
def test_python_hook_ctx_run_uses_the_watch_shell(make_watch, call_for):
    exe = Path(sys.executable).as_posix()
    watch_dir = make_watch("s", config=f"shell = ['{exe}', '-c']\n", files={"watch.py": '''
        def check(ctx):
            return ctx.run("import sys; sys.exit(6)", check=False).returncode == 6
    '''})
    assert Runner().run(call_for(watch_dir)).status == "true"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_ctx.py tests/test_portable_hooks.py -q` → FAIL (`TypeError: ... unexpected keyword argument 'shell'`)

- [ ] **Step 3: Implement.** In `src/keepwatch/ctx.py`: add the keyword parameter `shell: Sequence[str] | None = None` to `Ctx.__init__` (after `emit`) and store `self._shell = list(shell) if shell else None`; in `run`, replace the first positional argument of `subprocess.run(...)` (`[*platform.default_shell(), args] if shell else args`) with:

```python
                platform.shell_argv(args, self._shell)
                if shell
                else platform.command_argv(args, Path(cwd) if cwd is not None else self.watch_dir),
```

and add `creationflags=platform.NO_WINDOW,` to the same call. Update the docstring sentence to: "argv: a list runs directly (a relative program is resolved against the watch directory, and on Windows scripts get their interpreter, as for list hooks); a string runs through the watch's `shell`, or the platform shell."

In `src/keepwatch/runner.py` (`_run_python`), add to the `request` dict: `"shell": list(call.watch.shell) if call.watch.shell else None,`. In `src/keepwatch/worker.py`, add `shell=request.get("shell"),` to the `Ctx(...)` call.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/ctx.py src/keepwatch/worker.py src/keepwatch/runner.py tests/test_ctx.py tests/test_portable_hooks.py
git commit -m "ctx.run follows the watch shell and resolves programs like command hooks"
```

---

### Task 3: alert_command and helper processes without console windows

**Files:**
- Modify: `src/keepwatch/alerts.py`, `src/keepwatch/service.py`, `src/keepwatch/winsched.py`, `src/keepwatch/runner.py`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Produces: `run_alert(..., base: Path | None = None)` — list commands go through `platform.command_argv(argv, base)` when `base` is given; `Service._alert` passes `base=self.config_path.parent`; `subprocess.run` in `run_alert`, `winsched.run_powershell` and `Runner.prepare_environment` passes `creationflags=platform.NO_WINDOW`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_alerts.py`:

```python
def test_alert_list_programs_are_relative_to_the_config_dir(tmp_path):
    helper = tmp_path / "alert.py"
    helper.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(4)\n")
    helper.chmod(0o755)
    records = []
    run_alert(Command(argv=("./alert.py",)), event="offline", watch="w", reason="r", environment={},
              timeout=10, sink=records.append, base=tmp_path)
    assert (records[0]["status"], records[0]["exit_code"]) == ("failed", 4)
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_alerts.py -q` → FAIL (`TypeError: ... unexpected keyword argument 'base'`)

- [ ] **Step 3: Implement.** In `src/keepwatch/alerts.py`: add `from pathlib import Path` and `from keepwatch import platform`; add the keyword parameter `base: Path | None = None` to `run_alert` (after `capture_bytes`); before the `try:` add:

```python
    argv = command.to_argv()
    if command.argv is not None and base is not None:
        argv = platform.command_argv(argv, base)
```

and in the `subprocess.run(...)` call use `argv` instead of `command.to_argv()` and add `creationflags=platform.NO_WINDOW,`. In `src/keepwatch/service.py` (`_alert`), add `base=self.config_path.parent,` to the `run_alert(...)` call. In `src/keepwatch/winsched.py` (`run_powershell`) add `from keepwatch import platform` and `creationflags=platform.NO_WINDOW,` to its `subprocess.run`. In `src/keepwatch/runner.py` (`prepare_environment`) add `creationflags=platform.NO_WINDOW,` to its `subprocess.run`.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/alerts.py src/keepwatch/service.py src/keepwatch/winsched.py src/keepwatch/runner.py tests/test_alerts.py
git commit -m "No console windows for alerts, PowerShell and uv; alert programs relative to the config dir"
```

---

### Task 4: Rotation that never half-runs; start failures logged without a console

**Files:**
- Modify: `src/keepwatch/logstore.py`, `src/keepwatch/cli.py`, `src/keepwatch/output.py`
- Test: `tests/test_logstore.py`, `tests/test_cli_run.py`, `tests/test_output.py`

**Interfaces:**
- Produces: `LogWriter._rotate` first renames the log to `<name>.rotating`; if that fails nothing else changes; if shifting the backups fails, the log is renamed back. `cli._LOG_FILE` (set by the `cli` group callback to `paths.log_file`); `_fail` writes a `cli.error` record (CRITICAL; `error`, `argv`) to the log when there is no console (`sys.stderr is None`). `format_record` prints `cli.error` as `command failed: <error>`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_logstore.py` (add `from pathlib import Path` to the imports if missing):

```python
def test_failed_rotation_leaves_backups_alone(tmp_path, monkeypatch):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=200, backups=2)
    for n in range(12):
        writer.write({"n": n, "pad": "x" * 80})

    def backups():
        return {p.name: p.read_text() for p in tmp_path.iterdir() if p.name.startswith("keepwatch.jsonl.")}

    before = backups()
    assert sorted(before) == ["keepwatch.jsonl.1", "keepwatch.jsonl.2"]
    real_rename = Path.rename

    def refuse_the_log(self, target):
        if self == path:
            raise PermissionError(13, "in use by another process")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_the_log)
    for n in range(5):
        writer.write({"n": 100 + n, "pad": "y" * 80})
    assert backups() == before
    assert json.loads(path.read_text().splitlines()[-1])["n"] == 104
```

Append to `tests/test_cli_run.py`:

```python
def test_fail_without_a_console_writes_to_the_log(xdg, monkeypatch):
    from keepwatch import cli as cli_module

    monkeypatch.setattr(cli_module, "_LOG_FILE", xdg.log_file)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(SystemExit):
        cli_module._fail("broken config: line 3")
    record = json.loads(xdg.log_file.read_text().splitlines()[-1])
    assert (record["event"], record["level"], record["error"]) == ("cli.error", "CRITICAL", "broken config: line 3")
```

Append to `tests/test_output.py`:

```python
def test_cli_error_is_readable():
    record = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "CRITICAL", "event": "cli.error", "error": "x: y"}
    assert format_record(record) == "10:11:12 command failed: x: y"
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_logstore.py tests/test_cli_run.py tests/test_output.py -q` → FAIL (backups shifted; `_LOG_FILE` missing; generic formatting)

- [ ] **Step 3: Implement.** In `src/keepwatch/logstore.py`, replace `_rotate` with:

```python
    def _rotate(self) -> None:
        """Shift the backups. If the log cannot be moved (Windows: someone holds it open), change nothing."""
        if self.backups <= 0:
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError:
                pass
            return
        staging = self.path.with_name(f"{self.path.name}.rotating")
        try:
            self.path.rename(staging)
        except PermissionError:
            return  # try again on a later write
        try:
            self._backup(self.backups).unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self._backup(index)
                if source.exists():
                    source.rename(self._backup(index + 1))
            staging.rename(self._backup(1))
        except PermissionError:
            staging.rename(self.path)  # keep writing to the same log; rotate later
```

In `src/keepwatch/cli.py`: add `_LOG_FILE: Path | None = None` near the top (after the constants); change the logstore import to include `make_record`; in the `cli` group callback add `global _LOG_FILE` as its first statement and `_LOG_FILE = paths.log_file` after `paths = resolve_paths()`; replace `_fail` with:

```python
def _fail(message: str) -> NoReturn:
    if sys.stderr is not None:
        make_console(stderr=True).print(message)
    elif _LOG_FILE is not None:
        # pythonw (Windows logon task): there is no console, so leave the reason in the log.
        try:
            LogWriter(_LOG_FILE, max_bytes=10_000_000, backups=10).write(
                make_record("cli.error", level="CRITICAL", error=message, argv=sys.argv[1:])
            )
        except OSError:
            pass
    raise SystemExit(1)
```

In `src/keepwatch/output.py`, add after `_service_error`:

```python
def _cli_error(record: dict[str, Any], verbose: bool) -> str:
    return f"command failed: {record.get('error')}"
```

and `"cli.error": _cli_error,` to `_FORMATTERS`.

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/logstore.py src/keepwatch/cli.py src/keepwatch/output.py tests/test_logstore.py tests/test_cli_run.py tests/test_output.py
git commit -m "Rotation never half-runs; log start failures when there is no console"
```

---

### Task 5: Stop requests, install/start split, duplicate uninstall block

**Files:**
- Modify: `src/keepwatch/service.py`, `src/keepwatch/cli.py`, `src/keepwatch/winsched.py`
- Test: `tests/test_service.py`, `tests/test_cli_install.py`

**Interfaces:**
- Produces: `Service(..., stale_before: float | None = None)` — a stop request whose file is older than `stale_before` is deleted and ignored (`run_service` passes the time it took the service lock); `Service.start` no longer deletes the stop request. `winsched.register_script` no longer starts the task and removes any Startup-folder shortcut left by an earlier fallback; `winsched.start_script()` starts the task; `install` on Windows: register → (on failure) shortcut fallback; (on success) start, where a start failure is only a warning. `uninstall` has one Windows block.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_service.py` (add `import os` if missing):

```python
def test_stale_stop_requests_are_ignored(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    started = time.time()
    service = Service(paths=xdg, config_path=xdg.config_file, sink=records.append, start_threads=False,
                      stale_before=started)
    xdg.stop_request.parent.mkdir(parents=True, exist_ok=True)
    xdg.stop_request.write_text("old")
    os.utime(xdg.stop_request, (started - 100, started - 100))
    thread = threading.Thread(target=service.run, args=(threading.Event(),), daemon=True)
    thread.start()
    time.sleep(2.5)
    assert thread.is_alive()
    assert not xdg.stop_request.exists()
    xdg.stop_request.write_text("new")
    thread.join(15)
    assert not thread.is_alive()
    assert "service.stop_requested" in [r["event"] for r in records]
```

In `tests/test_cli_install.py`, replace `test_windows_install_registers_a_logon_task` with the two tests below and append the third:

```python
def test_windows_install_registers_and_starts_a_logon_task(xdg, monkeypatch):
    scripts = on_windows(monkeypatch)
    result = run("install")
    assert result.exit_code == 0, result.output
    register, start = scripts
    assert "Register-ScheduledTask" in register and "New-ScheduledTaskTrigger -AtLogOn" in register
    assert "-m keepwatch run" in register and "keepwatch.lnk" in register
    assert "Start-ScheduledTask" not in register and "Start-ScheduledTask" in start
    assert "registered Task Scheduler task 'keepwatch'" in result.output and "started it" in result.output


def test_windows_install_start_failure_is_only_a_warning(xdg, monkeypatch):
    scripts = on_windows(monkeypatch, exit_codes=(0, 1))
    result = run("install")
    assert result.exit_code == 0, result.output
    assert len(scripts) == 2
    assert "will start at the next logon" in result.output


def test_windows_uninstall_has_one_windows_branch():
    import inspect

    source = inspect.getsource(cli_module.uninstall.callback)
    assert source.count("if _on_windows():") == 1
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_service.py tests/test_cli_install.py -q` → FAIL (`TypeError: ... 'stale_before'`; one script instead of two; the duplicate block)

- [ ] **Step 3: Implement.**

In `src/keepwatch/service.py`: add the keyword parameter `stale_before: float | None = None` to `Service.__init__` and store it as `self._stale_before`; delete `self.paths.stop_request.unlink(missing_ok=True)` from `start()`; replace `_stop_requested` with:

```python
    def _stop_requested(self) -> bool:
        """True (once) when `keepwatch stop` asked this service to stop. Requests older than the lock are stale."""
        request = self.paths.stop_request
        try:
            written = request.stat().st_mtime
            request.unlink()
        except OSError:
            return False
        if self._stale_before is not None and written < self._stale_before:
            return False
        self._sink(make_record("service.stop_requested"))
        return True
```

In `src/keepwatch/cli.py` (`run_service`): right after the `stack.enter_context(hold_lock(...))` line succeeds, add `locked_at = time.time()`, and pass `stale_before=locked_at` to `Service(...)`. In `uninstall`, delete the second, identical `if _on_windows():` block. In `_install_windows`, replace everything from `result = winsched.run_powershell(register)` to the line before `click.echo("check it with: keepwatch status")` with:

```python
    result = winsched.run_powershell(register)
    if result.returncode == 0:
        started = winsched.run_powershell(winsched.start_script())
        if started.returncode == 0:
            click.echo(f"registered Task Scheduler task '{winsched.TASK_NAME}' (at logon, restarted on failure) and started it")
        else:
            click.echo(
                f"registered Task Scheduler task '{winsched.TASK_NAME}', but could not start it now "
                f"({_last_line(started.stderr)}); it will start at the next logon"
            )
    else:
        click.echo(f"Task Scheduler refused ({_last_line(result.stderr)}); using a Startup-folder shortcut instead")
        fallback = winsched.run_powershell(shortcut)
        if fallback.returncode != 0:
            _fail(f"could not create the Startup-folder shortcut either: {_last_line(fallback.stderr)}")
        click.echo("created a Startup-folder shortcut (keepwatch.lnk) and started keepwatch")
```

In `src/keepwatch/winsched.py`, in `register_script` replace the last list item (`f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}",`) with `f"Remove-Item -LiteralPath {_SHORTCUT} -ErrorAction SilentlyContinue",` and add:

```python
def start_script() -> str:
    return f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}"
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/service.py src/keepwatch/cli.py src/keepwatch/winsched.py tests/test_service.py tests/test_cli_install.py
git commit -m "Ignore stale stop requests; split task registration from start; remove a duplicate uninstall block"
```

---

### Task 6: UTF-8 output in PowerShell scripts; docs

**Files:**
- Modify: `src/keepwatch/templates.py`, `src/keepwatch/examples/disk-space-ps/low_space.ps1`, `src/keepwatch/examples/disk-space-ps/notify.ps1`, `src/keepwatch/reference/executables.md`
- Test: `tests/test_docs.py`

- [ ] **Step 1: Write the failing test.** In `tests/test_docs.py`, append `"UTF8Encoding"` to the `"executables"` list in `KEY_FACTS`.

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_docs.py -q` → FAIL

- [ ] **Step 3: Implement.** Make this line the first line after the comments of `POWERSHELL_CHECK` and `POWERSHELL_ACTION` in `templates.py`, and the first line of `low_space.ps1` and the line after `param(...)` in `notify.ps1`:

```powershell
try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
```

In `src/keepwatch/reference/executables.md`, replace the paragraph under "## Output" that starts "stdout and stderr are captured" with:

```markdown
stdout and stderr are captured into the hook's log record (the first and last 32 KiB of each with the default `capture_bytes`) and decoded as UTF-8; anything that is not valid UTF-8 is replaced, not fatal. String hooks in the default Windows PowerShell are switched to UTF-8 output automatically. A `.ps1` script should start with `try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }` (the templates and examples do), otherwise non-ASCII output arrives garbled. A command that leaves a background process holding its stdout open is not waited for: once the command exits, keepwatch waits two seconds and then stops its process tree.
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/templates.py src/keepwatch/examples/disk-space-ps src/keepwatch/reference/executables.md tests/test_docs.py
git commit -m "UTF-8 output for PowerShell scripts in templates and examples; document it"
```
