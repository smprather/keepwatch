# keepwatch Plan 7 (Sweep) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The sweep: delete each file from the source host once its pulled copy is verified (`pull` recipe, `delete_remote = true`), report files on the receiving side only once their `.sha256` marker matches (`files` observer, `marker = "sha256"`), and push file and marker in one scp run.

**Architecture:** The ssh command building moves to a stdlib-only `keepwatch/remote.py` (the pull recipe runs in the worker, which cannot import `observers.py`). `remote_watcher.py` gains a delete mode. The pull recipe records verified pulls in a `to_delete` ledger and deletes them in one ssh call per poll, retrying failures after `delete_retry`. `FilesObserver` verifies markers before reporting. `transfer.copy` accepts extra sources so `push` sends file and marker together.

**Tech Stack:** as Plan 6g.

**Spec:** `docs/superpowers/specs/2026-10-02-keepwatch-sweep-design.md`. Builds on release 2026.10.3 (branch `keepwatch-sweep`, from `main`).

## Global Constraints

- Branch `keepwatch-sweep` (already checked out). Do not push. Stage files explicitly (`git add <paths>`); never `git commit -a`/`git add -A` (the working tree has an unrelated `.gitignore` change that must stay uncommitted).
- Before every commit run, without a pipe: `uv run pytest -q` and `uv run ruff check --no-cache src tests`; `git diff --stat` shows only the task's files. Keep import names in alphabetical order. No code formatter.
- All imports at the top of each module. 4-space indentation, no tabs.
- `remote_watcher.py` and `tests/remote_watcher_selftest.py` stay Python 3.6-compatible and ASCII; `remote.py`, `transfer.py` and `recipes/*.py` stay stdlib-only.
- Tests are portable unless marked (`posix_only`, `windows_only`); commands are built from the current Python; TOML paths via `toml_path(...)`.
- If any step is wrong, stop and report your diagnosis instead of improvising.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A file that changed on the source after it was pulled must never be deleted. Tests: Task 2 `test_delete_keeps_a_changed_file`, Task 3 `test_a_changed_source_is_kept`.
- A delete that fails (permissions, host away) must not fail the poll, and must be retried later, not every poll. Test: Task 3 `test_a_failed_delete_is_retried_after_delete_retry`.
- The delete helper must never delete outside `remote_dir` or through a symlink. Test: Task 2 `test_delete_refuses_outside_dir_and_symlinks`.
- A receiving watch must not see a file before its marker matches. Test: Task 4 `test_marker_mode_waits_for_a_matching_marker`.
- The marker must still arrive after the data. Test: Task 5 `test_push_sends_file_and_marker_in_one_scp`.

---

### Task 1: `keepwatch.remote`

**Files:**
- Create: `src/keepwatch/remote.py`
- Modify: `src/keepwatch/observers.py`
- Test: `tests/test_remote_module.py` (new)

**Interfaces:**
- Produces: `remote.AUTO_PYTHON`, `remote.SSH_DEFAULTS` (moved unchanged), `remote.ssh_argv(remote: str, *, port: int | None = None, identity: str | os.PathLike[str] | None = None, ssh_options: Sequence[str] = (), ssh_command: Sequence[str] | None = None, remote_python: str = "auto") -> list[str]`, `remote.watcher_source(options: Mapping[str, Any]) -> str` (moved unchanged). `observers.remote_command(config)` now calls `ssh_argv`; `observers` keeps exporting `AUTO_PYTHON` and `watcher_source` for existing callers.

- [ ] **Step 1: Write the failing test** — `tests/test_remote_module.py`:

```python
import ast
from pathlib import Path

from keepwatch import observers, remote
from keepwatch.config import ObserverConfig

SOURCE = Path(remote.__file__)


def test_ssh_argv_matches_the_observer_command():
    config = ObserverConfig(
        name="r",
        kind="remote_files",
        remote="me@h",
        dir="/d",
        port=2222,
        identity=Path("/keys/id"),
        ssh_options=("-o", "ProxyJump=b"),
        remote_python="/opt/py/bin/python3",
        ssh_command=("ssh.exe",),
    )
    assert observers.remote_command(config) == remote.ssh_argv(
        "me@h",
        port=2222,
        identity=Path("/keys/id"),
        ssh_options=("-o", "ProxyJump=b"),
        ssh_command=("ssh.exe",),
        remote_python="/opt/py/bin/python3",
    )


def test_ssh_argv_defaults():
    assert remote.ssh_argv("me@h") == ["ssh", *remote.SSH_DEFAULTS, "--", "me@h", remote.AUTO_PYTHON]


def test_remote_is_stdlib_only():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert modules <= {"__future__", "collections", "importlib", "json", "os", "shlex", "typing", "keepwatch"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_remote_module.py -q`
Expected: FAIL — `cannot import name 'remote'`.

- [ ] **Step 3: Implement** — create `src/keepwatch/remote.py`:

