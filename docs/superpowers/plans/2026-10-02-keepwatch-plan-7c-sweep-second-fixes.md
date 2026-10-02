# keepwatch Plan 7c (Sweep Second Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the sweep spec's section 9: no backfill (only content-verified files are deleted), `changed` keeps the new version moving, timeouts keep partial delete results, marker waits are capped, and four cleanups.

**Architecture:** Edits in `recipes/pull.py`, `observers.py`, `remote_watcher.py`, `transfer.py`, `remote.py`, `config.py`, the `relay-receive` example and the docs.

**Tech Stack:** as Plan 7b.

**Spec:** `docs/superpowers/specs/2026-10-02-keepwatch-sweep-design.md` (section 9). Builds on Plan 7b (implemented, up to `dad0cfa`).

## Global Constraints

- All Global Constraints of Plan 7 apply (branch `keepwatch-sweep`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, alphabetical imports, `git diff --stat` shows only the task's files, no formatter, do not push, Python 3.6/ASCII for the remote helper, stdlib-only for remote/transfer/recipes, stop and report on plan defects, attribution trailer).

## Review Focus

- A file pulled while `delete_remote` was off (no recorded sha256) must never be deleted. Test: Task 1 `test_files_pulled_before_delete_remote_are_left_alone`.
- After a `changed` result the new content must be pullable at its next report. Test: Task 1 `test_a_changed_source_is_kept` (the key is in neither `pulled` nor `skipped`).
- A re-sent identical file must be handled again on linux2. Test: Task 2 `test_relay_receive_example_processes_a_verified_arrival` (event carries size and mtime).

---

### Task 1: The pull recipe

**Files:**
- Modify: `src/keepwatch/recipes/pull.py`, `tests/test_recipe_pull_delete.py`

- [ ] **Step 1: Update the tests** — in `tests/test_recipe_pull_delete.py`:

(a) Replace the whole `test_files_pulled_before_delete_remote_are_deleted_too` with:

```python
def test_files_pulled_before_delete_remote_are_left_alone(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "old.tar.gz"
    source.write_bytes(b"old")
    ledger(xdg, "pulled").add(pull.event_key(file_event(source)))  # pulled while delete_remote was off
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [])
    assert report.outcome is Outcome.FALSE and not report.failed
    assert source.exists()  # no sha256 was recorded for it, so it is not deleted
```

(b) In `test_a_refused_delete_is_kept_and_not_retried`, add `from keepwatch.recipes import pull` as the first line of the body (followed by a blank line), and right after `ledger(xdg, "pulled").add(key)` add:

```python
    ledger(xdg, "to_delete").add(pull.queue_key(key, None))
```

(c) In `test_a_changed_source_is_kept`, after `assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 0` add:

```python
    assert key not in ledger(xdg, "skipped")  # the new content is pulled at its next report
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_recipe_pull_delete.py -q`
Expected: FAIL — the old file is deleted (backfill); the changed key is in `skipped`.

- [ ] **Step 3: Implement** — in `src/keepwatch/recipes/pull.py`:

(a) Module docstring: replace `Done keys leave "pulled" for "skipped" (forgotten after 7 days; stale events for them are ignored);
refused ones go to "kept". See keepwatch docs recipes.` with `Deleted or gone keys leave "pulled" for "skipped" (forgotten after 7 days; stale events for them are ignored); changed ones just leave "pulled" (the new version is pulled when reported); refused ones go to "kept". Files pulled while delete_remote was off have no recorded sha256 and are never deleted. See keepwatch docs recipes.`

(b) Replace `due_deletions` with:

```python
def due_deletions(ctx: Ctx) -> list[str]:
    """Queued deletions due for an attempt (only files pulled and verified while delete_remote was on are queued)."""
    settings = ctx.settings
    if not settings["delete_remote"]:
        return []
    pending = ctx.ledger(TO_DELETE)
    now = datetime.now(timezone.utc)
    return [entry for entry in pending if (now - pending.added_at(entry)).total_seconds() >= settings["delete_retry"]]
```

(c) In `delete_remote`, replace

```python
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr = "", str(exc)
```

with

```python
    except subprocess.TimeoutExpired as exc:
        # Keep what the helper reported before the deadline: files it already deleted are not retried.
        output = exc.stdout or b""
        stdout = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
        stderr = "timed out (raise action_timeout: the helper re-reads each file to check it)"
    except OSError as exc:
        stdout, stderr = "", str(exc)
```

and replace

```python
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
```

with

```python
        if event in ("deleted", "gone"):
            pending.discard(entry)
            pulled.discard(key)
            skipped.add(key)
            if event == "deleted":
                ctx.log.info("deleted %s from %s", name, remote)
            else:
                ctx.log.info("%s was already gone from %s", name, remote)
        elif event == "changed":
            pending.discard(entry)
            pulled.discard(key)  # not skipped: the new content is pulled when it is reported
            ctx.log.warning("%s changed on %s since it was pulled; kept there", name, remote)
```

(d) In `on_true`, replace

```python
            pulled.add(item["key"])
            if queue is not None:
                entry = queue_key(item["key"], sha256)
                queue.add(entry)
                fresh.append(entry)
```

with

```python
            if queue is not None:  # queued first: a crash before the next line still gets the file deleted
                entry = queue_key(item["key"], sha256)
                queue.add(entry)
                fresh.append(entry)
            pulled.add(item["key"])
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_recipe_pull.py tests/test_sweep_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/recipes/pull.py tests/test_recipe_pull_delete.py
git commit -m "pull recipe: delete only content-verified files; changed keeps the new version; partial results on timeout"
```

---

### Task 2: Marker waits, the receive example, cleanups, docs

**Files:**
- Modify: `src/keepwatch/observers.py`, `src/keepwatch/remote_watcher.py`, `src/keepwatch/transfer.py`, `src/keepwatch/remote.py`, `src/keepwatch/config.py`, `src/keepwatch/recipes/pull.py`, `src/keepwatch/examples/relay-receive/watch.py`, `src/keepwatch/reference/recipes.md`
- Modify: `tests/test_transfer.py`, `tests/test_remote_module.py`, `tests/test_examples.py`

- [ ] **Step 1: Update the tests**

(a) `tests/test_transfer.py`: delete `test_extra_sources_follow_the_first`, add `with_known_hosts` to the `keepwatch.transfer` import (alphabetical order) and append:

```python
def test_with_known_hosts():
    assert with_known_hosts(["-o", "X=1"], None) == ["-o", "X=1"]
    assert with_known_hosts([], "C:/k h/kh") == ["-o", 'UserKnownHostsFile="C:/k h/kh"']
```

(b) `tests/test_remote_module.py`: delete `test_with_known_hosts`, and in `test_remote_is_stdlib_only` remove `"keepwatch"` from the allowed set.

(c) `tests/test_examples.py`, `test_relay_receive_example_processes_a_verified_arrival`: replace the `event = {...}` line with

```python
    info = (incoming / "a.tar.gz").stat()
    event = {
        "event": "file",
        "path": str(incoming / "a.tar.gz"),
        "name": "a.tar.gz",
        "size": info.st_size,
        "mtime": info.st_mtime,
        "sha256": digest,
        "marker": str(incoming / "a.tar.gz.sha256"),
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_transfer.py tests/test_remote_module.py -q`
Expected: FAIL — `cannot import name 'with_known_hosts'` from `keepwatch.transfer`; `remote.py` imports keepwatch.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/transfer.py`: append after `known_hosts_option`:

```python
def with_known_hosts(ssh_options: Sequence[str], known_hosts: str | os.PathLike[str] | None) -> list[str]:
    """ssh_options plus the known_hosts check: what the remote_files observer and the pull recipe's deletes use."""
    return [*ssh_options, *(known_hosts_option(known_hosts) if known_hosts else [])]
```

and remove `extra_sources` again: in `scp_argv` delete the parameter `extra_sources: Sequence[Endpoint] = (),` and change `sources = [_operand(end, classic=classic, source=True) for end in (source, *extra_sources)]` / `return [*argv, "--", *sources, _operand(destination, classic=classic, source=False)]` to

```python
    return [*argv, "--", _operand(source, classic=classic, source=True), _operand(destination, classic=classic, source=False)]
```

in `copy` delete the parameter `extra_sources: …`, the line `extra = [_endpoint(item) for item in extra_sources]`, the `, extra_sources=extra` argument, and change the docstring's first sentence back to `Copy one file with scp.`

(b) `src/keepwatch/remote.py`: delete `with_known_hosts`, and delete the import `from keepwatch.transfer import known_hosts_option` together with the blank line above it (so `from typing import Any` is followed by one blank line and then `AUTO_PYTHON = (`, as before Plan 7b).

(c) `src/keepwatch/config.py` and `src/keepwatch/recipes/pull.py`: import `with_known_hosts` from `keepwatch.transfer` instead of `keepwatch.remote` (keep the other names they import from each module; alphabetical order).

(d) `src/keepwatch/remote_watcher.py`: change `sha256_of` to

```python
def sha256_of(path, output=None, heartbeat=None):
    """The file's sha256, read in chunks; with an output, heartbeats keep flowing while a large file is hashed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            if output is not None:
                output.heartbeat_if_due(heartbeat)
    return digest.hexdigest()
```

delete the whole `file_sha256` function, and in `delete_one` change `if file_sha256(raw) != expected:` to `if sha256_of(raw) != expected:`.

(e) `src/keepwatch/observers.py`: add after `FILES_MIN_RESCAN = 0.5 …`:

```python
FILES_MARKER_WAIT = 60.0  # re-check a file waiting for its marker every second for this long, then on events/rescans
```

in `FilesObserver.__init__` add `self._waiting: dict[str, float] = {}`; in `_watch` replace

```python
                    extra = self._verified(path, info)
                    if extra is None:
                        if any(version[:3] == (path, *key) for version in self._mismatched):
                            del candidates[path]  # a wrong marker: wait for a new upload (an event or the rescan)
                        continue  # no marker yet: stay a candidate, re-checked every FILES_SETTLE_STEP
                    del candidates[path]
```

with

```python
                    extra = self._verified(path, info)
                    if extra is None:
                        mismatched = any(version[:3] == (path, *key) for version in self._mismatched)
                        waited = now - self._waiting.setdefault(path, now)
                        if mismatched or waited >= FILES_MARKER_WAIT:
                            del candidates[path]  # wait for a filesystem event or the rescan instead
                        continue  # no marker yet: stay a candidate, re-checked every FILES_SETTLE_STEP
                    self._waiting.pop(path, None)
                    del candidates[path]
```

and after `for path in set(candidates) - set(found):` / `del candidates[path]` add:

```python
            for path in set(self._waiting) - set(found):
                del self._waiting[path]
```

(f) `src/keepwatch/examples/relay-receive/watch.py`: in `check`, change the payload items to also carry `"size": event["size"], "mtime": event["mtime"]`, and change `key` to:

```python
def key(event):
    """One arrival: path, size, mtime and content (the same bytes arriving again later is a new arrival)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}|{event['sha256']}"
```

(g) `src/keepwatch/reference/recipes.md`, the `delete_remote` bullet: replace `Files pulled before the option was switched on are deleted too.` with `Only files pulled while the option is on are deleted (they carry the verified sha256 the check needs); files pulled before stay on the source host. A rewrite that keeps both size and modification time is invisible to the watcher (as to any watcher that goes by modification times).`

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_remote_module.py tests/test_files_observer.py tests/test_examples.py tests/test_remote_watcher.py tests/test_recipe_pull_delete.py tests/test_sweep_e2e.py tests/test_docs.py -q` and `uv run python tests/remote_watcher_selftest.py`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the eleven files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/remote_watcher.py src/keepwatch/transfer.py src/keepwatch/remote.py src/keepwatch/config.py src/keepwatch/recipes/pull.py src/keepwatch/examples/relay-receive/watch.py src/keepwatch/reference/recipes.md tests/test_transfer.py tests/test_remote_module.py tests/test_examples.py
git commit -m "Capped marker waits, receive example keyed by arrival, one hash loop, no unused multi-source scp"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
