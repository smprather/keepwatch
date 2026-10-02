# keepwatch Plan 6e (Relay, Install Guards) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the observers/transfers/relay feature set for release 2026.10.3: `keepwatch install` refuses interpreters that would break the login service later, the relay ships as two tested example watches with a `relay` docs topic, and an end-to-end test moves a file linux1 → staging → linux2 through the running service.

**Architecture:** A new `keepwatch/installcheck.py` (transient-environment detection, a `--version` probe of the exact command the login service will run) used by `install` before it changes anything (`--force` skips only the transient check). Two example directories use the `pull` and `push` recipes. The end-to-end test runs `Service` with both watches against two in-process scp servers, one without a chroot standing in for linux1.

**Tech Stack:** as Plan 6d.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (sections 6 and 9). Builds on Plans 6a-6d (implemented).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests unless marked, stop and report on plan defects, attribution trailer).
- `keepwatch install` must never search PATH, `py` or `uv python find` for an interpreter: it registers the interpreter that runs it, and only checks it.

## Review Focus

- `uvx keepwatch install` must refuse with the fix (`uv tool install keepwatch`), not register a cache path. Test: Task 1 `test_install_refuses_a_transient_environment`.
- A service command that does not report this keepwatch's version (a broken venv, the Store placeholder exiting 9009) must stop `install` before anything is registered. Test: Task 1 `test_install_refuses_a_broken_service_command`.
- The whole relay must work through the running service, with a password on the destination. Test: Task 3 `test_relay_moves_a_file_from_linux1_to_linux2`.
- With the destination unknown, the example push watch must answer unknown, not fail. Test: Task 2 `test_relay_push_example_waits_when_linux2_is_away`.

---

### Task 1: Install guards

**Files:**
- Create: `src/keepwatch/installcheck.py`
- Modify: `src/keepwatch/cli.py` (`install`: `--force`, the checks)
- Test: `tests/test_installcheck.py` (new)

**Interfaces:**
- Produces (`keepwatch.installcheck`):
  - `uv_cache_dirs(env: Mapping[str, str]) -> list[Path]` — `$UV_CACHE_DIR`, `uv cache dir` (when uv is on PATH), and the default location (`%LOCALAPPDATA%\uv\cache` on Windows, `$XDG_CACHE_HOME/uv` or `~/.cache/uv` elsewhere)
  - `transient_reason(prefix: Path, *, cache_dirs: Sequence[Path], temp_dir: Path) -> str | None`
  - `service_check_argv() -> list[str]` — Windows: the console `python.exe` beside `winsched.pythonw()` with `-m keepwatch --version`; elsewhere `[systemd.find_executable(), "--version"]`
  - `verify_command(argv: Sequence[str], env: Mapping[str, str]) -> str | None`
- `keepwatch install --force`

- [ ] **Step 1: Write the failing tests** — `tests/test_installcheck.py`:

