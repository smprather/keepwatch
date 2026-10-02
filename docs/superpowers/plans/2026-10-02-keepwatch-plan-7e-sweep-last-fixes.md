# keepwatch Plan 7e (Sweep Last Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The delete helper itself refuses a file without a sha256; an unreadable file stays a candidate; one parse of queued entries; the receive example logs a vanished file; two tests made exact.

**Architecture:** Small edits in `remote_watcher.py`, `observers.py`, `recipes/pull.py`, the `relay-receive` example and three test files.

**Tech Stack:** as Plan 7d.

**Spec:** `docs/superpowers/specs/2026-10-02-keepwatch-sweep-design.md` (section 11). Builds on Plan 7d (implemented, up to `9a1a589`).

## Global Constraints

- All Global Constraints of Plan 7 apply (branch `keepwatch-sweep`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before the commit, `git diff --stat` shows only the listed files, no formatter, do not push, Python 3.6 and ASCII for `remote_watcher.py` and its selftest, stop and report on plan defects, attribution trailer).

## Review Focus

- The helper never deletes without a sha256, whoever calls it. Test: `test_delete_refuses_without_a_sha256` (selftest).

---

### Task 1: All fixes

**Files:**
- Modify: `src/keepwatch/remote_watcher.py`, `src/keepwatch/observers.py`, `src/keepwatch/recipes/pull.py`, `src/keepwatch/examples/relay-receive/watch.py`
- Modify: `tests/remote_watcher_selftest.py`, `tests/test_files_observer.py`, `tests/test_examples.py`

- [ ] **Step 1: Update the tests**

(a) `tests/remote_watcher_selftest.py`: replace the method `item` with

```python
    def item(self, path):
        info = os.stat(path)
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        return {"path": os.path.abspath(path), "size": info.st_size, "mtime": info.st_mtime, "sha256": digest}
```

and add after `test_delete_with_a_matching_sha256`:

```python
    def test_delete_refuses_without_a_sha256(self):
        path = self.write("a.gz")
        item = self.item(path)
        del item["sha256"]
        result = self.delete([item])[item["path"]]
        self.assertEqual(result["event"], "refused")
        self.assertIn("sha256", result["reason"])
        self.assertTrue(os.path.exists(path))
```

(b) `tests/test_files_observer.py`, `test_a_file_without_a_marker_costs_no_rescans`: replace the line `        time.sleep(2.5)  # settled (settle is 1s) with no marker` with

```python
        assert wait_for(lambda: observer._held, timeout=8.0)  # settled with no marker: held
```

(c) `tests/test_examples.py`, `test_relay_receive_example_leaves_a_file_that_changed_since_its_report`: replace the line `        "mtime": 1.5,` with

```python
        "mtime": (incoming / "a.tar.gz").stat().st_mtime,  # the same mtime: only the size gives it away
```

- [ ] **Step 2: Run the selftest to verify it fails**

Run: `uv run python tests/remote_watcher_selftest.py`
Expected: FAIL — `test_delete_refuses_without_a_sha256`: `'deleted' != 'refused'`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/remote_watcher.py`, `delete_one`: change the docstring to `"""Delete one pulled file if it is still exactly what was pulled: size, mtime and sha256 (required)."""` and replace

```python
    expected = item.get("sha256")
    if expected:
        try:
            if sha256_of(raw) != expected:
                return {"event": "changed", "reason": "its content differs from the pulled copy"}
        except OSError as exc:
            return {"event": "failed", "reason": exc.strerror or str(exc)}
```

with

```python
    expected = item.get("sha256")
    if not expected:
        return {"event": "refused", "reason": "no sha256 given: the content cannot be checked"}
    try:
        if sha256_of(raw) != expected:
            return {"event": "changed", "reason": "its content differs from the pulled copy"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
```

(b) `src/keepwatch/observers.py`, `FilesObserver._watch`: replace

```python
                except OSError:
                    continue  # unreadable for now: a candidate again at the next scan
```

with

```python
                except OSError:
                    candidates[path] = (version, now)  # unreadable for now (locked on Windows): again after settle
                    continue
```

(c) `src/keepwatch/recipes/pull.py`, `delete_remote`: replace

```python
    for entry in [entry for entry in entries if entry.endswith(f"|{UNKNOWN_SHA}")]:
        key, item = split_queue_key(entry)
        pending.discard(entry)
        kept.add(key)
        name = item["path"].rsplit("/", 1)[-1]
        ctx.log.warning("not deleting %s on %s: no verified sha256 was recorded for it (delete it by hand)", name, remote)
    entries = [entry for entry in entries if not entry.endswith(f"|{UNKNOWN_SHA}")]
    if not entries:
        return
```

with

```python
    queued = [(entry, *split_queue_key(entry)) for entry in entries]
    for entry, key, item in queued:
        if "sha256" not in item:
            pending.discard(entry)
            kept.add(key)
            name = item["path"].rsplit("/", 1)[-1]
            ctx.log.warning("not deleting %s on %s: no verified sha256 was recorded for it (delete it by hand)", name, remote)
    queued = [(entry, key, item) for entry, key, item in queued if "sha256" in item]
    if not queued:
        return
```

then delete the line `    pairs = [split_queue_key(entry) for entry in entries]`, change `"files": [item for _, item in pairs]` to `"files": [item for _, _, item in queued]`, and change `    for index, (entry, (key, item)) in enumerate(zip(entries, pairs)):` to

```python
    for index, (entry, key, item) in enumerate(queued):
```

(d) `src/keepwatch/examples/relay-receive/watch.py`, `on_true`: replace

```python
        except OSError:
            continue  # moved or deleted since it was reported
```

with

```python
        except OSError:
            ctx.log.warning("%s is gone since it was reported", Path(item["path"]).name)
            continue
```

- [ ] **Step 4: Run the tests**

Run: `uv run python tests/remote_watcher_selftest.py` and `uv run pytest tests/test_remote_watcher.py tests/test_recipe_pull_delete.py tests/test_sweep_e2e.py tests/test_files_observer.py tests/test_examples.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the seven files. Then:

```bash
git add src/keepwatch/remote_watcher.py src/keepwatch/observers.py src/keepwatch/recipes/pull.py src/keepwatch/examples/relay-receive/watch.py tests/remote_watcher_selftest.py tests/test_files_observer.py tests/test_examples.py
git commit -m "The delete helper requires a sha256; unreadable files stay candidates; one parse of queued deletes"
```

---

## After the last task

Report: the commit, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
