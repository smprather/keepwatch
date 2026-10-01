# keepwatch Plan 5c (Portable Tests, Windows Docs, Strict CI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the test suite pass on Windows (the 35 tests that still assume POSIX), document Windows behaviour in the reference, add a PowerShell template and example watch, and make Windows CI a required check.

**Architecture:** A small `tests/portable.py` builds test commands from the running Python and writes paths into TOML as literal strings. Tests of inherently POSIX behaviour are marked `posix_only`, with Windows counterparts where the behaviour differs. Product text changes only in the docs, templates and examples.

**Tech Stack:** as Plan 5a.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-windows-design.md` (sections 7, 8). Builds on Plans 5a and 5b (implemented). The failing Windows tests are listed in the supervisor's CI run of 2026-10-01 (run 36900922987).

## Global Constraints

- All Global Constraints of Plan 5a apply (branch `keepwatch-windows`, explicit `git add`, Linux suite green before every commit, do not push).
- **Run pytest without a pipe when you check the result before committing** (`uv run pytest -q`, then read the last line), or use `set -o pipefail`: a pipe into `tail` hides pytest's exit status.
- Changed tests must still pass on Linux; whether they pass on Windows is verified by the supervisor on CI.
- Product text (docs, templates, examples) must be copied exactly.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- `test_runner_command.py` must test the same behaviours as before, on both OSes. Task 1.
- Every topic still states only what the code does; the Windows statements match Plans 5a/5b. Task 3.

---

### Task 1: `tests/portable.py` and a portable `test_runner_command.py`

**Files:**
- Create: `tests/portable.py`
- Replace: `tests/test_runner_command.py`

- [ ] **Step 1: Create the helper** — `tests/portable.py`

```python
"""Test commands that behave the same on every OS: they run the current Python interpreter."""

import sys
import textwrap
from pathlib import Path

PY = Path(sys.executable).as_posix()


def literal(text: str) -> str:
    """A TOML literal string (no escape processing). `text` must not contain a single quote."""
    assert "'" not in text, text
    return f"'{text}'"


def toml_path(path) -> str:
    """A filesystem path as a TOML literal string; forward slashes work on Windows too."""
    return literal(Path(path).as_posix())


def py(code: str) -> str:
    """A TOML list hook running `code` with the current Python. Use double quotes inside `code`."""
    return f"[{literal(PY)}, '-c', {literal(code)}]"


def exits(code: int) -> str:
    return py(f"import sys; sys.exit({code})")


TRUE = exits(0)


def script(body: str) -> str:
    """The text of a .py hook file: runs directly on POSIX (shebang) and through Python on Windows."""
    return f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip("\n")
```

- [ ] **Step 2: Replace `tests/test_runner_command.py`** with:

```python
import json
import os
import threading
import time
from pathlib import Path

import pytest
from portable import exits, py, script

from keepwatch.platform import IS_WINDOWS
from keepwatch.runner import Runner

ACT_PY = '''
    import json, os, pathlib
    env = os.environ
    seen = {
        "condition": env["KEEPWATCH_CONDITION"],
        "hook": env["KEEPWATCH_HOOK"],
        "dest": env["KEEPWATCH_SETTING_DEST"],
        "extra": env["EXTRA"],
        "global": env["GLOBAL_ONLY"],
        "payload": json.loads(pathlib.Path(env["KEEPWATCH_PAYLOAD_FILE"]).read_text()),
        "settings": json.loads(pathlib.Path(env["KEEPWATCH_SETTINGS_FILE"]).read_text()),
        "cwd": os.getcwd(),
    }
    pathlib.Path(env["KEEPWATCH_RUN_DIR"], "seen.json").write_text(json.dumps(seen))
'''

SLEEP = py("import time; time.sleep(30)")


def test_exit_codes_map_to_outcomes(make_watch, call_for):
    check = py('import os, sys; sys.exit(int(os.environ["CODE"]))')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n[check_exit_codes]\nunknown = [3]\n")
    for code, status in ((0, "true"), (1, "false"), (3, "unknown"), (7, "error")):
        result = Runner().run(call_for(watch_dir, environment={"CODE": str(code)}))
        assert (result.status, result.exit_code, result.kind) == (status, code, "command")
    assert "exit code 7 is not listed in [check_exit_codes]" in result.reason