```python
import sys
from pathlib import Path

from click.testing import CliRunner

from keepwatch import __version__, installcheck
from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_transient_reason(tmp_path):
    cache, temp, home = tmp_path / "cache", tmp_path / "temp", tmp_path / "home"
    for directory in (cache / "archive-v0" / "x", temp / "env", home / ".local" / "share" / "uv" / "tools" / "keepwatch"):
        directory.mkdir(parents=True)
    kwargs = {"cache_dirs": [cache], "temp_dir": temp}
    assert "uv's cache" in installcheck.transient_reason(cache / "archive-v0" / "x", **kwargs)
    assert "temporary directory" in installcheck.transient_reason(temp / "env", **kwargs)
    assert installcheck.transient_reason(home / ".local" / "share" / "uv" / "tools" / "keepwatch", **kwargs) is None


def test_uv_cache_dirs_include_the_variable(tmp_path):
    dirs = installcheck.uv_cache_dirs({"UV_CACHE_DIR": str(tmp_path / "c"), "PATH": "", "HOME": str(tmp_path)})
    assert tmp_path / "c" in dirs


def test_verify_command():
    good = [sys.executable, "-c", f"print('keepwatch, version {__version__}')"]
    assert installcheck.verify_command(good, {**__import__("os").environ}) is None
    wrong = [sys.executable, "-c", "print('keepwatch, version 1999.1.1')"]
    assert "without reporting keepwatch" in installcheck.verify_command(wrong, {**__import__("os").environ})
    store = [sys.executable, "-c", "import sys; print('Python was not found; run without arguments to install from the Microsoft Store'); sys.exit(9009)"]
    problem = installcheck.verify_command(store, {**__import__("os").environ})
    code = 9009 if os.name == "nt" else 9009 & 0xFF  # POSIX exit statuses are 8-bit
    assert f"exited {code}" in problem and "Microsoft Store" in problem
    assert "cannot run" in installcheck.verify_command([str(Path("/no/such/program"))], {})


def test_the_service_command_reports_this_version():
    import os

    assert installcheck.verify_command(installcheck.service_check_argv(), os.environ) is None


def test_install_refuses_a_transient_environment(xdg, monkeypatch):
    monkeypatch.setattr(installcheck, "transient_reason", lambda prefix, **kwargs: "this keepwatch runs from uv's cache (x)")
    result = run("install", "--dry-run")
    assert result.exit_code == 1
    assert "uv tool install keepwatch" in result.output and "--force" in result.output
    assert run("install", "--dry-run", "--force").exit_code == 0


def test_install_refuses_a_broken_service_command(xdg, monkeypatch):
    monkeypatch.setattr(installcheck, "transient_reason", lambda prefix, **kwargs: None)
    monkeypatch.setattr(installcheck, "verify_command", lambda argv, env: "python.exe exited 9009 …")
    result = run("install", "--dry-run", "--force")
    assert result.exit_code == 1
    assert "the login service would run" in result.output and "exited 9009" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_installcheck.py -q`
Expected: collection error, `cannot import name 'installcheck'`.

- [ ] **Step 3: Implement** — create `src/keepwatch/installcheck.py`:

```python
"""Checks `keepwatch install` makes before it registers this interpreter to start at login.

keepwatch never searches for a Python (PATH, `py`, `uv python find` can find the Microsoft Store placeholder or
an interpreter without keepwatch): it registers the one running it, and these checks make sure that one lasts
and works.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from keepwatch import __version__, platform, systemd, winsched


def uv_cache_dirs(env: Mapping[str, str]) -> list[Path]:
    """Where uv keeps its cache (uvx and `uv run --with` environments live there and get pruned)."""
    dirs = []
    if env.get("UV_CACHE_DIR"):
        dirs.append(Path(env["UV_CACHE_DIR"]))
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv:
        try:
            completed = subprocess.run(
                [uv, "cache", "dir"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                creationflags=platform.NO_WINDOW,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                dirs.append(Path(completed.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if platform.IS_WINDOWS:
        if env.get("LOCALAPPDATA"):
            dirs.append(Path(env["LOCALAPPDATA"]) / "uv" / "cache")
    else:
        cache_home = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path(env.get("HOME", "~")).expanduser() / ".cache"
        dirs.append(cache_home / "uv")
    return dirs


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        return False


def transient_reason(prefix: Path, *, cache_dirs: Sequence[Path], temp_dir: Path) -> str | None:
    """Why the environment at `prefix` (sys.prefix) will not last, or None."""
    for cache in cache_dirs:
        if _inside(prefix, cache):
            return f"this keepwatch runs from uv's cache ({prefix}): uvx and `uv run --with` environments are pruned later"
    if _inside(prefix, temp_dir):
        return f"this keepwatch runs from the temporary directory ({prefix})"
    return None


def service_check_argv() -> list[str]:
    """The command the login service will run, asked for its version."""
    if platform.IS_WINDOWS:
        python = Path(winsched.pythonw())
        console = python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python
        return [str(console), "-m", "keepwatch", "--version"]
    return [systemd.find_executable(), "--version"]


def verify_command(argv: Sequence[str], env: Mapping[str, str]) -> str | None:
    """Run argv and require it to report this keepwatch's version. Returns the problem, or None."""
    shown = " ".join(argv)
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            env=dict(env) or None,
            creationflags=platform.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot run {shown}: {exc}"
    if completed.returncode == 0 and __version__ in completed.stdout:
        return None
    output = " | ".join((completed.stdout + completed.stderr).strip().splitlines()[-3:]) or "no output"
    return f"{shown} exited {completed.returncode} without reporting keepwatch {__version__}: {output}"
```