```python
"""Running keepwatch's remote helper on another host over ssh. Stdlib only.

The `remote_files` observer and the `pull` recipe (which runs in the Python worker, where watchdog is not
available) both build their ssh command lines here.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

AUTO_PYTHON = (
    "sh -c 'if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 \"$@\"; else exec python3 \"$@\"; fi' sh -u -"
)
SSH_DEFAULTS = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    # No connection sharing: a ControlPersist master would outlive the ssh keepwatch stops and hold its pipes.
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)


def ssh_argv(
    remote: str,
    *,
    port: int | None = None,
    identity: str | os.PathLike[str] | None = None,
    ssh_options: Sequence[str] = (),
    ssh_command: Sequence[str] | None = None,
    remote_python: str = "auto",
) -> list[str]:
    """The ssh command line that runs the remote helper (its source comes on stdin). User ssh_options come first."""
    argv = [*(ssh_command or ("ssh",)), *ssh_options, *SSH_DEFAULTS]
    if port is not None:
        argv += ["-p", str(port)]
    if identity is not None:
        argv += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    python = AUTO_PYTHON if remote_python == "auto" else f"{shlex.quote(remote_python)} -u -"
    return [*argv, "--", remote, python]


def watcher_source(options: Mapping[str, Any]) -> str:
    """The remote helper's source with its options prepended as an assignment (ASCII: json escapes the rest)."""
    source = resources.files("keepwatch").joinpath("remote_watcher.py").read_text(encoding="utf-8")
    return f"KEEPWATCH_REMOTE_ARGS = {json.dumps(json.dumps(options))}\n{source}"
```

In `src/keepwatch/observers.py`:
- delete the `AUTO_PYTHON = (...)` and `SSH_DEFAULTS = (...)` assignments and the whole `watcher_source` function;
- add `from keepwatch.remote import AUTO_PYTHON as AUTO_PYTHON` and `from keepwatch.remote import ssh_argv, watcher_source` (in the keepwatch import block, alphabetical);
- remove `import shlex` and `from importlib import resources` if nothing else in the module uses them (ruff tells you);
- replace the body of `remote_command` (keep its docstring) with:

```python
    assert config.remote is not None
    return ssh_argv(
        config.remote,
        port=config.port,
        identity=config.identity,
        ssh_options=config.ssh_options,
        ssh_command=config.ssh_command,
        remote_python=config.remote_python,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_remote_module.py tests/test_remote_files_observer.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/remote.py src/keepwatch/observers.py tests/test_remote_module.py
git commit -m "keepwatch.remote: the ssh command line for the remote helper, stdlib only"
```

---

### Task 2: The delete helper

**Files:**
- Modify: `src/keepwatch/remote_watcher.py`, `tests/remote_watcher_selftest.py`

**Interfaces:**
- Produces: options `mode` (`"watch"` default, or `"delete"`) and `files` (`[{"path", "size", "mtime"}]`, default `[]`). In delete mode the helper prints one line per file, `{"event": "deleted" | "gone" | "changed" | "refused" | "failed", "path", "reason"?}`, then `{"event": "done"}`, and exits 0; `dir` need not exist.

- [ ] **Step 1: Write the failing tests** — in `tests/remote_watcher_selftest.py`, add this helper method to `WatcherTests` and the tests below (before `if __name__ == "__main__":`):

```python
    def delete(self, files, directory=None):
        options = {"mode": "delete", "dir": directory or self.dir, "files": files}
        code, out, err = self.run_once(options)
        lines = [json.loads(line) for line in out.decode("ascii").splitlines()]
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[-1], {"event": "done"})
        return {line["path"]: line for line in lines[:-1]}

    def item(self, path):
        info = os.stat(path)
        return {"path": os.path.abspath(path), "size": info.st_size, "mtime": info.st_mtime}

    def test_delete_removes_an_unchanged_file(self):
        path = self.write("a.gz")
        results = self.delete([self.item(path)])
        self.assertEqual(results[os.path.abspath(path)]["event"], "deleted")
        self.assertFalse(os.path.exists(path))

    def test_delete_keeps_a_changed_file(self):
        path = self.write("a.gz")
        pulled = self.item(path)
        with open(path, "ab") as handle:
            handle.write(b"more")
        self.assertEqual(self.delete([pulled])[pulled["path"]]["event"], "changed")
        self.assertTrue(os.path.exists(path))

    def test_delete_reports_a_missing_file_as_gone(self):
        path = os.path.join(self.dir, "never.gz")
        results = self.delete([{"path": path, "size": 1, "mtime": 1.5}])
        self.assertEqual(results[path]["event"], "gone")

    def test_delete_refuses_outside_dir_and_symlinks(self):
        outside_dir = tempfile.mkdtemp()
        try:
            outside = os.path.join(outside_dir, "x.gz")
            with open(outside, "wb") as handle:
                handle.write(b"x")
            results = self.delete([self.item(outside)])
            self.assertEqual(results[os.path.abspath(outside)]["event"], "refused")
            self.assertTrue(os.path.exists(outside))
            if hasattr(os, "symlink") and os.name != "nt":
                link = os.path.join(self.dir, "link.gz")
                os.symlink(outside, link)
                info = os.lstat(link)
                item = {"path": os.path.abspath(link), "size": info.st_size, "mtime": info.st_mtime}
                self.assertEqual(self.delete([item])[item["path"]]["event"], "refused")
                self.assertTrue(os.path.exists(outside))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a POSIX user that is not root"
    )
    def test_delete_reports_a_failure(self):
        path = self.write("a.gz")
        os.chmod(self.dir, 0o500)
        try:
            results = self.delete([self.item(path)])
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(results[os.path.abspath(path)]["event"], "failed")
        self.assertIn("ermission", results[os.path.abspath(path)]["reason"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run python tests/remote_watcher_selftest.py`
Expected: FAIL — `unknown option(s): files, mode`.

- [ ] **Step 3: Implement** — in `src/keepwatch/remote_watcher.py`:

