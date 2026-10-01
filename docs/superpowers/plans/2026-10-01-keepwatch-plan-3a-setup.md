# keepwatch Plan 3a (Dependencies and Setup Commands) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let watches declare third-party Python packages (installed by uv), and add the setup commands `new`, `init`, `install` and `uninstall`.

**Architecture:** A watch with `python_dependencies` runs its worker through `uv run --no-project --with …`, offline first (cached environment) with one online retry. The worker imports the running keepwatch through a `PYTHONPATH` shim directory holding only a symlink to the `keepwatch` package. `validate` builds the environment ahead of time. Templates for `new` live in `templates.py`; `install` writes a systemd user unit and drives `systemctl --user` (overridable through `KEEPWATCH_SYSTEMCTL` so tests never touch the real systemd).

**Tech Stack:** Python ≥ 3.12, uv, rich-click, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md` (sections 10, 13 Setup, 15). Builds on Plans 1, 1b and 2 (implemented).

## Global Constraints

- All Global Constraints of Plans 1 and 2 still apply (explicit `git add`, ruff clean before every commit with `--fix` allowed only for I001/W29x, tests touch only temporary directories).
- Tests must never run the real `systemctl` or write to the real `~/.config/systemd`: `install`/`uninstall` tests set `KEEPWATCH_SYSTEMCTL` to a fake script and use the `xdg` fixture's `XDG_CONFIG_HOME`.
- Tests that need network access (uv downloading a package) are marked `@pytest.mark.network` and are skipped when `KEEPWATCH_SKIP_NETWORK=1` or `uv` is not on `PATH`.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A watch with `python_dependencies` must work at login without network once `validate` has built its environment (offline first). Test: Task 1 `test_dependencies_are_installed_and_importable` runs `validate` first and then a poll.
- `uv` missing must give an actionable error, not a traceback. Test: Task 1 `test_dependencies_without_uv_are_reported`.
- `new` must refuse to overwrite an existing watch. Test: Task 2 `test_new_refuses_existing_and_invalid_names`.
- `init` must never overwrite an existing config. Test: Task 3 `test_init_never_overwrites`.
- A failing `systemctl` must make `install` exit 1 with systemctl's message. Test: Task 4 `test_install_reports_systemctl_failure`.

---

### Task 1: `python_dependencies` through uv

**Files:**
- Modify: `src/keepwatch/runner.py`, `src/keepwatch/validation.py`, `pyproject.toml` (register the `network` marker)
- Test: `tests/test_runner_python.py`, `tests/test_cli_validate.py`

**Interfaces:**
- Produces:
  - `Runner(*, python=sys.executable, kill_grace=KILL_GRACE, uv: str | None = None)`; attribute `uv` (defaults to `shutil.which("uv")`; set it to `None` to simulate a missing uv)
  - `Runner.prepare_environment(watch: WatchConfig, timeout: float = 600.0) -> str | None` — builds (or reuses) the uv environment; returns a problem message or None
  - `runner.make_shim(directory: Path) -> Path` — creates `directory/keepwatch` as a symlink to the running `keepwatch` package (idempotent) and returns `directory`
  - Python hooks of watches with dependencies run as `uv run --no-project --quiet --python <python> [--offline] --with <dep>... python -m keepwatch.worker ...` with `PYTHONPATH` starting with `<run_dir>/../lib`; the first attempt is `--offline`, and if the worker never started (no hello) it is retried once online within the same deadline

- [ ] **Step 1: Write the failing tests.** In `pyproject.toml` add under `[tool.pytest.ini_options]`:

```toml
markers = ["network: needs network access (skipped when KEEPWATCH_SKIP_NETWORK=1 or uv is missing)"]
```

In `tests/test_runner_python.py`, add `import shutil` at the top, replace the whole `test_python_dependencies_not_supported_yet` test with the tests below, and append the `network` helper near the top of the file (after the imports):

```python
network = pytest.mark.skipif(
    os.environ.get("KEEPWATCH_SKIP_NETWORK") == "1" or shutil.which("uv") is None,
    reason="needs uv and network access",
)
```

```python
def test_dependencies_without_uv_are_reported(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["six"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    runner = Runner()
    runner.uv = None
    result = runner.run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("python_dependencies needs uv on PATH")
    assert runner.prepare_environment(load_watch_config(watch_dir)).startswith("python_dependencies needs uv on PATH")


def test_make_shim_links_the_running_package(tmp_path):
    import keepwatch

    shim = make_shim(tmp_path / "lib")
    assert (shim / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent
    assert make_shim(tmp_path / "lib") == shim


@pytest.mark.network
@network
def test_dependencies_are_installed_and_importable(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["six==1.16.0"]\n', files={"watch.py": '''
        import six

        def check(ctx):
            return True, six.__version__
    '''})
    runner = Runner()
    assert runner.prepare_environment(load_watch_config(watch_dir)) is None
    result = runner.run(call_for(watch_dir, timeout=120))
    assert result.status == "true", (result.reason, result.stderr)
    assert result.payload == "1.16.0"
```

Also add to the imports of `tests/test_runner_python.py`: `import pytest`, `from keepwatch.config import load_watch_config` and change the runner import to `from keepwatch.runner import Runner, make_shim`.

Append to `tests/test_cli_validate.py`:

```python
def test_validate_reports_missing_uv(xdg, make_watch, monkeypatch):
    make_watch("dep", config='python_dependencies = ["six"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    monkeypatch.setenv("PATH", "/nonexistent")
    result = run("validate", "dep")
    assert result.exit_code == 1
    assert "python_dependencies needs uv on PATH" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_runner_python.py tests/test_cli_validate.py -q`
Expected: FAIL (`ImportError: cannot import name 'make_shim'`; validate still says "not supported")

- [ ] **Step 3: Implement** in `src/keepwatch/runner.py`.

Add `import shutil` to the stdlib imports, and below the `setting_variables` function add:

```python
def make_shim(directory: Path) -> Path:
    """A PYTHONPATH directory exposing only the running keepwatch package (for uv environments)."""
    import keepwatch

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = directory / "keepwatch"
    if not link.exists():
        link.symlink_to(Path(keepwatch.__file__).resolve().parent, target_is_directory=True)
    return directory


@dataclass
class _WorkerRun:
    returncode: int
    timed_out: bool
    out: _Reader
    err: _Reader
    messages: _MessageReader
```

Change `Runner.__init__` to:

```python
    def __init__(self, *, python: str = sys.executable, kill_grace: float = KILL_GRACE, uv: str | None = None) -> None:
        self.python = python
        self.kill_grace = kill_grace
        self.uv = uv or shutil.which("uv")
        self._active: set[int] = set()
        self._active_lock = threading.Lock()
```

Add these methods to `Runner` (before `_run_python`):

```python
    def _uv_prefix(self, dependencies: Sequence[str], *, offline: bool) -> list[str]:
        argv = [self.uv or "uv", "run", "--no-project", "--quiet", "--python", self.python]
        if offline:
            argv.append("--offline")
        for dependency in dependencies:
            argv += ["--with", dependency]
        return argv

    def prepare_environment(self, watch: WatchConfig, timeout: float = 600.0) -> str | None:
        """Build (or reuse) the uv environment for python_dependencies. Returns a problem, or None."""
        if not watch.python_dependencies:
            return None
        if not self.uv:
            return _NO_UV
        argv = [*self._uv_prefix(watch.python_dependencies, offline=False), "python", "-c", "pass"]
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"building the environment for python_dependencies timed out after {format_duration(timeout)}"
        except OSError as exc:
            return f"cannot run uv ({self.uv}): {exc}"
        if completed.returncode != 0:
            tail = " | ".join(completed.stderr.strip().splitlines()[-5:])
            return f"uv could not build the environment for python_dependencies: {tail}"
        return None

    def _start_worker(
        self,
        call: HookCall,
        request: dict[str, Any],
        env: dict[str, str],
        *,
        offline: bool,
        timeout: float,
    ) -> _WorkerRun | str:
        req_r, req_w = os.pipe()
        res_r, res_w = os.pipe()
        tail = ["-m", "keepwatch.worker", "--request-fd", str(req_r), "--result-fd", str(res_w)]
        if call.watch.python_dependencies:
            argv = [*self._uv_prefix(call.watch.python_dependencies, offline=offline), "python", *tail]
        else:
            argv = [self.python, *tail]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=call.watch.watch_dir,
                env=env,
                pass_fds=(req_r, res_w),
                start_new_session=True,
            )
        except OSError as exc:
            for fd in (req_r, req_w, res_r, res_w):
                os.close(fd)
            return f"cannot start the Python worker ({argv[0]}): {exc}"
        os.close(req_r)
        os.close(res_w)
        _Writer(req_w, json.dumps(request).encode("utf-8")).start()
        out = _Reader(proc.stdout, call.capture_bytes)
        err = _Reader(proc.stderr, call.capture_bytes)
        messages = _MessageReader(os.fdopen(res_r, "rb"), RESULT_LIMIT, call.on_message)
        for reader in (out, err, messages):
            reader.start()
        self._track(proc.pid)
        try:
            returncode, timed_out = self._supervise(proc, (out, err, messages), timeout)
        finally:
            self._untrack(proc.pid)
        return _WorkerRun(returncode, timed_out, out, err, messages)
```

Below `RESULT_LIMIT`/`FAILED_STATUSES` add:

```python
_NO_UV = "python_dependencies needs uv on PATH (https://docs.astral.sh/uv/); install it or remove python_dependencies"
```

Replace the whole `_run_python` method with the two methods below (the second holds the result interpretation that `_run_python` did before, unchanged in behaviour):

```python
    def _run_python(self, call: HookCall) -> HookResult:
        target = f"watch.py:{call.hook}" if call.mode == "call" else "watch.py (describe)"
        started = time.monotonic()
        dependencies = call.watch.python_dependencies
        if dependencies and not self.uv:
            return self._error(call, "python", target, started, _NO_UV)
        deadline = time.time() + call.timeout
        request = {
            "watch": call.watch.name,
            "hook": call.hook,
            "watch_dir": str(call.watch.watch_dir),
            "data_dir": str(call.data_dir),
            "run_dir": str(call.run_dir),
            "poll_id": call.poll_id,
            "condition": call.condition,
            "payload": call.payload,
            "settings": dict(call.watch.settings),
            "deadline": deadline,
            "capture_bytes": call.capture_bytes,
            "mode": call.mode,
        }
        env = self._environment(call)
        env["PYTHONUNBUFFERED"] = "1"
        if dependencies:
            shim = make_shim(call.run_dir.parent / "lib")
            env["PYTHONPATH"] = os.pathsep.join(part for part in (str(shim), env.get("PYTHONPATH", "")) if part)
        run: _WorkerRun | None = None
        for offline in (True, False) if dependencies else (False,):
            attempt = self._start_worker(call, request, env, offline=offline, timeout=max(deadline - time.time(), 0.0))
            if isinstance(attempt, str):
                return self._error(call, "python", target, started, attempt)
            run = attempt
            # Offline first: when the environment is not cached yet, uv fails before the worker says hello.
            if not (offline and run.messages.hello is None and not run.timed_out):
                break
        assert run is not None
        return self._worker_result(call, target, started, run)

    def _worker_result(self, call: HookCall, target: str, started: float, run: _WorkerRun) -> HookResult:
        results = run.messages
        base = self._captured(run.out, run.err, run.returncode, started)
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
        returncode = run.returncode
        if run.timed_out:
            return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
        hello = results.hello
        result = results.result
        if hello is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) before starting; see stderr",
                **common,
            )
        if hello.get("protocol") != PROTOCOL_VERSION or hello.get("version") != _EXPECTED_VERSION:
            return HookResult(
                status=failure,
                reason=(
                    f"keepwatch version mismatch: service {_EXPECTED_VERSION} (protocol {PROTOCOL_VERSION}), "
                    f"worker {hello.get('version')} (protocol {hello.get('protocol')})"
                ),
                **common,
            )
        if result is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) without reporting a result; see stderr",
                **common,
            )
        status = result.get("status")
        details = {"reason": result.get("reason"), "exception": result.get("exception")}
        if call.mode == "describe":
            return HookResult(status="ok" if status == "ok" else "error", hooks=result.get("hooks"), **details, **common)
        if call.hook == CHECK:
            if status == "answer":
                mapped = "true" if result.get("answer") else "false"
            elif status == "unknown":
                mapped = "unknown"
            else:
                mapped = "error"
            return HookResult(status=mapped, payload=result.get("payload"), **details, **common)
        return HookResult(status="ok" if status == "ok" else "failed", **details, **common)
```

Before replacing, compare the old `_run_python`'s result interpretation with `_worker_result` above; if they differ in anything other than reading from `run`, stop and report.

In `src/keepwatch/validation.py`, replace the block that starts with `if watch.python_dependencies:` and ends at the end of the `elif source.is_file() and problem is None:` branch (just before `return report`) with:

```python
    can_describe = source.is_file() and problem is None
    if watch.python_dependencies:
        prepared = runner.prepare_environment(watch)
        if prepared is not None:
            report.problems.append(f"{watch.config_file}: {prepared}; see: keepwatch docs dependencies")
            can_describe = False
    if can_describe:
        result = runner.run(
            HookCall(
                watch=watch,
                hook=CHECK,
                poll_id="validate",
                condition=watch.initial_condition,
                payload=None,
                data_dir=paths.watch_data_dir(watch.name),
                run_dir=paths.run_dir(pid, watch.name),
                timeout=max(watch.check_timeout, 120.0) if watch.python_dependencies else watch.check_timeout,
                capture_bytes=global_config.log.capture_bytes,
                environment=global_config.environment,
                mode="describe",
            )
        )
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

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS (the network test runs when uv and network are available; if it fails only because the network is down, re-run with `KEEPWATCH_SKIP_NETWORK=1` and say so in your report)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/keepwatch/runner.py src/keepwatch/validation.py tests/test_runner_python.py tests/test_cli_validate.py
git commit -m "Support python_dependencies through uv, offline first"
```

---

### Task 2: `keepwatch new`

**Files:**
- Create: `src/keepwatch/templates.py`
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_cli_new.py`

**Interfaces:**
- Produces: `templates.TEMPLATES: dict[str, dict[str, str]]` (template name → relative path → content, with `{name}` placeholders replaced by `str.replace`); `render(template: str, name: str) -> dict[str, str]`; `keepwatch new NAME [--template python|shell|expect] [--dir DIR]` — exit 0 on success, 1 if the name is invalid or taken
- Files whose content starts with `#!` are created executable (0755).

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_new.py`

```python
import json
import os
import stat

from click.testing import CliRunner

from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_python_template_validates_and_polls(xdg):
    result = run("new", "fresh")
    assert result.exit_code == 0, result.output
    watch_dir = xdg.default_watches_dir / "fresh"
    assert sorted(p.name for p in watch_dir.iterdir()) == ["config.toml", "watch.py"]
    assert "keepwatch validate fresh" in result.output
    assert run("validate", "fresh").exit_code == 0
    data = json.loads(run("poll", "fresh", "--json").output)
    assert data["polls"][0]["outcome"] == "false"


def test_shell_template_validates_and_scripts_are_executable(xdg):
    assert run("new", "sh-watch", "--template", "shell").exit_code == 0
    watch_dir = xdg.default_watches_dir / "sh-watch"
    for script in ("check.sh", "on_true.sh"):
        assert os.stat(watch_dir / script).st_mode & stat.S_IXUSR
    assert run("validate", "sh-watch").exit_code == 0
    data = json.loads(run("poll", "sh-watch", "--fake", "true", "--json").output)
    assert data["polls"][0]["results"] == [{"hook": "on_true", "status": "ok", "reason": None}]


def test_expect_template_files(xdg):
    assert run("new", "exp", "--template", "expect").exit_code == 0
    watch_dir = xdg.default_watches_dir / "exp"
    assert sorted(p.name for p in watch_dir.iterdir()) == ["check.sh", "config.toml", "on_true.exp"]
    assert (watch_dir / "on_true.exp").read_text().startswith("#!/usr/bin/env expect")


def test_new_into_another_directory(xdg, tmp_path):
    target = tmp_path / "elsewhere"
    assert run("new", "x", "--dir", str(target)).exit_code == 0
    assert (target / "x" / "watch.py").exists()


def test_new_refuses_existing_and_invalid_names(xdg):
    assert run("new", "dup").exit_code == 0
    (xdg.default_watches_dir / "dup" / "watch.py").write_text("# mine\n")
    result = run("new", "dup")
    assert result.exit_code == 1 and "already exists" in result.output
    assert (xdg.default_watches_dir / "dup" / "watch.py").read_text() == "# mine\n"
    result = run("new", "bad name")
    assert result.exit_code == 1 and "not a valid watch name" in result.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_new.py -q`
Expected: FAIL with `No such command 'new'`

- [ ] **Step 3: Implement** — `src/keepwatch/templates.py`

```python
"""Starting points for `keepwatch new`. "{name}" is replaced with the watch name."""

from __future__ import annotations

PYTHON_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"
# check_timeout = "60s"
# action_timeout = "60s"
# max_failures = 5
# retry_after = "1h"
# python_dependencies = ["requests>=2.32"]

[settings]
# Free-form parameters for watch.py: ctx.settings["example"]
example = "value"
'''

PYTHON_WATCH = '''"""Watch "{name}". Contract: keepwatch docs python. Helpers: keepwatch docs ctx."""

from keepwatch import Ctx


def check(ctx: Ctx):
    """Look at the world; never change it.

    Return True or False (optionally as (answer, payload) with a JSON payload for the actions),
    None when you cannot tell right now, or raise an exception if the check itself broke.
    """
    return False


def on_true(ctx: Ctx) -> None:
    """Runs on every poll while the condition is TRUE. Raise to report a failure.

    Other hooks: on_rise (FALSE -> TRUE), on_fall (TRUE -> FALSE), on_false (every poll while FALSE).
    """
    ctx.log.info("condition is TRUE; payload=%s", ctx.payload)
'''

SHELL_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"

[hooks]
# A list runs directly; a string runs through /bin/sh -c. See: keepwatch docs executables
check = ["./check.sh"]
on_true = ["./on_true.sh"]

[check_exit_codes]
# Defaults: true = [0], false = [1]; other codes are errors unless listed here.
unknown = [3]

[settings]
# Reaches the scripts as $KEEPWATCH_SETTING_EXAMPLE
example = "value"
'''

SHELL_CHECK = '''#!/bin/sh
# Check for watch "{name}": exit 0 = TRUE, 1 = FALSE, 3 = unknown (see config.toml).
# To hand data to the actions, write JSON to "$KEEPWATCH_PAYLOAD_OUT".
# Never change anything here: keepwatch poll --dry-run runs this for real.
exit 1
'''

SHELL_ACTION = '''#!/bin/sh
# Runs on every poll while the condition of "{name}" is TRUE. Exit nonzero to report a failure.
# The check's payload (if any) is in the JSON file "$KEEPWATCH_PAYLOAD_FILE".
set -eu
echo "condition is TRUE; example setting: $KEEPWATCH_SETTING_EXAMPLE"
'''

EXPECT_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"
action_timeout = "2m"

[hooks]
check = ["./check.sh"]
on_true = ["expect", "./on_true.exp"]

[settings]
# Reaches the Expect script as $env(KEEPWATCH_SETTING_HOST)
host = "example.com"
'''

EXPECT_ACTION = '''#!/usr/bin/env expect
# Runs on every poll while the condition of "{name}" is TRUE. exit 1 reports a failure.
# stdin is /dev/null, so drive interactive programs through spawn/expect only.
set timeout 30
set host $env(KEEPWATCH_SETTING_HOST)
spawn echo "connecting to $host"
expect {
    "connecting" { }
    timeout { exit 1 }
}
expect eof
'''

TEMPLATES: dict[str, dict[str, str]] = {
    "python": {"config.toml": PYTHON_CONFIG, "watch.py": PYTHON_WATCH},
    "shell": {"config.toml": SHELL_CONFIG, "check.sh": SHELL_CHECK, "on_true.sh": SHELL_ACTION},
    "expect": {"config.toml": EXPECT_CONFIG, "check.sh": SHELL_CHECK, "on_true.exp": EXPECT_ACTION},
}


def render(template: str, name: str) -> dict[str, str]:
    return {path: content.replace("{name}", name) for path, content in TEMPLATES[template].items()}
```

In `src/keepwatch/cli.py`, add `is_valid_watch_name` to the `keepwatch.config` import and `from keepwatch.templates import TEMPLATES, render`, put `"new"` first in the Develop panel's command list (`["new", "validate", "poll"]`), and add before `def main()`:

```python
@cli.command()
@click.argument("name")
@click.option("--template", type=click.Choice(sorted(TEMPLATES)), default="python", show_default=True,
              help="python: watch.py with check() and on_true(). shell: check.sh and on_true.sh. "
              "expect: a shell check and an Expect action.")
@click.option("--dir", "base", type=click.Path(path_type=Path, file_okay=False),
              help="Watches directory to create it in. Default: the first entry of watch_dirs.")
@click.pass_obj
def new(app: App, name: str, template: str, base: Path | None) -> None:
    """Create watch NAME from a commented template, ready to validate and poll.

    The template's comments point at the reference topics for every part. Next steps are printed.

    Exit status: 0, or 1 if NAME is not a valid watch name or already exists.
    """
    if not is_valid_watch_name(name):
        _fail(f"'{name}' is not a valid watch name: use letters, digits, '_', '.' and '-', starting with a letter or digit")
    if base is None:
        try:
            base = app.load_global().watch_dirs[0]
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
    watch_dir = base / name
    if watch_dir.exists():
        _fail(f"{watch_dir} already exists; choose another name or edit it directly")
    watch_dir.mkdir(parents=True)
    for relative, content in render(template, name).items():
        path = watch_dir / relative
        path.write_text(content, encoding="utf-8")
        if content.startswith("#!"):
            path.chmod(0o755)
    click.echo(f"created {watch_dir} from the {template} template")
    click.echo(f"next: edit it, then run `keepwatch validate {name}` and `keepwatch poll {name} --dry-run`")
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/templates.py src/keepwatch/cli.py tests/test_cli_new.py
git commit -m "Add keepwatch new with python, shell and expect templates"
```

---

### Task 3: `keepwatch init`

**Files:**
- Modify: `src/keepwatch/templates.py`, `src/keepwatch/cli.py`
- Test: `tests/test_cli_init.py`

**Interfaces:**
- Produces: `templates.GLOBAL_CONFIG: str` (every key commented out with its default; parses to the defaults); `keepwatch init` — creates the global config file and the default watches directory if missing, never overwrites, reports each path as `created` or `exists`; exit 0

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_init.py`

```python
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.config import LogSettings, load_global_config


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_init_creates_config_and_watches_dir(xdg):
    result = run("init")
    assert result.exit_code == 0, result.output
    assert f"created {xdg.config_file}" in result.output
    assert xdg.default_watches_dir.is_dir()
    config = load_global_config(xdg.config_file, xdg)
    assert config.watch_dirs == (xdg.default_watches_dir,)
    assert config.reload_interval == 5.0 and config.log == LogSettings()
    assert dict(config.defaults) == {}


def test_init_never_overwrites(xdg):
    xdg.config_home.mkdir(parents=True)
    xdg.config_file.write_text('reload_interval = "9s"\n')
    result = run("init")
    assert result.exit_code == 0
    assert f"exists  {xdg.config_file}" in result.output
    assert xdg.config_file.read_text() == 'reload_interval = "9s"\n'
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_init.py -q`
Expected: FAIL with `No such command 'init'`

- [ ] **Step 3: Implement.** Append to `src/keepwatch/templates.py`:

```python
GLOBAL_CONFIG = '''# keepwatch global config. Every key: keepwatch docs config
# Every key is optional; the commented values are the defaults. Changes apply live.

# Directories whose subdirectories are watches (earlier entries win name clashes).
# watch_dirs = ["~/.config/keepwatch/watches"]

# How often the service picks up config changes.
# reload_interval = "5s"

# Run when a watch goes offline or comes back online. Receives KEEPWATCH_ALERT_EVENT
# (offline/online), KEEPWATCH_WATCH and KEEPWATCH_ALERT_REASON.
# alert_command = 'notify-send "keepwatch: $KEEPWATCH_WATCH $KEEPWATCH_ALERT_EVENT" "$KEEPWATCH_ALERT_REASON"'

[defaults]
# Defaults for every watch's config.toml.
# interval = "60s"
# check_timeout = "60s"
# action_timeout = "60s"
# max_failures = 5
# retry_after = "1h"
# initial_condition = false

[environment]
# Extra environment variables for every hook, e.g. a stable ssh-agent socket:
# SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"

[log]
# max_bytes = 10000000
# backups = 10
# capture_bytes = 65536
'''
```

In `src/keepwatch/cli.py`, change the templates import to `from keepwatch.templates import GLOBAL_CONFIG, TEMPLATES, render`, add a Setup panel to `COMMAND_GROUPS` after Control: `{"name": "Setup", "commands": ["init", "install", "uninstall"]}`, and add before `def main()`:

```python
@cli.command()
@click.pass_obj
def init(app: App) -> None:
    """Create the global config file (all defaults, commented) and the default watches directory.

    Never overwrites anything: each path is reported as created or exists.

    Exit status: 0.
    """
    config_path = app.config_path
    if config_path.exists():
        click.echo(f"exists  {config_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(GLOBAL_CONFIG, encoding="utf-8")
        click.echo(f"created {config_path}")
    watches = app.paths.default_watches_dir
    if watches.is_dir():
        click.echo(f"exists  {watches}")
    else:
        watches.mkdir(parents=True)
        click.echo(f"created {watches}")
    click.echo("next: keepwatch new <name>, then keepwatch install to start the service at login")
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/templates.py src/keepwatch/cli.py tests/test_cli_init.py
git commit -m "Add keepwatch init"
```

---

### Task 4: `keepwatch install` and `uninstall` (systemd user service)

**Files:**
- Create: `src/keepwatch/systemd.py`
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_cli_install.py`

**Interfaces:**
- Produces:
  - `systemd.UNIT_NAME = "keepwatch.service"`; `unit_dir(paths) -> Path` (`$XDG_CONFIG_HOME/systemd/user`); `unit_text(executable: str, path_env: str) -> str`; `find_executable() -> str` (absolute path of the `keepwatch` command)
  - `systemctl(*args: str) -> subprocess.CompletedProcess[str]` — runs `[$KEEPWATCH_SYSTEMCTL or "systemctl", "--user", *args]`
  - `keepwatch install [--dry-run]` — writes the unit, `daemon-reload`, `enable --now keepwatch.service`; prints what it did and how to check; warns when `SSH_AUTH_SOCK` is set. `--dry-run` prints the unit and the commands without changing anything. Exit 1 if systemctl fails.
  - `keepwatch uninstall` — `disable --now` (failure ignored if the unit is not loaded), deletes the unit, `daemon-reload`; exit 0

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_install.py`

```python
from click.testing import CliRunner

from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def fake_systemctl(tmp_path, monkeypatch, exit_code=0):
    calls = tmp_path / "calls.txt"
    script = tmp_path / "systemctl"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho "fake failure" >&2\nexit {exit_code}\n'
                      if exit_code else f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("KEEPWATCH_SYSTEMCTL", str(script))
    return calls


def unit_file(xdg):
    return xdg.config_home.parent / "systemd" / "user" / "keepwatch.service"


def test_install_writes_the_unit_and_enables_it(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    result = run("install")
    assert result.exit_code == 0, result.output
    text = unit_file(xdg).read_text()
    assert "ExecStart=" in text and " run" in text
    assert 'Environment="PATH=' in text and "WantedBy=default.target" in text
    assert calls.read_text().splitlines() == ["--user daemon-reload", "--user enable --now keepwatch.service"]
    assert "systemctl --user status keepwatch" in result.output
    assert "SSH_AUTH_SOCK" not in result.output


def test_install_warns_about_ssh_agent(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-XXXX/agent.123")
    result = run("install")
    assert result.exit_code == 0
    assert "SSH_AUTH_SOCK" in result.output and "keepwatch docs environment" in result.output


def test_install_dry_run_changes_nothing(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    result = run("install", "--dry-run")
    assert result.exit_code == 0
    assert "[Service]" in result.output and "systemctl --user enable --now keepwatch.service" in result.output
    assert not unit_file(xdg).exists() and not calls.exists()


def test_install_reports_systemctl_failure(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch, exit_code=1)
    result = run("install")
    assert result.exit_code == 1
    assert "fake failure" in result.output


def test_uninstall(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    assert run("install").exit_code == 0
    result = run("uninstall")
    assert result.exit_code == 0, result.output
    assert not unit_file(xdg).exists()
    assert calls.read_text().splitlines()[-2:] == ["--user disable --now keepwatch.service", "--user daemon-reload"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_cli_install.py -q`
Expected: FAIL with `No such command 'install'`

- [ ] **Step 3: Implement** — `src/keepwatch/systemd.py`

```python
"""The systemd user service that starts keepwatch at login (spec section 15)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from keepwatch.paths import Paths

UNIT_NAME = "keepwatch.service"


def unit_dir(paths: Paths) -> Path:
    return paths.config_home.parent / "systemd" / "user"


def find_executable() -> str:
    """Absolute path of the keepwatch command that is running now."""
    found = shutil.which("keepwatch")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def unit_text(executable: str, path_env: str) -> str:
    return (
        "[Unit]\n"
        "Description=keepwatch: poll conditions and run actions\n"
        "Documentation=https://github.com/smprather/keepwatch\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'ExecStart="{executable}" run\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f'Environment="PATH={path_env}"\n'
        'Environment="PYTHONUNBUFFERED=1"\n'
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `systemctl --user ...` ($KEEPWATCH_SYSTEMCTL replaces systemctl, for tests)."""
    program = os.environ.get("KEEPWATCH_SYSTEMCTL", "systemctl")
    return subprocess.run([program, "--user", *args], capture_output=True, text=True, errors="replace")
```

In `src/keepwatch/cli.py`, add `from keepwatch import systemd` to the imports, and add before `def main()`:

```python
def _systemctl_or_fail(*args: str) -> None:
    try:
        completed = systemd.systemctl(*args)
    except OSError as exc:
        _fail(f"cannot run systemctl: {exc}")
    if completed.returncode != 0:
        _fail(f"systemctl --user {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print the unit file and the commands without changing anything.")
@click.pass_obj
def install(app: App, dry_run: bool) -> None:
    """Start keepwatch at login: install and start a systemd user service running `keepwatch run`.

    Writes $XDG_CONFIG_HOME/systemd/user/keepwatch.service with this keepwatch's absolute path and the
    current PATH (so hooks find the same programs as your shell), then runs `systemctl --user daemon-reload`
    and `systemctl --user enable --now keepwatch.service`. Re-running it updates the unit.

    The service does not see your shell's ssh-agent unless its socket is set in [environment] of the
    global config; see: keepwatch docs environment.

    Exit status: 0, or 1 if systemctl fails.
    """
    unit_path = systemd.unit_dir(app.paths) / systemd.UNIT_NAME
    text = systemd.unit_text(systemd.find_executable(), os.environ.get("PATH", ""))
    commands = ["systemctl --user daemon-reload", f"systemctl --user enable --now {systemd.UNIT_NAME}"]
    if dry_run:
        click.echo(f"would write {unit_path}:\n")
        click.echo(text)
        click.echo("would run:\n  " + "\n  ".join(commands))
        return
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text, encoding="utf-8")
    click.echo(f"wrote {unit_path}")
    _systemctl_or_fail("daemon-reload")
    _systemctl_or_fail("enable", "--now", systemd.UNIT_NAME)
    click.echo(f"ran: {'; '.join(commands)}")
    click.echo("check it with: systemctl --user status keepwatch   and   keepwatch status")
    if os.environ.get("SSH_AUTH_SOCK"):
        click.echo(
            "warning: SSH_AUTH_SOCK is set in this shell, but the service will not see that ssh-agent. "
            "Hooks that use ssh/scp need a key without a passphrase, or a stable agent socket set as "
            "SSH_AUTH_SOCK in [environment] of the global config; see: keepwatch docs environment"
        )


@cli.command()
@click.pass_obj
def uninstall(app: App) -> None:
    """Stop and remove the systemd user service installed by `keepwatch install`.

    Watches, state and logs are left alone.

    Exit status: 0, or 1 if systemctl daemon-reload fails.
    """
    unit_path = systemd.unit_dir(app.paths) / systemd.UNIT_NAME
    try:
        systemd.systemctl("disable", "--now", systemd.UNIT_NAME)
    except OSError as exc:
        _fail(f"cannot run systemctl: {exc}")
    if unit_path.exists():
        unit_path.unlink()
        click.echo(f"removed {unit_path}")
    else:
        click.echo(f"{unit_path} was not installed")
    _systemctl_or_fail("daemon-reload")
    click.echo("keepwatch will no longer start at login")
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/systemd.py src/keepwatch/cli.py tests/test_cli_install.py
git commit -m "Add keepwatch install and uninstall (systemd user service)"
```