def test_string_check_runs_in_the_platform_shell(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "exit 3"\n[check_exit_codes]\nunknown = [3]\n')
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_check_passes_a_payload(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.py"]\n', files={"check.py": script('''
        import os, pathlib
        pathlib.Path(os.environ["KEEPWATCH_PAYLOAD_OUT"]).write_text('["a.tar.gz", "b.tar.gz"]')
    ''')})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.payload == ["a.tar.gz", "b.tar.gz"]
    assert result.target == "./check.py"


def test_invalid_payload_json_is_an_error(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.py"]\n', files={"check.py": script('''
        import os, pathlib
        pathlib.Path(os.environ["KEEPWATCH_PAYLOAD_OUT"]).write_text("not json")
    ''')})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON")


def test_action_sees_environment_payload_and_settings(xdg, make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        on_true = ["./act.py"]
        [environment]
        EXTRA = "watch-level"
        [settings]
        dest = "foo@bar:/in/"
    ''', files={"act.py": script(ACT_PY)})
    result = Runner().run(call_for(watch_dir, hook="on_true", condition=True, payload=["a.tar.gz"],
                                   environment={"GLOBAL_ONLY": "yes"}))
    assert result.status == "ok", (result.reason, result.stderr)
    run_dir = xdg.run_dir(os.getpid(), "cmd")
    seen = json.loads((run_dir / "seen.json").read_text())
    assert Path(seen.pop("cwd")) == watch_dir
    assert seen == {
        "condition": "true",
        "hook": "on_true",
        "dest": "foo@bar:/in/",
        "extra": "watch-level",
        "global": "yes",
        "payload": ["a.tar.gz"],
        "settings": {"dest": "foo@bar:/in/"},
    }
    assert sorted(p.name for p in run_dir.iterdir()) == ["seen.json"]


def test_failed_action(make_watch, call_for):
    action = py('import sys; sys.stderr.write("oops"); sys.exit(4)')
    watch_dir = make_watch("cmd", config=f"[hooks]\non_false = {action}\n")
    result = Runner().run(call_for(watch_dir, hook="on_false"))
    assert (result.status, result.exit_code, result.stderr) == ("failed", 4, "oops")
    assert result.reason == "exit code 4"


def test_missing_executable(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = ["./nope.exe"]\n')
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason.startswith("cannot start command")


@pytest.mark.posix_only
def test_killed_by_signal(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "kill -TERM $$"\n')
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.signal, result.reason) == ("error", 15, "killed by signal SIGTERM")


def test_timeout(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.0))
    assert result.status == "timeout" and result.duration < 10


def test_background_child_holding_stdout_does_not_hang(make_watch, call_for):
    check = py(
        'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
        'print("started")'
    )
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout.strip() == "started"
    assert result.duration < 10


def test_binary_output_is_decoded_with_replacement(make_watch, call_for):
    check = py('import sys; sys.stdout.buffer.write(bytes([255]) + b"ok")')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "\ufffdok"


def test_output_is_clipped(make_watch, call_for):
    check = py('import sys; sys.stdout.write("x" * 10000)')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir, capture_bytes=1000))
    assert result.stdout_truncated is True
    assert "bytes omitted" in result.stdout
    assert len(result.stdout) < 1200