(a) `DEFAULTS`: add `"mode": "watch",` and `"files": [],` after `"skip": [],`. Extend the module docstring's output paragraph with:

```
Delete mode ({"mode": "delete", "dir", "files": [{"path", "size", "mtime"}]}): deletes each file only if it is
a regular file directly in `dir` (resolved, never through a symlink) whose size and mtime still match; prints
{"event": "deleted" | "gone" | "changed" | "refused" | "failed", "path", "reason"?} per file, then
{"event": "done"}.
```

(b) In `parse_options`, replace `if not options["dir"]:` with `if options["mode"] not in ("watch", "delete"):` / `raise ValueError("option 'mode' must be watch or delete")` followed by the existing `dir` check, so it reads:

```python
    if options["mode"] not in ("watch", "delete"):
        raise ValueError("option 'mode' must be watch or delete")
    if not options["dir"]:
        raise ValueError("option 'dir' is required")
```

(c) Add before `parse_options`:

```python
def delete_one(item, directory):
    """Delete one pulled file if it is still exactly what was pulled. Returns the result line (without path)."""
    raw = str(item.get("path", "")).encode("utf-8", "surrogateescape")
    if os.path.realpath(os.path.dirname(raw)) != directory:
        return {"event": "refused", "reason": "not directly in " + decode(directory)}
    try:
        info = os.lstat(raw)
    except FileNotFoundError:
        return {"event": "gone"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
    if not stat.S_ISREG(info.st_mode):
        return {"event": "refused", "reason": "not a regular file"}
    if info.st_size != item.get("size") or info.st_mtime != float(item.get("mtime", -1)):
        return {"event": "changed"}
    try:
        os.unlink(raw)
    except FileNotFoundError:
        return {"event": "gone"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
    return {"event": "deleted"}


def delete_files(options, output):
    directory = os.path.realpath(os.path.expanduser(options["dir"]).encode("utf-8", "surrogateescape"))
    for item in options["files"]:
        try:
            result = delete_one(item, directory)
        except Exception as exc:  # keep going: one odd entry must not stop the others
            result = {"event": "failed", "reason": str(exc)}
        result["path"] = item.get("path")
        output.send(result)
    output.send({"event": "done"})
    return 0
```

(d) In `main`, right after the `parse_options` try/except, add:

```python
    if options["mode"] == "delete":
        try:
            return delete_files(options, Output(sys.stdout))
        except Closed:
            silence_stdout()
            return 0
```

- [ ] **Step 4: Run the tests**

Run: `uv run python tests/remote_watcher_selftest.py` and `uv run pytest tests/test_remote_watcher.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py
git commit -m "Remote helper: delete mode (only unchanged regular files directly in the directory)"
```

---

### Task 3: `pull` recipe: `delete_remote`

**Files:**
- Modify: `src/keepwatch/config.py` (pull settings, validation), `src/keepwatch/recipes/pull.py`
- Test: `tests/test_recipe_pull_delete.py` (new), `tests/test_config_recipe.py`

**Interfaces:**
- Produces: pull settings `delete_remote` (bool, `false`) and `delete_retry` (duration, `10m`); config error when `delete_remote = true` with `checksum = false`; `recipes.pull.TO_DELETE = "to_delete"`, `recipes.pull.split_key(key) -> dict`, `recipes.pull.due_deletions(ctx) -> list[str]`, `recipes.pull.delete_remote(ctx, keys) -> None`. `check` answers TRUE for new files or due deletions.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_config_recipe.py`, append:

```python
def test_delete_remote_needs_checksum(make_watch):
    [problem] = problems(make_watch, PULL + "delete_remote = true\nchecksum = false\n")
    assert "delete_remote = true needs checksum = true" in problem


def test_delete_remote_defaults(make_watch):
    settings = load(make_watch, PULL).settings
    assert (settings["delete_remote"], settings["delete_retry"]) == (False, 600.0)
```

(b) `tests/test_recipe_pull_delete.py` (POSIX only: the stand-in source host serves real absolute paths):

```python
import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.ctx import Ledger
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import PY, literal, toml_path
from scp_server import ScpServer

pytestmark = [pytest.mark.posix_only, pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")]
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


@pytest.fixture
def linux1(tmp_path):
    root = tmp_path / "linux1"
    root.mkdir()
    (tmp_path / "keys").mkdir()
    server = ScpServer(root, tmp_path / "keys", chroot=False).start()
    yield server
    server.stop()


def sweep_watch(make_watch, server, tmp_path, extra=""):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return load_watch_config(
        make_watch(
            "sweep",
            config=(
                'recipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\n'
                f"remote_dir = {toml_path(server.root)}\nlocal_dir = {toml_path(tmp_path / 'stage')}\n"
                f"port = {server.port}\nidentity = {toml_path(server.client_key)}\n"
                f"known_hosts = {toml_path(server.known_hosts)}\nremote_python = {literal(PY)}\n"
                f"ssh_command = [{literal(PY)}, {toml_path(FAKE_SSH)}]\n"
                f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\n"
                f"delete_remote = true\n{extra}"
            ),
        )
    )


def file_event(path):
    info = path.stat()
    return {
        "event": "file",
        "path": str(path),
        "name": path.name,
        "size": info.st_size,
        "mtime": info.st_mtime,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "observer": "remote",
        "received": "2026-10-02T00:00:00.000+00:00",
    }


def poll(xdg, watch, events=(), state=None, records=None):
    global_config = load_global_config(xdg.config_file, xdg)
    sink = records.append if records is not None else (lambda record: None)
    engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=sink, pid=os.getpid())
    return engine.poll(watch, state or WatchState(False), events=list(events))


