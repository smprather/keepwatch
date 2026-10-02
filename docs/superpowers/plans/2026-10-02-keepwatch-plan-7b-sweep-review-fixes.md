# keepwatch Plan 7b (Sweep Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the sweep spec's section 8 revisions: re-hash before deleting, results by position, backfill and bounded ledgers, stale events that never fail the poll, deletes that never mask a pull error, push back to two scp runs, prompt marker re-checks, a correct receive example, shared ssh options, and accurate docs.

**Architecture:** The remote helper's delete mode verifies an optional `sha256` and tags results with `index`. `transfer.pull` raises `TransferMismatch` (a `TransferFailed`) for size/sha256 mismatches; `push` sends the marker in a second run. The pull recipe is rewritten around four ledgers (`pulled`, `to_delete` with the verified sha256, `skipped` expiring after 7 days, `kept`). `remote.with_known_hosts` builds the ssh options once for the observer and the deletes.

**Tech Stack:** as Plan 7.

**Spec:** `docs/superpowers/specs/2026-10-02-keepwatch-sweep-design.md` (sections 2-4 and the revisions in section 8). Builds on Plan 7 (implemented, up to `a3353aa`).

## Global Constraints

- All Global Constraints of Plan 7 apply (branch `keepwatch-sweep`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, alphabetical imports, `git diff --stat` shows only the task's files, no formatter, do not push, Python 3.6/ASCII for the remote helper, stdlib-only for remote/transfer/recipes, stop and report on plan defects, attribution trailer).

## Review Focus

- A source file rewritten with the same size and modification time must not be deleted. Test: Task 1 `test_delete_keeps_a_same_size_same_mtime_rewrite`.
- Files pulled before `delete_remote` was switched on must be deleted too. Test: Task 3 `test_files_pulled_before_delete_remote_are_deleted_too`.
- An event for a file that changed before it was pulled must not fail the poll. Test: Task 3 `test_a_file_changed_before_its_pull_does_not_fail_the_poll`.
- A failed data upload must never be followed by its marker. Test: Task 2 `test_a_failed_upload_sends_no_marker`.

---

### Task 1: The delete helper checks content and reports positions

**Files:**
- Modify: `src/keepwatch/remote_watcher.py`, `tests/remote_watcher_selftest.py`

**Interfaces:**
- Produces: delete items may carry `"sha256"`; the helper hashes the file before unlinking and answers `changed` on a mismatch. Every result line carries `"index"` (the item's position in `files`).

- [ ] **Step 1: Write the failing tests** — in `tests/remote_watcher_selftest.py`, add to `WatcherTests` (before `if __name__ == "__main__":`):

```python
    def test_delete_keeps_a_same_size_same_mtime_rewrite(self):
        path = self.write("a.gz", b"one")
        before = os.stat(path)
        pulled = self.item(path)
        pulled["sha256"] = hashlib.sha256(b"one").hexdigest()
        with open(path, "wb") as handle:
            handle.write(b"two")  # same size
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the exact same mtime, as cp -p leaves it
        self.assertEqual(os.stat(path).st_mtime, pulled["mtime"])
        self.assertEqual(self.delete([pulled])[pulled["path"]]["event"], "changed")
        self.assertTrue(os.path.exists(path))

    def test_delete_with_a_matching_sha256(self):
        path = self.write("a.gz", b"one")
        item = self.item(path)
        item["sha256"] = hashlib.sha256(b"one").hexdigest()
        self.assertEqual(self.delete([item])[item["path"]]["event"], "deleted")

    def test_delete_results_carry_their_index(self):
        first = self.write("a.gz")
        second = os.path.join(self.dir, "never.gz")
        options = {"mode": "delete", "dir": self.dir, "files": [self.item(first), {"path": second, "size": 1, "mtime": 1.5}]}
        code, out, err = self.run_once(options)
        lines = [json.loads(line) for line in out.decode("ascii").splitlines()]
        self.assertEqual([(line["index"], line["event"]) for line in lines[:-1]], [(0, "deleted"), (1, "gone")])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run python tests/remote_watcher_selftest.py`
Expected: FAIL — the rewrite is deleted; no `index`.

- [ ] **Step 3: Implement** — in `src/keepwatch/remote_watcher.py`:

(a) Add before `delete_one`:

```python
def file_sha256(raw):
    digest = hashlib.sha256()
    with open(raw, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
```

(b) In `delete_one`, replace

```python
    if info.st_size != item.get("size") or info.st_mtime != float(item.get("mtime", -1)):
        return {"event": "changed"}
```

with

```python
    if info.st_size != item.get("size") or info.st_mtime != float(item.get("mtime", -1)):
        return {"event": "changed"}
    expected = item.get("sha256")
    if expected:
        try:
            if file_sha256(raw) != expected:
                return {"event": "changed", "reason": "its content differs from the pulled copy"}
        except OSError as exc:
            return {"event": "failed", "reason": exc.strerror or str(exc)}
```

and change its docstring to `"""Delete one pulled file if it is still exactly what was pulled (size, mtime and, when given, sha256)."""`.

(c) In `delete_files`, replace

```python
    for item in options["files"]:
        try:
            result = delete_one(item, directory)
        except Exception as exc:  # keep going: one odd entry must not stop the others
            result = {"event": "failed", "reason": str(exc)}
        result["path"] = item.get("path")
```

with

```python
    for index, item in enumerate(options["files"]):
        try:
            result = delete_one(item, directory)
        except Exception as exc:  # keep going: one odd entry must not stop the others
            result = {"event": "failed", "reason": str(exc)}
        result["path"] = item.get("path")
        result["index"] = index
```

and extend the module docstring's delete-mode paragraph: after `whose size and mtime still match` add `(and sha256, when the item carries one)`, and after `"reason"?` add `, "index"`.

- [ ] **Step 4: Run the tests**

Run: `uv run python tests/remote_watcher_selftest.py` and `uv run pytest tests/test_remote_watcher.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py
git commit -m "Delete helper: re-hash before deleting; results carry their index"
```

---

### Task 2: `TransferMismatch`; the marker only after the data

**Files:**
- Modify: `src/keepwatch/transfer.py`
- Modify: `tests/test_transfer_scp.py`

**Interfaces:**
- Produces: `transfer.TransferMismatch(TransferFailed)` raised by `pull` for a size or sha256 mismatch; `push` sends the marker in a second scp run, only after the data succeeded. `copy(..., extra_sources=...)` stays.

- [ ] **Step 1: Write the failing tests** — in `tests/test_transfer_scp.py`:

(a) Change `assert len(calls) == 1  # file and marker in one scp run` back to `assert len(calls) == 2  # the file, then its marker`.

(b) Replace the whole `test_push_sends_file_and_marker_in_one_scp` with:

```python
def test_a_failed_upload_sends_no_marker(tmp_path, server, ssh_config):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    calls = []
    with pytest.raises(CommandFailed):
        push(local, REMOTE + "no/such/dir/", options=key_options(server, ssh_config), report=lambda *call: calls.append(call))
    assert len(calls) == 1  # the data failed, so its marker was never sent
    assert not list(server.root.rglob("*.sha256"))


def test_pull_mismatches_raise_transfer_mismatch(tmp_path, server, ssh_config):
    from keepwatch.transfer import TransferMismatch

    (server.root / "b.gz").write_bytes(b"remote data")
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", size=99, options=key_options(server, ssh_config))
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", sha256="0" * 64, options=key_options(server, ssh_config))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_transfer_scp.py -q`
Expected: FAIL — one scp run per push; no `TransferMismatch`.

- [ ] **Step 3: Implement** — in `src/keepwatch/transfer.py`:

(a) After the `TransferFailed` class add:

```python
class TransferMismatch(TransferFailed):
    """A pulled file's size or sha256 differs from what was expected (usually: it changed after it was reported)."""
```

(b) In `pull`, change `raise TransferFailed(f"pulled {actual_size} bytes of …` and `raise TransferFailed(f"sha256 of the pulled …` to `raise TransferMismatch(...)` (same messages).

(c) In `push`, replace

```python
    with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
        marker_file = Path(work) / f"{local.name}.sha256"
        marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
        # One scp run (one login): scp sends its sources in order, so the data arrives before its marker.
        copy(local, directory, extra_sources=[marker_file], options=options, report=report)
    return target.scp_arg()
```

with

```python
    copy(local, target, options=options, report=report)
    with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
        marker_file = Path(work) / f"{local.name}.sha256"
        marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
        # A second run, only after the data succeeded: one scp run carries on past a failed source, which
        # could put the marker beside a truncated file.
        copy(marker_file, directory.child(marker_file.name), options=options, report=report)
    return target.scp_arg()
```

and change `push`'s docstring to `"""Upload a file into remote_dir, then (marker="sha256") NAME.sha256 in sha256sum format once the data succeeded. Returns the remote path."""`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_recipe_push.py tests/test_cli_kit.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/transfer.py tests/test_transfer_scp.py
git commit -m "Transfers: TransferMismatch; the marker only after the data succeeded"
```

---

### Task 3: The pull recipe's ledgers, backfill and failure handling

**Files:**
- Modify: `src/keepwatch/remote.py` (`with_known_hosts`), `src/keepwatch/config.py` (`_recipe_observers` uses it), `src/keepwatch/recipes/pull.py` (rewritten)
- Modify: `tests/test_recipe_pull_delete.py`, `tests/test_remote_module.py`

**Interfaces:**
- Produces: `remote.with_known_hosts(ssh_options: Sequence[str], known_hosts: str | os.PathLike[str] | None) -> list[str]`; pull recipe ledgers `pulled`, `to_delete` (entries `"<key>|<sha256 or ->"`), `skipped` (expires after 7 days), `kept`; functions `event_key`, `transfer_options`, `queue_key`, `split_queue_key`, `due_deletions`, `delete_remote`, `check`, `on_true`.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_remote_module.py`, append:

```python
def test_with_known_hosts():
    assert remote.with_known_hosts(["-o", "X=1"], None) == ["-o", "X=1"]
    assert remote.with_known_hosts([], "C:/k h/kh") == ["-o", 'UserKnownHostsFile="C:/k h/kh"']
```

(b) `tests/test_recipe_pull_delete.py`:
- in `test_a_verified_pull_deletes_the_source`, change `assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 1` to

```python
    assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 0  # done: out of the skip list
    assert len(ledger(xdg, "skipped")) == 1
```

- replace the whole `test_a_changed_source_is_kept` with:

```python
def test_a_file_changed_before_its_pull_does_not_fail_the_poll(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    event = file_event(source)
    source.write_bytes(b"changed after the observer reported it")
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [event], records=records)
    assert not report.failed
    assert source.exists() and len(ledger(xdg, "pulled")) == 0 and len(ledger(xdg, "to_delete")) == 0
    assert any("changed on u@127.0.0.1 after it was reported" in r["message"] for r in records if r["event"] == "plugin.log")


def test_a_changed_source_is_kept(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    before = source.stat()
    event = file_event(source)
    key = pull.event_key(event)
    source.write_bytes(b"datb")  # same size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))  # the exact same mtime: only the content differs
    assert source.stat().st_mtime == event["mtime"]
    ledger(xdg, "pulled").add(key)
    ledger(xdg, "to_delete").add(pull.queue_key(key, event["sha256"]))
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n"), [], records=records)
    assert not report.failed and source.read_bytes() == b"datb"
    assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 0
    assert any("changed on u@127.0.0.1 since it was pulled" in r["message"] for r in records if r["event"] == "plugin.log")


def test_files_pulled_before_delete_remote_are_deleted_too(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "old.tar.gz"
    source.write_bytes(b"old")
    ledger(xdg, "pulled").add(pull.event_key(file_event(source)))  # pulled while delete_remote was off
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [])
    assert report.outcome is Outcome.TRUE and not report.failed
    assert not source.exists()


def test_a_refused_delete_is_kept_and_not_retried(linux1, make_watch, tmp_path, xdg):
    if os.name == "nt":
        pytest.skip("symlinks")
    target = tmp_path / "elsewhere.tar.gz"
    target.write_bytes(b"x")
    link = linux1.root / "link.tar.gz"
    link.symlink_to(target)
    info = os.lstat(link)
    key = f"{link}|{info.st_size}|{info.st_mtime!r}"
    ledger(xdg, "pulled").add(key)
    watch = sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n")
    first = poll(xdg, watch, [])
    assert not first.failed and link.is_symlink() and target.exists()
    assert key in ledger(xdg, "kept") and key in ledger(xdg, "pulled")
    assert poll(xdg, watch, [], state=first.after).outcome is Outcome.FALSE  # nothing left to do
```

(c) `tests/test_recipe_pull.py`: replace the whole `test_pull_recipe_does_not_record_a_failed_pull` (a mismatch no longer fails the poll; it means the file changed after it was reported) with:

```python
def test_pull_recipe_does_not_record_a_mismatched_pull(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    report = engine(xdg, records).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"data", sha="0" * 64)])
    assert not report.failed
    assert not list((tmp_path / "stage").iterdir())
    assert len(Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")) == 0
    assert any("after it was reported" in r["message"] for r in records if r["event"] == "plugin.log")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_remote_module.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/remote.py`: add `from keepwatch.transfer import known_hosts_option` to the imports and append:

```python
def with_known_hosts(ssh_options: Sequence[str], known_hosts: str | os.PathLike[str] | None) -> list[str]:
    """ssh_options plus the known_hosts check: what the remote_files observer and the pull recipe's deletes use."""
    return [*ssh_options, *(known_hosts_option(known_hosts) if known_hosts else [])]
```

(b) `src/keepwatch/config.py`, `_recipe_observers`: replace

```python
    ssh_options = list(settings["ssh_options"])
    if settings["known_hosts"]:
        ssh_options += known_hosts_option(settings["known_hosts"])
```

with `ssh_options = with_known_hosts(settings["ssh_options"], settings["known_hosts"])`; add `from keepwatch.remote import with_known_hosts` to the imports and drop `known_hosts_option` from the `keepwatch.transfer` import if nothing else uses it.

(c) Replace the whole of `src/keepwatch/recipes/pull.py` with:

```python
"""recipe = "pull": copy settled files from a remote directory into a local staging folder.

The `remote_files` observer reports settled remote files (with their sha256). check() answers TRUE with the
files not yet pulled; on_true() pulls each one (verified) and records it in the ledger "pulled", which also tells
the observer which files to skip when it reconnects.

With delete_remote, each verified file is queued in "to_delete" (with the sha256 it was verified with) and
deleted from the source host by keepwatch's remote helper, which first checks that it is still exactly that
file. Done keys leave "pulled" for "skipped" (forgotten after 7 days; stale events for them are ignored);
refused ones go to "kept". See keepwatch docs recipes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

from keepwatch.ctx import Ctx
from keepwatch.remote import ssh_argv, watcher_source, with_known_hosts
from keepwatch.transfer import TransferMismatch

LEDGER = "pulled"
TO_DELETE = "to_delete"
SKIPPED = "skipped"
KEPT = "kept"
SKIP_FOR = "7d"
UNKNOWN_SHA = "-"


def event_key(event: dict[str, Any]) -> str:
    """The ledger key of a remote file: "<path>|<size>|<mtime!r>" (the remote watcher builds the same text)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    options = {name: settings[name] for name in ("port", "identity", "known_hosts") if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def queue_key(key: str, sha256: str | None) -> str:
    """A to_delete entry: the pulled file's key plus the sha256 it was verified with ("-" when unknown)."""
    return f"{key}|{sha256 or UNKNOWN_SHA}"


def split_queue_key(entry: str) -> tuple[str, dict[str, Any]]:
    """(the pulled key, the helper's {"path", "size", "mtime", "sha256"?}) for a to_delete entry."""
    key, sha256 = entry.rsplit("|", 1)
    path, size, mtime = key.rsplit("|", 2)
    item: dict[str, Any] = {"path": path, "size": int(size), "mtime": float(mtime)}
    if sha256 != UNKNOWN_SHA:
        item["sha256"] = sha256
    return key, item


def due_deletions(ctx: Ctx) -> list[str]:
    """Queued deletions due for an attempt, plus pulled files never queued (pulled before delete_remote, or a crash)."""
    settings = ctx.settings
    if not settings["delete_remote"]:
        return []
    pending = ctx.ledger(TO_DELETE)
    now = datetime.now(timezone.utc)
    due = [entry for entry in pending if (now - pending.added_at(entry)).total_seconds() >= settings["delete_retry"]]
    queued = {entry.rsplit("|", 1)[0] for entry in pending}
    kept = ctx.ledger(KEPT)
    due += [queue_key(key, None) for key in ctx.ledger(LEDGER) if key not in queued and key not in kept]
    return due


def delete_remote(ctx: Ctx, entries: list[str]) -> None:
    """Delete these pulled files from the source host in one ssh call; problems are logged and retried."""
    if not entries:
        return
    settings = ctx.settings
    pending, pulled, kept = ctx.ledger(TO_DELETE), ctx.ledger(LEDGER), ctx.ledger(KEPT)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    remote = settings["remote"]
    argv = ssh_argv(
        remote,
        port=settings["port"],
        identity=settings["identity"],
        ssh_options=with_known_hosts(settings["ssh_options"], settings["known_hosts"]),
        ssh_command=settings["ssh_command"],
        remote_python=settings["remote_python"],
    )
    pairs = [split_queue_key(entry) for entry in entries]
    options = {"mode": "delete", "dir": settings["remote_dir"], "files": [item for _, item in pairs]}
    try:
        completed = ctx.run(argv, input=watcher_source(options), check=False)
        stdout, stderr = completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr = "", str(exc)
    results: dict[int, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("index"), int):
            results[message["index"]] = message
    for index, (entry, (key, item)) in enumerate(zip(entries, pairs)):
        name = item["path"].rsplit("/", 1)[-1]
        result = results.get(index)
        event = result["event"] if result else None
        if event in ("deleted", "gone", "changed"):
            pending.discard(entry)
            pulled.discard(key)
            skipped.add(key)
            if event == "deleted":
                ctx.log.info("deleted %s from %s", name, remote)
            elif event == "gone":
                ctx.log.info("%s was already gone from %s", name, remote)
            else:
                ctx.log.warning("%s changed on %s since it was pulled; kept there", name, remote)
        elif event == "refused":
            pending.discard(entry)
            kept.add(key)
            ctx.log.warning("not deleting %s on %s: %s", name, remote, result.get("reason"))
        else:
            pending.add(entry)  # refreshes its stamp: retried after delete_retry
            reason = result.get("reason") if result else (" | ".join(stderr.strip().splitlines()[-3:]) or "no answer")
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, reason)


def check(ctx: Ctx) -> tuple[bool, list[dict[str, Any]]]:
    pulled = ctx.ledger(LEDGER)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    new: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in ctx.events:
        if event.get("event") != "file":
            continue
        key = event_key(event)
        if key in pulled or key in skipped or key in seen:
            continue
        seen.add(key)
        new.append(
            {"path": event["path"], "name": event["name"], "size": event["size"], "sha256": event.get("sha256"), "key": key}
        )
    return bool(new) or bool(due_deletions(ctx)), new


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    queue = ctx.ledger(TO_DELETE) if settings["delete_remote"] else None
    options = transfer_options(settings)
    fresh: list[str] = []
    error: Exception | None = None
    try:
        for item in ctx.payload:
            if item["key"] in pulled or item["key"] in skipped:
                continue
            sha256 = item["sha256"] if settings["checksum"] else None
            try:
                final = ctx.transfer.pull(
                    f"{settings['remote']}:{item['path']}",
                    settings["local_dir"],
                    size=item["size"],
                    sha256=sha256,
                    on_conflict=settings["on_conflict"],
                    **options,
                )
            except TransferMismatch as exc:
                # The file changed after it was reported; its new version comes as a new event.
                ctx.log.warning("%s changed on %s after it was reported (%s); not pulled", item["name"], settings["remote"], exc)
                continue
            pulled.add(item["key"])
            if queue is not None:
                entry = queue_key(item["key"], sha256)
                queue.add(entry)
                fresh.append(entry)
            ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
    except Exception as exc:  # deletes below still run for what was pulled; the error is raised after them
        error = exc
    if queue is not None and (fresh or error is None):
        entries = set(fresh) if error is not None else set(fresh) | set(due_deletions(ctx))
        try:
            delete_remote(ctx, sorted(entries))
        except Exception as exc:
            if error is None:
                raise
            ctx.log.warning("deleting from %s failed as well: %s", settings["remote"], exc)
    if error is not None:
        raise error
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_recipe_pull.py tests/test_remote_module.py tests/test_config_recipe.py tests/test_relay_e2e.py tests/test_sweep_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/remote.py src/keepwatch/config.py src/keepwatch/recipes/pull.py tests/test_recipe_pull_delete.py tests/test_remote_module.py tests/test_recipe_pull.py
git commit -m "pull recipe: content-checked deletes, backfill, bounded ledgers, stale events never fail"
```

---

### Task 4: Marker cadence, the receive example, docs

**Files:**
- Modify: `src/keepwatch/observers.py` (`_watch`), `src/keepwatch/examples/relay-receive/watch.py`
- Modify: `src/keepwatch/reference/recipes.md`, `src/keepwatch/reference/relay.md`, `src/keepwatch/reference/transfers.md`
- Modify: `tests/test_files_observer.py`, `tests/test_docs.py`

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_files_observer.py`, append:

```python
def test_a_marker_that_arrives_late_is_noticed_within_seconds(tmp_path, make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "FILES_RESCAN", 600.0)  # no idle rescan to rescue it
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n", settle="3s")
    observer.start()
    try:
        time.sleep(1.5)  # the data is now a candidate; its settle check comes at about 3s
        marker = inbox / "a.tar.gz.sha256"
        marker.write_text(f"{hashlib.sha256(b'payload').hexdigest()}  a.tar.gz\n", encoding="utf-8")
        # Fresh: still unsettled when the data's check comes, and no filesystem event follows it.
        assert wait_for(lambda: events, timeout=10.0)
    finally:
        stop(observer)
```

(b) `tests/test_docs.py`: append `"interval"` and `"the same size and modification time"` to `KEY_FACTS["recipes"]`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_files_observer.py tests/test_docs.py -q`
Expected: FAIL (the late marker waits for the 600s rescan; missing doc facts).

- [ ] **Step 3: Implement**

(a) `src/keepwatch/observers.py`, in `FilesObserver._watch`, replace

```python
                    del candidates[path]  # settled; without a matching marker it settles again and is re-checked
                    extra = self._verified(path, info)
                    if extra is None:
                        continue
```

with

```python
                    extra = self._verified(path, info)
                    if extra is None:
                        if any(version[:3] == (path, *key) for version in self._mismatched):
                            del candidates[path]  # a wrong marker: wait for a new upload (an event or the rescan)
                        continue  # no marker yet: stay a candidate, re-checked every FILES_SETTLE_STEP
                    del candidates[path]
```

(b) `src/keepwatch/examples/relay-receive/watch.py`: replace the `check` and `on_true` functions with:

```python
def check(ctx):
    handled = ctx.ledger("handled")
    new = [event for event in ctx.events if event.get("event") == "file" and key(event) not in handled]
    return bool(new), [{"path": event["path"], "marker": event["marker"], "sha256": event["sha256"]} for event in new]


def on_true(ctx):
    handled = ctx.ledger("handled")
    done = Path(ctx.settings["done_dir"]).expanduser()
    done.mkdir(parents=True, exist_ok=True)
    for item in ctx.payload:
        if key(item) in handled:
            continue
        for path in (item["path"], item["marker"]):
            if Path(path).exists():
                shutil.move(path, str(done / Path(path).name))
        handled.add(key(item))
        ctx.log.info("received %s", Path(item["path"]).name, extra={"sha256": item["sha256"]})


def key(event):
    """One arrival: its path and content (the same bytes arriving again under any name is a new arrival)."""
    return f"{event['path']}|{event['sha256']}"
```

(c) `recipes.md`, pull section: replace the bullet beginning `- The same ledger is the observer's \`skip_ledger\`` with:

```markdown
- The same ledger is the observer's `skip_ledger`: after a reconnect, files already pulled are neither hashed nor reported again. Without `delete_remote`, files stay on the source host and a pulled file is pulled again only if its size or modification time changes.
```

and replace the `delete_remote` bullet (beginning `- With \`delete_remote = true\``) with:

```markdown
- With `delete_remote = true`, each verified file is also queued in the ledger `to_delete` (with the sha256 it was verified with) and deleted from the source host by keepwatch's remote helper, in one ssh call per poll. The helper deletes only a regular file directly in `remote_dir` that still has the same size and modification time **and the same content** (it re-hashes it, so even a rewrite with the same size and modification time is kept). Files pulled before the option was switched on are deleted too. A delete problem never fails the poll; a failed delete is retried at the first poll after `delete_retry`, which without new files comes with the watch's `interval`. Needs `checksum = true`. A file replaced in the instant between the helper's check and its delete cannot be told apart; producers should write under a temporary name and rename, as most tools do.
```

(d) `relay.md`, in "Sweep: delete from the source": replace the last sentence `linux1 then only ever holds files that have not reached the hub yet.` with `linux1 then holds only files that have not reached the hub yet, plus any it refused to delete (a symlink, say) or that changed after they were pulled; both are logged.`

(e) `transfers.md`: in the Operations table's `push` row, change the "What it does" cell to: ``Uploads `NAME`, then, once that succeeded, `NAME.sha256` in `sha256sum` format. Returns / prints the remote path.``; and in the "Errors" section add the bullet ``- A size or sha256 mismatch on a pull raises `keepwatch.transfer.TransferMismatch` (a `TransferFailed`): usually the remote file changed after it was reported.``

Also update `tests/test_docs.py` `KEY_FACTS["transfers"]`: replace `"one scp run"` with `"TransferMismatch"`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_files_observer.py tests/test_docs.py tests/test_examples.py tests/test_sweep_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the seven files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/examples/relay-receive/watch.py src/keepwatch/reference/recipes.md src/keepwatch/reference/relay.md src/keepwatch/reference/transfers.md tests/test_files_observer.py tests/test_docs.py
git commit -m "Prompt marker re-checks, a receive example keyed by path and content, accurate sweep docs"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