In `src/keepwatch/cli.py`:

(a) Add `installcheck` to the `from keepwatch import …` line (`from keepwatch import __version__, installcheck, platform, systemd, transfer, winsched`) and `import tempfile` to the stdlib imports.

(b) Add before the `install` command:

```python
def _install_problem(force: bool) -> str | None:
    """Why the login service would break, or None (see keepwatch.installcheck)."""
    if not force:
        reason = installcheck.transient_reason(
            Path(sys.prefix), cache_dirs=installcheck.uv_cache_dirs(os.environ), temp_dir=Path(tempfile.gettempdir())
        )
        if reason is not None:
            return (
                f"{reason}, and the login service would stop working when it disappears. Install keepwatch for good "
                "with `uv tool install keepwatch` and run `keepwatch install` from there (or pass --force)."
            )
    problem = installcheck.verify_command(installcheck.service_check_argv(), os.environ)
    if problem is not None:
        return f"the login service would run a command that does not work: {problem}"
    return None
```

(c) Change the `install` command: add the option

```python
@click.option(
    "--force",
    is_flag=True,
    help="Install even when this keepwatch runs from a temporary environment (uv's cache, uvx, the temp directory).",
)
```

after `--dry-run`, add `force: bool` to its parameters, append this paragraph to its docstring before the "Exit status" line:

```
    Before changing anything it checks the command the service will run: it must not live in a temporary
    environment (uv's cache, where uvx puts it, or the temp directory; --force skips this check) and it must
    report this keepwatch's version. keepwatch never looks for another Python on PATH.
```

change the "Exit status" line to `Exit status: 0, or 1 if a check fails or systemctl (Task Scheduler) fails.`, and make the first lines of its body:

```python
    problem = _install_problem(force)
    if problem is not None:
        _fail(problem)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_installcheck.py tests/test_cli_install.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/installcheck.py src/keepwatch/cli.py tests/test_installcheck.py
git commit -m "install refuses transient environments and service commands that do not work"
```

---

### Task 2: The relay examples

**Files:**
- Create: `src/keepwatch/examples/relay-pull/config.toml`, `src/keepwatch/examples/relay-push/config.toml`
- Modify: `tests/test_examples.py`

- [ ] **Step 1: Write the failing tests** — in `tests/test_examples.py`, change the expected list in `test_examples_are_in_the_docs` to

```python
    assert [d.name for d in example_dirs()] == ["disk-space-ps", "greet-once", "psg-export", "relay-pull", "relay-push", "site-down"]
```

and append:

```python
needs_openssh = pytest.mark.skipif(shutil.which("scp") is None or shutil.which("ssh") is None, reason="needs OpenSSH")


@needs_openssh
def test_relay_examples_validate(xdg):
    install(xdg, "relay-pull")
    install(xdg, "relay-push")
    result = run("validate", "relay-pull", "relay-push")
    assert result.exit_code == 0, result.output


@needs_openssh
def test_relay_push_example_waits_when_linux2_is_away(xdg):
    install(xdg, "relay-push")
    staging = xdg.default_watches_dir / "relay-push" / "staging"
    staging.mkdir(parents=True)
    data = json.loads(run("poll", "relay-push", "--json").output)
    poll = data["polls"][0]
    assert poll["outcome"] == "unknown" and poll["failed"] is False
    assert "linux2.example" in poll["reason"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_examples.py -q`
Expected: FAIL — the examples do not exist.

- [ ] **Step 3: Create the examples**

`src/keepwatch/examples/relay-pull/config.toml`:

```toml
# Relay, step 1 of 2 (on the hub): pull finished exports from linux1 into a staging folder.
# linux1 needs key authentication from this machine, a known host key, a silent login shell and
# Python 3.6+; nothing is installed there. See: keepwatch docs relay
description = "Pull finished .tar.gz exports from linux1 into the relay staging folder"
recipe = "pull"
action_timeout = "2h"      # big files over a slow link
retry_after = "15m"        # if it goes offline (linux1 down for a while), try again every 15 minutes

[settings]
remote = "me@linux1.example"
remote_dir = "/data/exports"
local_dir = "staging"      # relative to this watch's directory; relay-push reads the same folder
pattern = "*.tar.gz"
settle = "30s"             # unchanged this long on linux1 before it counts as finished
```