def ledger(xdg, name):
    return Ledger(xdg.watch_data_dir("sweep") / "ledgers" / f"{name}.json")


def test_a_verified_pull_deletes_the_source(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [file_event(source)], records=records)
    assert report.outcome is Outcome.TRUE and not report.failed
    assert (tmp_path / "stage" / "a.tar.gz").read_bytes() == b"data"
    assert not source.exists()
    assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 1
    messages = [r["message"] for r in records if r["event"] == "plugin.log"]
    assert any(m.startswith("deleted a.tar.gz from u@127.0.0.1") for m in messages)


def test_a_failed_delete_is_retried_after_delete_retry(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    watch = sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '1s'\n")
    os.chmod(linux1.root, 0o500)
    try:
        first = poll(xdg, watch, [file_event(source)])
        assert first.outcome is Outcome.TRUE and not first.failed
        assert source.exists() and len(ledger(xdg, "to_delete")) == 1
        assert poll(xdg, watch, [], state=first.after).outcome is Outcome.FALSE  # not due yet
    finally:
        os.chmod(linux1.root, 0o700)
    time.sleep(1.2)
    retry = poll(xdg, watch, [], state=first.after)
    assert retry.outcome is Outcome.TRUE and not retry.failed
    assert not source.exists() and len(ledger(xdg, "to_delete")) == 0
```

and append this test too:

```python
def test_a_changed_source_is_kept(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    event = file_event(source)
    watch = sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n")  # the queued key is due at once
    source.write_bytes(b"changed after the observer reported it")  # different size: the pull fails its check
    records = []
    report = poll(xdg, watch, [event], records=records)
    assert report.failed  # the pull's size check failed, so nothing was pulled or queued
    assert source.exists() and len(ledger(xdg, "to_delete")) == 0
    from keepwatch.recipes import pull

    ledger(xdg, "to_delete").add(pull.event_key(event))  # queue the old version's key directly
    records.clear()
    second = poll(xdg, watch, [], records=records)
    assert not second.failed and source.exists()
    assert len(ledger(xdg, "to_delete")) == 0
    assert any("changed on u@127.0.0.1" in r["message"] for r in records if r["event"] == "plugin.log")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_config_recipe.py -q`
Expected: FAIL — `unknown key 'delete_remote' in [settings]`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/config.py`, `RECIPE_SETTINGS["pull"]`: add after the `on_conflict` key:

```python
        Key(
            "delete_remote",
            "bool",
            False,
            "Delete each file from the source host once its copy in local_dir is verified (size and sha256) and "
            "recorded. Only an unchanged regular file directly in remote_dir is deleted. Needs checksum = true.",
        ),
        Key(
            "delete_retry",
            "duration",
            600.0,
            "With delete_remote: retry a delete that failed (permissions, host away) after this long.",
        ),
```

and in `_recipe_settings`, before the `if recipe == "push":` block, add:

```python
    if recipe == "pull" and values["delete_remote"] and not values["checksum"]:
        collector.add(
            "delete_remote = true needs checksum = true: the only other copy may go only after a verified pull",
            key="delete_remote",
            table="settings",
            topic="recipes",
        )
```

(b) `src/keepwatch/recipes/pull.py`: replace the module docstring's last sentence with `The same ledger tells the observer which files to skip when it reconnects. With delete_remote, verified files are also queued in the ledger "to_delete" and deleted from the source host by keepwatch's remote helper (see keepwatch docs recipes).`; add imports:

```python
import json
import subprocess
from datetime import datetime, timezone

from keepwatch.remote import ssh_argv, watcher_source
from keepwatch.transfer import known_hosts_option
```

(keep `from typing import Any` and `from keepwatch.ctx import Ctx`; order the blocks as ruff wants), add `TO_DELETE = "to_delete"` after `LEDGER = "pulled"`, and add after `transfer_options`:

```python
def split_key(key: str) -> dict[str, Any]:
    """A ledger key back into the helper's {"path", "size", "mtime"}."""
    path, size, mtime = key.rsplit("|", 2)
    return {"path": path, "size": int(size), "mtime": float(mtime)}


def due_deletions(ctx: Ctx) -> list[str]:
    """Queued deletions whose last attempt is at least delete_retry old."""
    settings = ctx.settings
    if not settings["delete_remote"]:
        return []
    pending = ctx.ledger(TO_DELETE)
    now = datetime.now(timezone.utc)
    return [key for key in pending if (now - pending.added_at(key)).total_seconds() >= settings["delete_retry"]]


def delete_remote(ctx: Ctx, keys: list[str]) -> None:
    """Delete these pulled files from the source host in one ssh call. Never raises: problems are logged and retried."""
    if not keys:
        return
    settings = ctx.settings
    pending = ctx.ledger(TO_DELETE)
    remote = settings["remote"]
    ssh_options = [*settings["ssh_options"], *(known_hosts_option(settings["known_hosts"]) if settings["known_hosts"] else [])]
    argv = ssh_argv(
        remote,
        port=settings["port"],
        identity=settings["identity"],
        ssh_options=ssh_options,
        ssh_command=settings["ssh_command"],
        remote_python=settings["remote_python"],
    )
    options = {"mode": "delete", "dir": settings["remote_dir"], "files": [split_key(key) for key in keys]}
    try:
        completed = ctx.run(argv, input=watcher_source(options), check=False)
        stdout, stderr = completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr = "", str(exc)
    results: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("path"), str):
            results[message["path"]] = message
    for key in keys:
        path = split_key(key)["path"]
        name = path.rsplit("/", 1)[-1]
        result = results.get(path)
        if result is None:
            pending.add(key)  # refreshes its stamp: retried after delete_retry
            tail = " | ".join(stderr.strip().splitlines()[-3:]) or "no answer"
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, tail)
        elif result["event"] == "deleted":
            pending.discard(key)
            ctx.log.info("deleted %s from %s", name, remote)
        elif result["event"] == "gone":
            pending.discard(key)
            ctx.log.info("%s was already gone from %s", name, remote)
        elif result["event"] in ("changed", "refused"):
            pending.discard(key)
            reason = result.get("reason") or "it is not the file that was pulled"
            if result["event"] == "changed":
                ctx.log.warning("%s changed on %s since it was pulled; kept there", name, remote)
            else:
                ctx.log.warning("not deleting %s on %s: %s", name, remote, reason)
        else:
            pending.add(key)
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, result.get("reason"))
```

Change `check` to return TRUE for due deletions too — replace its last line `return bool(new), new` with:

```python
    return bool(new) or bool(due_deletions(ctx)), new