def test_unwritable_run_dir_is_a_failed_hook(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\non_true = {exits(0)}\n")
    call = call_for(watch_dir, hook="on_true")
    call.run_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    call.run_dir.parent.write_text("not a directory")
    result = Runner().run(call)
    assert result.status == "failed"
    assert result.reason.startswith("cannot prepare the hook's files in")


def test_terminate_all_stops_running_hooks(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    runner = Runner()
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(1.0)
    runner.terminate_all()
    thread.join(10)
    assert results and results[0].status == "error"
    if IS_WINDOWS:
        assert results[0].reason == "terminated by keepwatch"
    else:
        assert results[0].signal == 15


@pytest.mark.posix_only
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


@pytest.mark.windows_only
def test_close_terminates_at_once_and_refuses_new_hooks_on_windows(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    runner = Runner(kill_grace=30)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(1.0)
    runner.close()
    thread.join(10)
    assert not thread.is_alive()
    assert results[0].reason == "terminated by keepwatch"
    refused = runner.run(call_for(watch_dir))
    assert (refused.status, refused.reason) == ("error", "keepwatch is shutting down")
```

- [ ] **Step 3: Run the suite and ruff** — `uv run pytest -q` → PASS on Linux (`uv run ruff check --fix tests` only if it reports I001)

- [ ] **Step 4: Commit**

```bash
git add tests/portable.py tests/test_runner_command.py
git commit -m "Portable test commands; test_runner_command runs on every OS"
```

---

### Task 2: The other tests that assumed POSIX

**Files:**
- Modify: `tests/conftest.py`, `tests/test_alerts.py`, `tests/test_cli_new.py`, `tests/test_cli_poll.py`, `tests/test_cli_validate.py`, `tests/test_config_watch.py`, `tests/test_ctx.py`, `tests/test_examples.py`, `tests/test_pollengine.py`, `tests/test_protocol.py`, `tests/test_runner_python.py`, `tests/test_scheduler.py`, `tests/test_worker.py`

Make exactly these changes (add imports at the top of each file as needed; `from portable import …` sorts with the first-party imports):

- [ ] **Step 1: `tests/conftest.py`** — in the `xdg` fixture, after `monkeypatch.setenv("HOME", ...)`, add `monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))` (Windows expands `~` from `USERPROFILE`, not `HOME`).

- [ ] **Step 2: `tests/test_alerts.py`** — add `import sys`; replace `test_alert_receives_environment` and `test_failing_and_slow_alerts_are_recorded_not_raised` with:

```python
def test_alert_receives_environment(tmp_path):
    out = tmp_path / "alert.txt"
    records = []
    code = (
        'import os, pathlib; e = os.environ; pathlib.Path(e["OUT"]).write_text('
        '"|".join([e["KEEPWATCH_ALERT_EVENT"], e["KEEPWATCH_WATCH"], e["KEEPWATCH_ALERT_REASON"]]))'
    )
    run_alert(Command(argv=(sys.executable, "-c", code)), event="offline", watch="psg", reason="5 failures",
              environment={"OUT": str(out)}, timeout=10, sink=records.append)
    assert out.read_text() == "offline|psg|5 failures"
    (record,) = records
    assert (record["event"], record["status"], record["alert_event"], record["level"]) == ("alert.end", "ok", "offline", "INFO")


def test_alert_string_runs_in_the_platform_shell():
    records = []
    run_alert(Command(shell="exit 3"), event="online", watch="w", reason="r", environment={}, timeout=10,
              sink=records.append)
    assert (records[0]["status"], records[0]["exit_code"]) == ("failed", 3)


def test_failing_and_slow_alerts_are_recorded_not_raised(tmp_path):
    records = []
    failing = (sys.executable, "-c", 'import sys; sys.stderr.write("bad"); sys.exit(3)')
    run_alert(Command(argv=failing), event="online", watch="w", reason="r", environment={},
              timeout=10, sink=records.append)
    run_alert(Command(argv=(sys.executable, "-c", "import time; time.sleep(5)")), event="online", watch="w",
              reason="r", environment={}, timeout=0.5, sink=records.append)
    run_alert(Command(argv=("/no/such/program",)), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    assert [(r["status"], r["level"]) for r in records] == [("failed", "WARNING"), ("timeout", "WARNING"), ("failed", "WARNING")]
    assert records[0]["stderr"] == "bad" and records[0]["exit_code"] == 3
    assert records[2]["reason"].startswith("cannot start alert_command")
```

- [ ] **Step 3: `tests/test_cli_new.py`** — add `import pytest`; mark `test_shell_template_validates_and_scripts_are_executable` with `@pytest.mark.posix_only`.

- [ ] **Step 4: `tests/test_cli_poll.py`** — add `from portable import toml_path`; in `test_config_option_selects_watch_dirs` replace the `config.write_text(...)` line with `config.write_text(f"watch_dirs = [{toml_path(other)}]\n")`.

- [ ] **Step 5: `tests/test_cli_validate.py`** — add `from portable import TRUE, toml_path`; in `test_named_duplicate_is_reported` replace the two `make_watch` lines and the `config.write_text` line with:

```python
    make_watch("backup", config=f"[hooks]\ncheck = {TRUE}\n")
    make_watch("backup", config=f"[hooks]\ncheck = {TRUE}\n", base=other)
    config = tmp_path / "custom.toml"
    config.write_text(f"watch_dirs = [{toml_path(xdg.default_watches_dir)}, {toml_path(other)}]\n")
```

- [ ] **Step 6: `tests/test_config_watch.py`** — add `from keepwatch import platform`; replace `assert cfg.hooks["check"].to_argv() == ["/bin/sh", "-c", "./check.sh"]` with `assert cfg.hooks["check"].to_argv() == [*platform.default_shell(), "./check.sh"]`.

- [ ] **Step 7: `tests/test_ctx.py`** — in `test_glob_expands_home_and_sorts`, add `monkeypatch.setenv("USERPROFILE", str(tmp_path))` after the `HOME` line. Replace the body of `test_run_raises_on_failure_with_stderr_tail` with:

```python
    ctx = make_ctx(tmp_path)
    with pytest.raises(CommandFailed, match="exited 3.*nope"):
        ctx.run([sys.executable, "-c", 'import sys; sys.stderr.write("nope"); sys.exit(3)'])
    assert ctx.run("exit 3", check=False).returncode == 3
```

- [ ] **Step 8: `tests/test_examples.py`** — add `import pytest` if missing; replace `test_examples_validate_and_poll` with:

```python
def test_psg_export_example_validates_and_polls(xdg):
    install(xdg, "psg-export")
    assert run("validate", "psg-export").exit_code == 0
    data = json.loads(run("poll", "psg-export", "--dry-run", "--json").output)
    assert data["polls"][0]["outcome"] == "false"


@pytest.mark.posix_only
def test_site_down_example_validates_and_polls(xdg):
    install(xdg, "site-down")
    assert run("validate", "site-down").exit_code == 0
    data = json.loads(run("poll", "site-down", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]
```

- [ ] **Step 9: `tests/test_pollengine.py`** — add `from portable import toml_path`. In `PSG_WATCH`, add `import sys` at the top of the embedded watch.py text and replace `ctx.run(["cp", f, ctx.settings["dest"]])` with `ctx.run([sys.executable, "-c", "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])", f, ctx.settings["dest"]])`. In `test_psg_style_watch_sends_once`, replace the two settings lines of the config with:

```python
        pattern = {toml_path(incoming / "psg-export-*.tar.gz")}
        dest = {toml_path(dest)}
```

- [ ] **Step 10: `tests/test_protocol.py`** — in `test_normalize_payload_converts_paths_and_tuples`, replace the expected value with `{"files": [str(Path("/in/a.tar.gz")), "b"]}`.

- [ ] **Step 11: `tests/test_runner_python.py`** — in `test_check_answers_with_payload_logs_and_output`, replace `assert result.stdout == "hello from check\n"` with `assert result.stdout.splitlines() == ["hello from check"]`.

- [ ] **Step 12: `tests/test_scheduler.py`** — add `from portable import py`. Replace both `iso_time(1.0)` with `iso_time(1_000_000.0)` (Windows cannot convert timestamps from early 1970 to local time). Define near the top:

```python
OK_FILE_CHECK = py('import os, sys; sys.exit(0 if os.path.exists("ok") else 9)')
```

and replace each config string containing `check = "test -f ok || exit 9"` with an f-string using `check = {OK_FILE_CHECK}`, e.g. `f'max_failures = 1\nretry_after = "1h"\n[hooks]\ncheck = {OK_FILE_CHECK}\n'`.

- [ ] **Step 13: `tests/test_worker.py`** — add `from keepwatch import platform`. Replace `run_worker` with:

```python
def run_worker(request):
    req_r, req_w = os.pipe()
    res_r, res_w = os.pipe()
    process = platform.start_process(
        [sys.executable, "-m", "keepwatch.worker", *platform.pipe_arguments(req_r, res_w)],
        cwd=None, env=None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        inherit=(req_r, res_w),
    )
    os.close(req_r)
    os.close(res_w)
    with os.fdopen(req_w, "wb") as handle:
        handle.write(json.dumps(request).encode())
    with os.fdopen(res_r, "rb") as handle:
        data = handle.read()
    out, err = process.popen.communicate(timeout=30)
    process.close()
    messages, bad = decode_lines(data)
    assert bad == []
    return messages, out.decode(), err.decode()
```

In the `test_interpret_check_return` parameters replace `"payload": ["/a"]` with `"payload": [str(Path("/a"))]`. In `test_check_with_payload_and_logs` replace `assert out == "to stdout\n"` with `assert out.splitlines() == ["to stdout"]`.

- [ ] **Step 14: Run the suite and ruff** — `uv run pytest -q` → PASS on Linux

- [ ] **Step 15: Commit**

```bash
git add tests/conftest.py tests/test_alerts.py tests/test_cli_new.py tests/test_cli_poll.py tests/test_cli_validate.py tests/test_config_watch.py tests/test_ctx.py tests/test_examples.py tests/test_pollengine.py tests/test_protocol.py tests/test_runner_python.py tests/test_scheduler.py tests/test_worker.py
git commit -m "Make the remaining tests portable to Windows"
```

---

### Task 3: Windows in the reference, a PowerShell template and example

**Files:**
- Modify: `src/keepwatch/reference/{agent,executables,environment,storage,failures,config,overview,examples}.md`, `src/keepwatch/reference.py`, `src/keepwatch/templates.py`, `src/keepwatch/cli.py`
- Create: `src/keepwatch/examples/disk-space-ps/{config.toml,low_space.ps1,notify.ps1}`
- Test: `tests/test_docs.py`, `tests/test_cli_new.py`, `tests/test_examples.py`

- [ ] **Step 1: Write the failing tests.** In `tests/test_docs.py`, add to `KEY_FACTS`: `"executables"` gets `"Windows PowerShell"` and `".ps1"` appended; `"environment"` gets `"Job Object"` and `"keepwatch stop"` appended; `"storage"` gets `"%LOCALAPPDATA%"` appended; and add an entry `"config": ["single-quoted"],`. In `tests/test_cli_new.py` append:

```python
def test_powershell_template_files(xdg):
    assert run("new", "ps", "--template", "powershell").exit_code == 0
    watch_dir = xdg.default_watches_dir / "ps"
    assert sorted(p.name for p in watch_dir.iterdir()) == ["check.ps1", "config.toml", "on_true.ps1"]


@pytest.mark.windows_only
def test_powershell_template_validates_and_polls(xdg):
    assert run("new", "ps", "--template", "powershell").exit_code == 0
    assert run("validate", "ps").exit_code == 0
    data = json.loads(run("poll", "ps", "--fake", "true", "--json").output)
    assert data["polls"][0]["results"] == [{"hook": "on_true", "status": "ok", "reason": None}]
```

In `tests/test_examples.py`, change the expected list in `test_examples_are_in_the_docs` to `["disk-space-ps", "greet-once", "psg-export", "site-down"]` and append:

```python
@pytest.mark.windows_only
def test_disk_space_example_runs_on_windows(xdg):
    install(xdg, "disk-space-ps")
    assert run("validate", "disk-space-ps").exit_code == 0
    data = json.loads(run("poll", "disk-space-ps", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]
    real = json.loads(run("poll", "disk-space-ps", "--json").output)["polls"][0]
    assert real["outcome"] in ("true", "false")
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_docs.py tests/test_cli_new.py tests/test_examples.py -q` → FAIL (missing facts; unknown template; missing example)

- [ ] **Step 3: Write the product text.**

`src/keepwatch/reference/agent.md` — in "The contract", replace item 7 with:

```markdown
7. **Every hook runs in a fresh process** with no standard input, the watch directory as working directory, and a deadline (`check_timeout`, `action_timeout`, default 60s). Nothing may prompt for input. See `keepwatch docs environment`.
```

and add an item after item 8:

```markdown
9. **On Windows,** string hooks run in Windows PowerShell; list hooks ending in `.ps1`, `.py`, `.cmd` or `.bat` run with their interpreter; `sh` scripts and Expect need extra tools. Start from `keepwatch new <name> --template powershell` or the default Python template.
```

In "Common mistakes", add as the last bullet:

```markdown
- Writing Windows paths in double-quoted TOML strings: `\` starts an escape there. Use single-quoted strings (`'C:\data\in'`) or forward slashes (`"C:/data/in"`).
```

`src/keepwatch/reference/executables.md` — replace the paragraph that starts "A hook defined both in `[hooks]`" with:

````markdown
A hook defined both in `[hooks]` and in watch.py is an error.

## Which program runs

- **A string** runs through the platform shell: `/bin/sh -c` on Linux and macOS, **Windows PowerShell** (`powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command`) on Windows. The watch key `shell` (or `[defaults]` in the global config) replaces it, e.g. `shell = ["pwsh", "-NoProfile", "-Command"]` or `shell = ["cmd.exe", "/d", "/c"]`. `exit N` sets the exit code in both default shells.
- **A list** runs directly. A program named with a path separator (`./notify.sh`, `.\notify.ps1`) is relative to the watch directory; a bare name (`expect`) is looked up on PATH. On Linux and macOS the file must be executable.
- **On Windows**, list hooks are started by file type: `.ps1` with Windows PowerShell (`-File`), `.py` with keepwatch's own Python, `.cmd`/`.bat` with `cmd.exe /d /c`; `.exe` files and other programs run directly.

`keepwatch validate` checks list-form programs; strings are only checked when they run.
````

and replace the sentence "- Working directory: the watch directory. stdin: `/dev/null`." with "- Working directory: the watch directory. Standard input: none (`/dev/null`, or `NUL` on Windows)."

`src/keepwatch/reference/environment.md` — append:

````markdown
## On Windows

- Every hook runs in its own **Job Object**, started suspended and assigned to the job before it runs, so nothing it starts can escape. A timeout, `keepwatch stop`, or the service ending terminates the whole job **at once**: Windows has no graceful stop signal for windowless processes, so hooks must not rely on cleanup when they are terminated. Hooks also die if the service process itself is killed.
- String hooks run in Windows PowerShell (see `keepwatch docs executables`).
- `keepwatch install` registers a Task Scheduler task "keepwatch" that runs `pythonw.exe -m keepwatch run` at logon (no console window, restarted on failure); if policy forbids user tasks, it creates a Startup-folder shortcut instead. The service runs with your normal user environment, so there is no PATH capture.
- Stop the service with **`keepwatch stop`** (it also works on Linux). Without a console, the service writes only to its log file.
- ssh and scp: the Windows OpenSSH `ssh-agent` service is shared by all your sessions, so keys added with `ssh-add` work for hooks too. For a password, see the askpass support planned with the transfer tools.
````

`src/keepwatch/reference/storage.md` — after the layout table's paragraph that begins "`XDG_CONFIG_HOME` defaults to", add:

```markdown
On Windows the defaults are `%APPDATA%\keepwatch` for configuration and watches, `%LOCALAPPDATA%\keepwatch` for state and logs, and `%LOCALAPPDATA%\keepwatch\run` for run-only data. Setting the XDG variables overrides them on Windows too.
```

`src/keepwatch/reference/failures.md` — in the alert_command section, replace "A string runs through `/bin/sh -c`; a list runs directly." with "A string runs through the platform shell (`/bin/sh -c`, or Windows PowerShell); a list runs directly."

`src/keepwatch/reference/config.md` — append this bullet to the "General rules" list:

```markdown
- **Windows paths** in TOML: `\` starts an escape inside double quotes, so write paths in single-quoted literal strings (`'C:\Users\me\in'`) or with forward slashes (`"C:/Users/me/in"`).
```

`src/keepwatch/reference/overview.md` — in the Commands table, change the Run row to `| Run | \`run\`, \`stop\` |`.

`src/keepwatch/reference/examples.md` — append the bullet:

```markdown
- **disk-space-ps** (PowerShell, Windows): warn when a drive runs low on space. Shows `.ps1` hooks, settings as environment variables, and exit codes from PowerShell.
```

In `src/keepwatch/reference.py`, add `".ps1": "powershell"` to `_LANGUAGES`.

`src/keepwatch/templates.py` — add before `TEMPLATES`:

```python
POWERSHELL_CONFIG = '''# keepwatch watch "{name}" (PowerShell). Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"

[hooks]
# .ps1 files run with Windows PowerShell; see: keepwatch docs executables
check = ["./check.ps1"]
on_true = ["./on_true.ps1"]

[check_exit_codes]
# Defaults: true = [0], false = [1]; other codes are errors unless listed here.
unknown = [3]

[settings]
# Reaches the scripts as $env:KEEPWATCH_SETTING_EXAMPLE
example = "value"
'''

POWERSHELL_CHECK = '''# Check for watch "{name}": exit 0 = TRUE, 1 = FALSE, 3 = unknown (see config.toml).
# To hand data to the actions, write JSON to the file named by $env:KEEPWATCH_PAYLOAD_OUT.
# Never change anything here: keepwatch poll --dry-run runs this for real.
exit 1
'''

POWERSHELL_ACTION = '''# Runs on every poll while the condition of "{name}" is TRUE. Exit nonzero (or throw) to report a failure.
# The check's payload (if any) is in the JSON file named by $env:KEEPWATCH_PAYLOAD_FILE.
$ErrorActionPreference = 'Stop'
Write-Output "condition is TRUE; example setting: $env:KEEPWATCH_SETTING_EXAMPLE"
'''
```

and add `"powershell": {"config.toml": POWERSHELL_CONFIG, "check.ps1": POWERSHELL_CHECK, "on_true.ps1": POWERSHELL_ACTION},` to `TEMPLATES`. In `src/keepwatch/cli.py`, extend the `--template` option's help text with ` powershell: check.ps1 and on_true.ps1 (Windows).`

Create the example `src/keepwatch/examples/disk-space-ps/config.toml`:

```toml
# Warn when a drive runs low on space. TRUE means "low".
description = "Tell me when drive C: has less than 10 GB free"
interval = "5m"

[hooks]
check = ["./low_space.ps1"]
on_rise = ["./notify.ps1", "is low on space"]
on_fall = ["./notify.ps1", "has enough space again"]

[check_exit_codes]
true = [0]
false = [1]

[settings]
drive = "C"
min_free_gb = 10
```

`src/keepwatch/examples/disk-space-ps/low_space.ps1`:

```powershell
# TRUE (exit 0) when the drive has less free space than min_free_gb, FALSE (exit 1) otherwise.
# Any other failure exits 2, which keepwatch reports as an error.
try {
    $drive = Get-PSDrive -Name $env:KEEPWATCH_SETTING_DRIVE -PSProvider FileSystem -ErrorAction Stop
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Write-Output "free: $freeGb GB"
    if ($freeGb -lt [double]$env:KEEPWATCH_SETTING_MIN_FREE_GB) { exit 0 } else { exit 1 }
} catch {
    Write-Error $_
    exit 2
}
```

`src/keepwatch/examples/disk-space-ps/notify.ps1`:

```powershell
param([string]$What)
# Report a change; the output goes to the keepwatch log. Replace it with a toast or an email as you like.
Write-Output ("drive {0}: {1} (watch {2}, poll {3})" -f $env:KEEPWATCH_SETTING_DRIVE, $What, $env:KEEPWATCH_WATCH, $env:KEEPWATCH_POLL_ID)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS on Linux

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/reference src/keepwatch/reference.py src/keepwatch/templates.py src/keepwatch/cli.py src/keepwatch/examples/disk-space-ps tests/test_docs.py tests/test_cli_new.py tests/test_examples.py
git commit -m "Document Windows; add a PowerShell template and example"
```

---

### Task 4: Windows CI becomes required

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Implement.** Delete these two lines from `ci.yml`:

```yaml
    # Windows is informational until the port is complete (plan 5c removes this line).
    continue-on-error: ${{ matrix.os == 'windows-latest' }}
```

- [ ] **Step 2: Run the suite** — `uv run pytest -q` → PASS

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "CI: Windows results are now required"
```