`src/keepwatch/examples/relay-push/config.toml`:

```toml
# Relay, step 2 of 2: push staged files to linux2 whenever it is reachable, then delete them here.
# linux2 accepts uploads only (scp); its password comes from an environment variable.
# See: keepwatch docs relay
description = "Push staged exports to linux2 when it is reachable"
recipe = "push"
interval = "5m"            # retry this often while linux2 is away (new files wake it at once)
action_timeout = "2h"

[settings]
local_dir = "staging"      # relative to this watch's directory: point both watches at one folder
dest = "me@linux2.example:incoming/"
password_env = "RELAY_PASSWORD"   # Windows: setx RELAY_PASSWORD "...", then restart keepwatch
pattern = "*.tar.gz"
```

(The test points nowhere real: `linux2.example` never resolves, so the probe answers unknown.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_examples.py tests/test_docs.py -q`
Expected: all pass (the push poll takes up to `reachable_timeout`, 5s).

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/examples/relay-pull/config.toml src/keepwatch/examples/relay-push/config.toml tests/test_examples.py
git commit -m "Relay examples: relay-pull and relay-push"
```

---

### Task 3: End to end through the service

**Files:**
- Modify: `tests/scp_server.py` (`chroot` parameter)
- Test: `tests/test_relay_e2e.py` (new; POSIX only: the stand-in for linux1 serves real absolute paths, which Windows paths cannot be)

**Interfaces:**
- Produces: `ScpServer(root, workdir, chroot: bool = True)`; with `chroot=False` the server reads and writes real paths.

- [ ] **Step 1: Give the test server a real-path mode** — in `tests/scp_server.py`: change `def __init__(self, root: Path, workdir: Path) -> None:` to `def __init__(self, root: Path, workdir: Path, chroot: bool = True) -> None:`, store `self.chroot = chroot`, and in `_listen` replace

```python
            sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(self.root)),
```

with

```python
            sftp_factory=(
                (lambda channel: asyncssh.SFTPServer(channel, chroot=str(self.root)))
                if self.chroot
                else asyncssh.SFTPServer
            ),
```

- [ ] **Step 2: Write the test** — `tests/test_relay_e2e.py`:

```python
import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from keepwatch.service import Service
from portable import PY, literal, toml_path
from scp_server import PASSWORD, ScpServer

pytestmark = [pytest.mark.posix_only, pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")]
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


def wait_for(predicate, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_relay_moves_a_file_from_linux1_to_linux2(tmp_path, make_watch, xdg, monkeypatch):
    linux1, linux2, stage = tmp_path / "linux1", tmp_path / "linux2", tmp_path / "stage"
    for directory in (linux1, linux2, tmp_path / "keys1", tmp_path / "keys2"):
        directory.mkdir()
    source = ScpServer(linux1, tmp_path / "keys1", chroot=False).start()
    dest = ScpServer(linux2, tmp_path / "keys2").start()
    try:
        ssh_config = tmp_path / "ssh_config"
        ssh_config.write_text("", encoding="utf-8")
        isolated = f"['-F', {toml_path(ssh_config)}, '-o', 'IdentityAgent=none'"
        make_watch(
            "relay-pull",
            config=(
                'interval = "1h"\nrecipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\n'
                f"remote_dir = {toml_path(linux1)}\nlocal_dir = {toml_path(stage)}\nsettle = '1s'\n"
                f"port = {source.port}\nidentity = {toml_path(source.client_key)}\n"
                f"known_hosts = {toml_path(source.known_hosts)}\nremote_python = {literal(PY)}\n"
                f"ssh_command = [{literal(PY)}, {toml_path(FAKE_SSH)}]\nssh_options = {isolated}]\n"
            ),
        )
        monkeypatch.setenv("KW_RELAY_PASSWORD", PASSWORD)
        make_watch(
            "relay-push",
            config=(
                'interval = "1h"\nrecipe = "push"\n[settings]\n'
                f'local_dir = {toml_path(stage)}\ndest = "u@127.0.0.1:"\nsettle = \'1s\'\nport = {dest.port}\n'
                f"password_env = 'KW_RELAY_PASSWORD'\nknown_hosts = {toml_path(dest.known_hosts)}\n"
                f"ssh_options = {isolated}, '-o', 'PubkeyAuthentication=no']\n"
            ),
        )
        records = []
        service = Service(paths=xdg, config_path=xdg.config_file, sink=records.append)
        service.start()
        try:
            data = os.urandom(200_000)
            (linux1 / "export-1.tar.gz").write_bytes(data)
            arrived = wait_for(lambda: (linux2 / "export-1.tar.gz.sha256").exists() and not any(stage.iterdir()))
            problems = [r for r in records if r["level"] in ("WARNING", "ERROR", "CRITICAL")]
            assert arrived, problems
        finally:
            service.stop()
        assert (linux2 / "export-1.tar.gz").read_bytes() == data
        expected = f"{hashlib.sha256(data).hexdigest()}  export-1.tar.gz\n"
        assert (linux2 / "export-1.tar.gz.sha256").read_text(encoding="utf-8") == expected
        assert (linux1 / "export-1.tar.gz").exists()  # pull leaves the source alone
        pulled = [r for r in records if r["event"] == "plugin.log" and r["message"].startswith("pulled")]
        pushed = [r for r in records if r["event"] == "plugin.log" and r["message"].startswith("pushed")]
        assert len(pulled) == 1 and len(pushed) == 1
    finally:
        source.stop()
        dest.stop()
```

- [ ] **Step 3: Run it**

Run: `uv run pytest tests/test_relay_e2e.py -q`
Expected: PASS (about 10-20 s). If it fails, report the `problems` list from the assertion; do not loosen the test.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add tests/scp_server.py tests/test_relay_e2e.py
git commit -m "End-to-end relay test through the service"
```

---

### Task 4: The relay guide

**Files:**
- Create: `src/keepwatch/reference/relay.md`
- Modify: `src/keepwatch/reference.py` (`TOPICS`), `README.md`
- Test: `tests/test_docs.py` (`KEY_FACTS`)

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`, `KEY_FACTS`, add:

```python
    "relay": ["relay-pull", "relay-push", "staging", "uv tool install keepwatch", "ssh-keygen", "authorized_keys", "known_hosts", "setx RELAY_PASSWORD", "sha256sum -c", "$?prompt", "keepwatch observe relay-pull", "scp -3", "unknown"],
```

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — no topic `relay`.

- [ ] **Step 2: Write the docs**

(a) `src/keepwatch/reference.py`, `TOPICS`: insert after `("recipes", ...)`:

```python
    ("relay", "Relaying files host A → this machine → host B (the relay-pull and relay-push examples), step by step"),
```

(b) Create `src/keepwatch/reference/relay.md`:

````markdown
# Relay: host A → this machine → host B

A common setup: files appear on a source host (linux1) and must reach a destination host (linux2) that this machine, the **hub**, can only upload to, and that is often disconnected. keepwatch on the hub does it with two watches chained by a staging folder:

| Host | Reachability | Role |
|---|---|---|
| hub (keepwatch, often Windows) | ssh/scp to linux1; scp uploads to linux2 | runs both watches |
| linux1 | none needed towards the hub or linux2 | produces the files (Python 3.6+ present) |
| linux2 | accepts scp uploads from the hub (password), often away | receives the files |

1. **relay-pull** (`recipe = "pull"`): one long-lived ssh connection to linux1 runs keepwatch's remote watcher, which reports every settled file with its sha256 (inotify where linux1 has it, rescans where not, catching up after every reconnect). Each new file is pulled into the staging folder through a hidden `.NAME.part` file, verified, renamed, and recorded in the ledger `pulled`.
2. **relay-push** (`recipe = "push"`): when a file settles in the staging folder and linux2 answers on its ssh port, the file is uploaded, then `NAME.sha256`, and the staged copy is deleted. While linux2 is away the check answers **unknown**: nothing fails, nothing goes offline, files simply wait in the staging folder (the queue).

Both are in `keepwatch docs examples` (`relay-pull`, `relay-push`).

## Setup on the hub

1. **Install keepwatch for good:** `uv tool install keepwatch` (not `uvx`: `keepwatch install` refuses temporary environments), then `keepwatch init`.
2. **A key for linux1:** `ssh-keygen -t ed25519 -f ~/.ssh/id_relay` (no passphrase, or one held by ssh-agent; on Windows the "OpenSSH Authentication Agent" service), then append `~/.ssh/id_relay.pub` to `~/.ssh/authorized_keys` on linux1. Set `identity = '~/.ssh/id_relay'` in relay-pull's `[settings]`.
3. **Known host keys:** connect once by hand to each host and accept its key: `ssh me@linux1 true`, and for linux2 `scp some-small-file me@linux2:incoming/` (enter the password once). keepwatch never accepts an unknown host key; the keys land in `~/.ssh/known_hosts` (or point `known_hosts` at a file of your own).
4. **Silent login shells:** the login scripts on linux1 and linux2 must print nothing for non-interactive sessions, or scp breaks ("Received message too long"). In `.cshrc`/`.tcshrc`: wrap any `echo` in `if ($?prompt) then … endif`; in `.bashrc`/`.profile`: `case $- in *i*) … ;; esac`.
5. **The password for linux2:** keep it in an environment variable, never in a file keepwatch reads. Windows: `setx RELAY_PASSWORD "…"`, then restart keepwatch (it only sees variables that existed when it started). Linux: an `Environment=` line in `systemctl --user edit keepwatch`.
6. **The watches:** copy the two examples into your watches directory (`keepwatch docs examples` prints them), set `remote`, `remote_dir`, `dest` and the same staging folder in both (`local_dir`; an absolute path such as `'C:/relay/staging'` when they live in different directories), then `keepwatch validate relay-pull relay-push`.
7. **Try each part:** `keepwatch observe relay-pull remote --for 1m` must print the files on linux1 as JSON lines; `keepwatch poll relay-push --dry-run` shows whether linux2 is reachable (unknown when it is not). Then `keepwatch install`.

## On linux2

Files arrive in `incoming/` as `NAME`, then `NAME.sha256`. A consumer should wait for the marker and check before using the file:

```sh
cd ~/incoming && for marker in *.sha256; do sha256sum -c "$marker" && process "${marker%.sha256}"; done
```

## Watching it work

- `keepwatch status` shows each watch, its pending events and its observer (running, restarts, last event).
- `keepwatch logs relay-pull --event observer.stopped` tells why the ssh connection to linux1 dropped (ssh's own last lines are in `stderr_tail`).
- `keepwatch logs relay-push --failed` shows failed uploads (a wrong password says "Permission denied").

## Variations and limits

- **Keep or archive instead of delete:** `after = "keep"` or `after = "archive"` (with `archive_dir`, `keep_for`) in relay-push.
- **Direct copy, no staging (`scp -3`):** only with SFTP on both hosts and keys on both (classic scp reports success even when the destination fails, so keepwatch refuses it); not for an upload-only, password-only destination like linux2. Write a watch.py using `ctx.transfer.copy` if your hosts allow it.
- **Several sources:** one relay-pull watch per source host, all writing into the same staging folder.
- Files stay on linux1; a pulled file is pulled again only if its size or modification time changes.
````

(c) `README.md`: after the paragraph starting "keepwatch runs *watches*", add:

```markdown

Watches can also react to events instead of polling: observers keep commands, local directories or remote directories (over one ssh connection, with nothing installed remotely) under watch. Built-in recipes move files between machines with verified scp transfers; `keepwatch docs relay` walks through relaying files host A → this machine → host B.
```

- [ ] **Step 3: Run the docs tests and read the topic**

Run: `uv run pytest tests/test_docs.py -q`, then `uv run keepwatch docs relay`.
Expected: tests pass; the topic renders.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/reference.py src/keepwatch/reference/relay.md README.md tests/test_docs.py
git commit -m "The relay guide"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push; the supervisor verifies, pushes, checks CI and releases 2026.10.3.