```

Replace `on_true` with:

```python
def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    queue = ctx.ledger(TO_DELETE) if settings["delete_remote"] else None
    options = transfer_options(settings)
    fresh: list[str] = []
    try:
        for item in ctx.payload:
            if item["key"] in pulled:
                continue
            sha256 = item["sha256"] if settings["checksum"] else None
            final = ctx.transfer.pull(
                f"{settings['remote']}:{item['path']}",
                settings["local_dir"],
                size=item["size"],
                sha256=sha256,
                on_conflict=settings["on_conflict"],
                **options,
            )
            pulled.add(item["key"])
            if queue is not None:
                queue.add(item["key"])
                fresh.append(item["key"])
            ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
    finally:
        if queue is not None:
            delete_remote(ctx, sorted(set(fresh) | set(due_deletions(ctx))))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_recipe_pull.py tests/test_config_recipe.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/config.py src/keepwatch/recipes/pull.py tests/test_recipe_pull_delete.py tests/test_config_recipe.py
git commit -m "pull recipe: delete_remote deletes verified files from the source, retrying failures"
```

---

### Task 4: `files` observer: `marker = "sha256"`

**Files:**
- Modify: `src/keepwatch/config.py` (`ObserverConfig.marker`, `OBSERVER_KEYS`, `_OBSERVER_KIND_KEYS`, `_observer`), `src/keepwatch/observers.py` (`FilesObserver`)
- Test: `tests/test_files_observer.py`, `tests/test_config_observe.py`

**Interfaces:**
- Produces: `ObserverConfig.marker: str = "none"`; observer key `marker` (kind `files`; `none` or `sha256`). With `sha256`, events gain `"sha256"` and `"marker"`.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_config_observe.py`, append:

```python
def test_files_marker(make_watch):
    config = load(make_watch, "[observe.in]\nkind = 'files'\npath = 'in'\nmarker = 'sha256'\n")
    assert config.observers["in"].marker == "sha256"


def test_files_marker_must_be_a_choice(make_watch):
    [problem] = problems(make_watch, "[observe.in]\nkind = 'files'\npath = 'in'\nmarker = 'md5'\n")
    assert "'marker' must be none or sha256" in problem
```

(b) `tests/test_files_observer.py`: add `import hashlib` to the imports and append:

```python
def write_marker(directory, name, digest=None):
    data = (directory / name).read_bytes()
    marker = directory / f"{name}.sha256"
    marker.write_text(f"{digest or hashlib.sha256(data).hexdigest()}  {name}\n", encoding="utf-8")
    age(marker)
    return marker


def test_marker_mode_waits_for_a_matching_marker(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        time.sleep(2.5)
        assert events == []  # no marker yet
        write_marker(inbox, "a.tar.gz")
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.tar.gz"]  # the marker itself is never reported
    assert events[0]["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert Path(events[0]["marker"]) == inbox / "a.tar.gz.sha256"


def test_marker_mismatch_warns_once_then_a_reupload_is_reported(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    write_marker(inbox, "a.tar.gz", digest="0" * 64)
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.output"))
        time.sleep(3.0)
        assert events == []
        write_marker(inbox, "a.tar.gz")
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    warnings = [r for r in kinds(records, "observer.output") if "does not match" in r["text"]]
    assert len(warnings) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_files_observer.py tests/test_config_observe.py -q`
Expected: FAIL — `unknown key 'marker'`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/config.py`: add `marker: str = "none"` to `ObserverConfig` (after `skip_ledger`); in `OBSERVER_KEYS` add after the `settle` key:

```python
    Key(
        "marker",
        "str",
        "none",
        "kind = files: `sha256` reports a file only once NAME.sha256 beside it (sha256sum format) matches it; the "
        "markers themselves are never reported. `none`: no markers.",
    ),
```

add `"marker",` to the end of `_OBSERVER_KIND_KEYS["files"]`; and in `_observer`, next to the `skip_ledger` check, add:

```python
        if key_name == "marker" and converted not in ("none", "sha256"):
            collector.add(f"'marker' must be none or sha256, got {converted!r}", key="marker", table=table, topic="observers")
            continue
```

(b) `src/keepwatch/observers.py`: add `import re` (stdlib block) and `from keepwatch.transfer import sha256_file` (keepwatch block). In `FilesObserver.__init__` add `self._mismatched: set[tuple[str, int, int, int, int]] = set()`. In `_wanted`, return `False` for names ending in `.sha256` when `self.config.marker == "sha256"`:

```python
    def _wanted(self, name: str) -> bool:
        if self.config.marker == "sha256" and name.endswith(".sha256"):
            return False
        return fnmatch.fnmatch(name, self.config.pattern) and not self._ignored(name)
```

Add this method to `FilesObserver`:

```python
    def _verified(self, path: str, info: os.stat_result) -> dict[str, Any] | None:
        """{} without markers; with marker = sha256 the event fields, or None while the marker is missing or wrong."""
        if self.config.marker != "sha256":
            return {}
        marker = Path(path + ".sha256")
        try:
            marker_info = marker.stat()
        except OSError:
            return None
        if time.time() - marker_info.st_mtime < self.config.settle:
            return None
        version = (path, info.st_size, info.st_mtime_ns, marker_info.st_size, marker_info.st_mtime_ns)
        if version in self._mismatched:
            return None
        try:
            fields = marker.read_text(encoding="utf-8", errors="replace").split()
            expected = fields[0].lower() if fields else ""
            actual = sha256_file(path)
        except OSError:
            return None
        if re.fullmatch(r"[0-9a-f]{64}", expected) and actual == expected:
            return {"sha256": actual, "marker": str(marker)}
        self._mismatched.add(version)
        self._sink(
            make_record(
                "observer.output",
                level="WARNING",
                **self._tag(),
                stream="marker",
                text=f"{Path(path).name}: {marker.name} does not match (sha256 {actual}); waiting for a new upload",
            )
        )
        return None
```

In `_watch`, replace

```python
                if now - seen[1] >= settle and wall - info.st_mtime >= settle:
                    del candidates[path]
                    self._reported.add((path, *key))
                    self.emit(
                        {
                            "event": "file",
                            "path": path,
                            "name": Path(path).name,
                            "size": info.st_size,
                            "mtime": info.st_mtime,
                        }
                    )
```

with

```python
                if now - seen[1] >= settle and wall - info.st_mtime >= settle:
                    del candidates[path]  # settled; without a matching marker it settles again and is re-checked
                    extra = self._verified(path, info)
                    if extra is None:
                        continue
                    self._reported.add((path, *key))
                    self.emit(
                        {
                            "event": "file",
                            "path": path,
                            "name": Path(path).name,
                            "size": info.st_size,
                            "mtime": info.st_mtime,
                            **extra,
                        }
                    )
```

and after `self._reported &= current` add:

```python
            self._mismatched = {version for version in self._mismatched if version[:3] in current}
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_files_observer.py tests/test_config_observe.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/config.py src/keepwatch/observers.py tests/test_files_observer.py tests/test_config_observe.py
git commit -m "files observer: marker = sha256 reports a file only once its marker matches"
```

---

### Task 5: Push file and marker in one scp

**Files:**
- Modify: `src/keepwatch/transfer.py` (`scp_argv`, `copy`, `push`)
- Modify: `tests/test_transfer_scp.py`, `tests/test_transfer.py`

**Interfaces:**
- Produces: `scp_argv(source, destination, options, version, extra_sources: Sequence[Endpoint] = ())`, `copy(source, destination, *, extra_sources=(), options=…, report=…)`: extra local sources go in the same scp run, in order, into `destination` taken as a directory. `push` with `marker = "sha256"` uploads `[file, NAME.sha256]` into `remote_dir` in one run.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_transfer_scp.py`: in `test_reports_each_scp_run` change `assert len(calls) == 2  # the file, then its marker` to `assert len(calls) == 1  # file and marker in one scp run`, and append:

```python
def test_push_sends_file_and_marker_in_one_scp(tmp_path, server, ssh_config):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    calls = []
    push(local, REMOTE, options=key_options(server, ssh_config), report=lambda *call: calls.append(call))
    [call] = calls
    argv = call[0]
    assert argv[-3:-1] == [str(local), str(Path(argv[-2]))] and argv[-2].endswith("a.tar.gz.sha256")
    assert (server.root / "a.tar.gz").read_bytes() == b"payload"
    assert (server.root / "a.tar.gz.sha256").exists()
```

(add `from pathlib import Path` to its imports if missing).

(b) `tests/test_transfer.py`, append:

```python
def test_extra_sources_follow_the_first():
    argv = scp_argv(parse_endpoint("a.gz"), parse_endpoint("me@h:in/"), ScpOptions(), (10, 5), extra_sources=[parse_endpoint("a.gz.sha256")])
    assert argv[-3:] == ["a.gz", "a.gz.sha256", "me@h:in/"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py -q`
Expected: FAIL — `unexpected keyword argument 'extra_sources'`; two scp runs.

- [ ] **Step 3: Implement** — in `src/keepwatch/transfer.py`:

- `scp_argv`: add the parameter `extra_sources: Sequence[Endpoint] = ()` after `version`, and replace its last two lines

```python
    classic = options.protocol == "scp"
    return [*argv, "--", _operand(source, classic=classic, source=True), _operand(destination, classic=classic, source=False)]
```

with

```python
    classic = options.protocol == "scp"
    sources = [_operand(end, classic=classic, source=True) for end in (source, *extra_sources)]
    return [*argv, "--", *sources, _operand(destination, classic=classic, source=False)]
```

- `copy`: add `extra_sources: Sequence[str | os.PathLike[str] | Endpoint] = (),` after `*,` in its signature; change its docstring first line to `"""Copy one file (and extra_sources, in order, into destination as a directory) with scp. …` (keep the rest), and replace

```python
    argv = scp_argv(src, dst, options, openssh_version(_ssh_beside(options.scp_command[0])))
```

with

```python
    extra = [_endpoint(item) for item in extra_sources]
    argv = scp_argv(src, dst, options, openssh_version(_ssh_beside(options.scp_command[0])), extra_sources=extra)
```

- `push`: replace

```python
    target = directory.child(local.name)
    copy(local, target, options=options, report=report)
    if marker == "sha256":
        with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
            marker_file = Path(work) / f"{local.name}.sha256"
            marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
            copy(marker_file, directory.child(marker_file.name), options=options, report=report)
    return target.scp_arg()
```

with

```python
    target = directory.child(local.name)
    if marker == "none":
        copy(local, target, options=options, report=report)
        return target.scp_arg()
    with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
        marker_file = Path(work) / f"{local.name}.sha256"
        marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
        # One scp run (one login): scp sends its sources in order, so the data arrives before its marker.
        copy(local, directory, extra_sources=[marker_file], options=options, report=report)
    return target.scp_arg()
```

and update `push`'s docstring to `"""Upload a file into remote_dir followed (marker="sha256") by NAME.sha256 in sha256sum format, in one scp run. Returns the remote path."""`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_recipe_push.py tests/test_cli_kit.py tests/test_relay_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/transfer.py tests/test_transfer.py tests/test_transfer_scp.py
git commit -m "push: file and marker in one scp run"
```

---

### Task 6: Examples, docs, end to end

**Files:**
- Create: `src/keepwatch/examples/relay-receive/config.toml`, `src/keepwatch/examples/relay-receive/watch.py`
- Modify: `src/keepwatch/examples/relay-pull/config.toml`, `src/keepwatch/reference/relay.md`, `src/keepwatch/reference/recipes.md`, `src/keepwatch/reference/observers.md`, `src/keepwatch/reference/transfers.md`
- Test: `tests/test_examples.py`, `tests/test_docs.py`, `tests/test_sweep_e2e.py` (new)

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_examples.py`: add `import hashlib` to its imports; change the expected list in `test_examples_are_in_the_docs` to include `"relay-receive"` (between `"relay-push"` and `"site-down"`), and append:

```python
def test_relay_receive_example_processes_a_verified_arrival(xdg):
    watch_dir = install(xdg, "relay-receive")
    incoming = Path.home() / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.tar.gz").write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    event = {"event": "file", "path": str(incoming / "a.tar.gz"), "name": "a.tar.gz", "sha256": digest, "marker": str(incoming / "a.tar.gz.sha256")}
    (incoming / "a.tar.gz.sha256").write_text(f"{digest}  a.tar.gz\n", encoding="utf-8")
    assert run("validate", "relay-receive").exit_code == 0
    result = run("poll", "relay-receive", "--events", json.dumps([event]), "--json")
    assert json.loads(result.output)["polls"][0]["failed"] is False, result.output
    done = Path.home() / "incoming" / "done"
    assert (done / "a.tar.gz").read_bytes() == b"payload" and (done / "a.tar.gz.sha256").exists()
    assert watch_dir.exists()
```

(b) `tests/test_docs.py`, `KEY_FACTS`: append `"delete_remote"`, `"delete_retry"`, `"to_delete"` to `"recipes"`; `"marker = \"sha256\""` to `"observers"`; `"relay-receive"`, `"delete_remote = true"`, `"Receiving on linux2"` to `"relay"`; `"one scp run"` to `"transfers"`.

(c) `tests/test_sweep_e2e.py` (POSIX only):

```python
import hashlib
import json
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
RECEIVE = """
    import json


    def check(ctx):
        return bool(ctx.events), ctx.events


    def on_true(ctx):
        with open(ctx.settings["log"], "a", encoding="utf-8") as handle:
            for event in ctx.payload:
                handle.write(json.dumps({"name": event["name"], "sha256": event["sha256"]}) + "\\n")
"""


def wait_for(predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_a_spawned_file_is_swept_to_linux2_and_received_once(tmp_path, make_watch, xdg, monkeypatch):
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
                "delete_remote = true\n"
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
        received = tmp_path / "received.jsonl"
        make_watch(
            "relay-receive",
            config=(
                'interval = "1h"\n[observe.arrivals]\nkind = "files"\n'
                f"path = {toml_path(linux2)}\npattern = '*.tar.gz'\nmarker = 'sha256'\nsettle = '1s'\n"
                f"[settings]\nlog = {toml_path(received)}\n"
            ),
            files={"watch.py": RECEIVE},
        )
        records = []
        service = Service(paths=xdg, config_path=xdg.config_file, sink=records.append)
        service.start()
        try:
            data = os.urandom(300_000)
            (linux1 / "export-1.tar.gz").write_bytes(data)
            done = wait_for(lambda: received.exists() and not (linux1 / "export-1.tar.gz").exists())
            problems = [r for r in records if r["level"] in ("WARNING", "ERROR", "CRITICAL")]
            assert done, problems
            time.sleep(3.0)  # nothing is received twice
        finally:
            service.stop()
        assert (linux2 / "export-1.tar.gz").read_bytes() == data
        lines = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines()]
        assert lines == [{"name": "export-1.tar.gz", "sha256": hashlib.sha256(data).hexdigest()}]
        assert list(stage.iterdir()) == []
    finally:
        source.stop()
        dest.stop()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_examples.py tests/test_docs.py tests/test_sweep_e2e.py -q`
Expected: the example and docs tests FAIL (no `relay-receive`, missing facts); the e2e test should already pass after Tasks 1-5 (it is the integration check).

- [ ] **Step 3: Examples**

(a) `src/keepwatch/examples/relay-pull/config.toml`: add after the `settle = "30s"…` line:

```toml
# delete_remote = true     # sweep: delete each file from linux1 once its copy here is verified (needs checksum)
```

(b) Create `src/keepwatch/examples/relay-receive/config.toml`:

```toml
# Relay, the receiving end (keepwatch on linux2): act on each file once it has fully arrived.
# relay-push uploads NAME, then NAME.sha256; marker = "sha256" reports NAME only when they match.
# See: keepwatch docs relay
description = "Move each verified arrival from incoming/ to incoming/done/"
interval = "1h"            # events wake the watch at once; this is only a fallback

[observe.arrivals]
kind = "files"
path = "~/incoming"
pattern = "*.tar.gz"
marker = "sha256"
settle = "10s"

[settings]
done_dir = "~/incoming/done"
```

(c) Create `src/keepwatch/examples/relay-receive/watch.py`:

```python
"""Handle each export once it has fully arrived and its .sha256 marker matched (see keepwatch docs relay).

Replace the body of on_true with your own processing; keep recording handled files in the ledger, because
events can arrive more than once.
"""

import shutil
from pathlib import Path


def check(ctx):
    handled = ctx.ledger("handled")
    new = [event for event in ctx.events if event.get("event") == "file" and event["sha256"] not in handled]
    return bool(new), [{"path": event["path"], "marker": event["marker"], "sha256": event["sha256"]} for event in new]


def on_true(ctx):
    handled = ctx.ledger("handled")
    done = Path(ctx.settings["done_dir"]).expanduser()
    done.mkdir(parents=True, exist_ok=True)
    for item in ctx.payload:
        if item["sha256"] in handled:
            continue
        for path in (item["path"], item["marker"]):
            if Path(path).exists():
                shutil.move(path, str(done / Path(path).name))
        handled.add(item["sha256"])
        ctx.log.info("received %s", Path(item["path"]).name, extra={"sha256": item["sha256"]})
```

- [ ] **Step 4: Docs**

(a) `relay.md`: replace everything from the heading `## On linux2` down to (not including) `## Watching it work` with:

````markdown
## Receiving on linux2

Run keepwatch on linux2 too, with a watch like the `relay-receive` example: a `files` observer with `marker = "sha256"` on the incoming directory. It reports `NAME` only once `NAME.sha256` has arrived beside it and matches, so the hook never sees a half-uploaded file:

```toml
[observe.arrivals]
kind = "files"
path = "~/incoming"
pattern = "*.tar.gz"
marker = "sha256"
```

Each event carries the verified `sha256` and the `marker` path; record handled files in a ledger (events can come more than once) and move or delete the file and its marker when done. Without keepwatch there, a script can do the same: wait for the marker, then `sha256sum -c NAME.sha256`.

## Sweep: delete from the source

Set `delete_remote = true` in relay-pull to delete each file from linux1 once its copy on the hub is verified (size and sha256) and recorded. Only an unchanged regular file directly in `remote_dir` is deleted: a file that changed since it was pulled stays on linux1 (with a WARNING). Deletes go through keepwatch's remote helper in one short ssh call per poll; a delete that fails (permissions, linux1 away) never fails the poll and is retried after `delete_retry` (10 minutes). linux1 then only ever holds files that have not reached the hub yet.
````

(b) `recipes.md`, in the pull section, add a bullet after the `on_conflict` bullet:

```markdown
- With `delete_remote = true`, each verified file is also queued in the ledger `to_delete` and deleted from the source host by keepwatch's remote helper (one ssh call per poll): only an unchanged regular file directly in `remote_dir`. Problems never fail the poll; failed deletes are retried after `delete_retry`. Needs `checksum = true`.
```

(c) `observers.md`, in the `kind = "files"` section, add at the end:

```markdown
With `marker = "sha256"` a file is reported only once `NAME.sha256` (sha256sum format) beside it has settled and matches; the event gains `sha256` and `marker`, and the markers themselves are never reported. A mismatch is logged once (an `observer.output` WARNING) and the file is reported after a corrected upload. The receiving end of a relay uses this (`keepwatch docs relay`).
```

(d) `transfers.md`, in the Operations table's `push` row, change the "What it does" cell to: ``Uploads `NAME`, then `NAME.sha256` in `sha256sum` format, in one scp run (one login, data first). Returns / prints the remote path.``

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_examples.py tests/test_docs.py tests/test_sweep_e2e.py -q`
Expected: all pass.

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the ten files. Then:

```bash
git add src/keepwatch/examples/relay-receive/config.toml src/keepwatch/examples/relay-receive/watch.py src/keepwatch/examples/relay-pull/config.toml src/keepwatch/reference/relay.md src/keepwatch/reference/recipes.md src/keepwatch/reference/observers.md src/keepwatch/reference/transfers.md tests/test_examples.py tests/test_docs.py tests/test_sweep_e2e.py
git commit -m "Sweep: relay-receive example, docs, end-to-end test"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
