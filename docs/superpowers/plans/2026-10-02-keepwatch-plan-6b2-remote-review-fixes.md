# keepwatch Plan 6b2 (Remote Watcher Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what the code review of Plan 6b found: a crash and reconnect loop on remote file names that are not UTF-8, unreadable files retried every second and never mentioned, a `heartbeat_timeout` that can be shorter than the heartbeat, full rescans every 2s even with inotify, `identity` keys offered after the agent's, and remote-watcher defaults that can drift from keepwatch's.

**Architecture:** The remote watcher skips non-UTF-8 names and backs off unreadable files (both noted once on stderr), and with inotify rescans only every `rescan` seconds (an option keepwatch now sends). keepwatch turns JSON events that cannot be encoded as UTF-8 into plain lines, adds `IdentitiesOnly=yes` with `identity`, and rejects a `heartbeat_timeout` that is not longer than `heartbeat`. A test pins the watcher's defaults to keepwatch's.

**Tech Stack:** as Plan 6b.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (sections 2.1, 3). Builds on Plans 6a, 6a2 and 6b (implemented, up to `af1521f`).

## Global Constraints

- All Global Constraints of Plans 6a and 6b apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests, stop and report on plan defects, attribution trailer).
- `remote_watcher.py` and `tests/remote_watcher_selftest.py` stay Python 3.6-compatible and ASCII (the 6b tests check this).

## Review Focus

- One remote file with a Latin-1 name must not stop every other file from being delivered. Tests: Task 1 `test_non_utf8_names_are_skipped_with_a_note`, Task 2 `test_events_that_cannot_be_utf8_become_lines`.
- A file the remote user cannot read must be mentioned once, not silently retried every second. Test: Task 1 `test_unreadable_file_is_noted_once`.
- `heartbeat_timeout = "30s"` with `heartbeat = "60s"` must be a config error, not a reconnect loop. Test: Task 2 `test_remote_heartbeat_timeout_must_exceed_heartbeat`.

---

### Task 1: The remote watcher

**Files:**
- Modify: `src/keepwatch/remote_watcher.py`, `tests/remote_watcher_selftest.py`, `tests/test_remote_watcher.py`

**Interfaces:**
- Produces: watcher option `rescan` (default 30): with inotify, a full rescan happens on every notification and every `rescan` seconds; without inotify every `interval`. `Watcher.note(message)` writes a message to stderr once. Unreadable files are retried every `rescan` seconds. `Running.stop()` (selftest) keeps the watcher's stderr in `Running.stderr_text` (bytes).

- [ ] **Step 1: Write the failing tests**

(a) `tests/remote_watcher_selftest.py`, class `Running`: in `__init__` add `self.stopped = False` and `self.stderr_text = b""` (before `self.process = ...`); replace `stop` with:

```python
    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(10)
        self.reader.join(5)
        self.stderr_text = self.process.stderr.read()
        self.process.stdout.close()
        self.process.stderr.close()
```

(b) Same file, append to `WatcherTests` (before `if __name__ == "__main__":`):

```python
    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "needs a filesystem that accepts any name bytes")
    def test_non_utf8_names_are_skipped_with_a_note(self):
        path = os.path.join(self.dir.encode("utf-8"), b"caf\xe9.gz")
        with open(path, "wb") as handle:
            handle.write(b"x")
        age(path)
        self.write("good.gz")
        running = self.start(heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["good.gz"])
        running.stop()
        self.assertEqual(running.stderr_text.count(b"not UTF-8"), 1)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a POSIX user that is not root"
    )
    def test_unreadable_file_is_noted_once(self):
        locked = self.write("locked.gz")
        os.chmod(locked, 0)
        self.write("ok.gz")
        running = self.start(heartbeat=0.5)
        try:
            names = running.file_names_for(3.0)
        finally:
            os.chmod(locked, 0o600)
        self.assertEqual(names, ["ok.gz"])
        running.stop()
        self.assertEqual(running.stderr_text.count(b"cannot read locked.gz"), 1)

    def test_rescan_option_is_accepted(self):
        self.write("a.gz")
        self.assertEqual(self.start(rescan=60).next_file()["name"], "a.gz")
```

(c) `tests/test_remote_watcher.py`: add `import importlib.util` to the imports and append:

```python
def test_the_watchers_defaults_match_keepwatch():
    from keepwatch import observers
    from keepwatch.config import ObserverConfig

    spec = importlib.util.spec_from_file_location("keepwatch_remote_watcher_copy", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    defaults = ObserverConfig(name="r", kind="remote_files")
    assert module.DEFAULTS["ignore"] == list(defaults.ignore)
    assert (module.DEFAULTS["settle"], module.DEFAULTS["interval"], module.DEFAULTS["heartbeat"]) == (
        defaults.settle,
        defaults.interval,
        defaults.heartbeat,
    )
    assert module.DEFAULTS["rescan"] == observers.FILES_RESCAN
    assert (module.SETTLE_STEP, module.MIN_RESCAN) == (observers.FILES_SETTLE_STEP, observers.FILES_MIN_RESCAN)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_remote_watcher.py -q`
Expected: FAIL — the non-UTF-8 and unreadable tests fail (no note, the Latin-1 name is reported), `rescan` is an unknown option, `DEFAULTS` has no `rescan`.

- [ ] **Step 3: Implement** — in `src/keepwatch/remote_watcher.py`:

(a) In `DEFAULTS`, add `"rescan": 30.0,` after `"heartbeat": 30.0,`. In the module docstring, after the "Options are one JSON object" paragraph, add the sentence: `With inotify the directory is rescanned on every notification and every "rescan" seconds; without it every "interval" seconds.`

(b) In `Watcher.__init__`, add after `self.reported = set()  # (path, size, mtime_ns)`:

```python
        self.unreadable = {}  # (path, size, mtime_ns) -> when reading it last failed
        self.noted = set()
```

and add this method to `Watcher` (before `scan`):

```python
    def note(self, message):
        """Tell keepwatch about a problem once (stderr lines become observer.output records)."""
        if message in self.noted:
            return
        self.noted.add(message)
        sys.stderr.write("keepwatch remote watcher: " + message + "\n")
        sys.stderr.flush()
```

(c) In `Watcher.scan`, replace

```python
        for raw in sorted(names):
            name = decode(raw)
            if not wanted(name, self.options):
                continue
```

with

```python
        for raw in sorted(names):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                # keepwatch could not name it to scp, log it or hand it to a hook as text.
                self.note("skipping a file whose name is not UTF-8: %r" % raw)
                continue
            if not wanted(name, self.options):
                continue
```

replace

```python
            if (path,) + key in self.reported:
                continue
```

with

```python
            if (path,) + key in self.reported:
                continue
            failed_at = self.unreadable.get((path,) + key)
            if failed_at is not None and now - failed_at < self.options["rescan"]:
                continue
```

replace

```python
                except OSError:
                    continue
                if (after.st_size, after.st_mtime_ns) != key:
```

with

```python
                except OSError as exc:
                    del self.candidates[path]
                    self.unreadable[(path,) + key] = now
                    self.note("cannot read %s: %s" % (name, exc.strerror or exc))
                    continue
                if (after.st_size, after.st_mtime_ns) != key:
```

and after the line `self.reported &= current` add:

```python
        for stale in set(self.unreadable) - current:
            del self.unreadable[stale]
```

(d) In `Watcher.run`, replace

```python
        interval = self.options["interval"]
        heartbeat = self.options["heartbeat"]
```

with

```python
        # With inotify, changes wake the loop at once; full rescans are only a safety net.
        interval = self.options["interval"] if inotify is None else max(self.options["interval"], self.options["rescan"])
        heartbeat = self.options["heartbeat"]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_remote_watcher.py -q` and `uv run python tests/remote_watcher_selftest.py -v`
Expected: all pass (the root-only skip does not apply locally).

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py tests/test_remote_watcher.py
git commit -m "Remote watcher: skip non-UTF-8 names, back off unreadable files, rescan rarely with inotify"
```

---

### Task 2: keepwatch's side

**Files:**
- Modify: `src/keepwatch/observers.py` (`parse_event`, `remote_command`, `watcher_options`), `src/keepwatch/config.py` (`OBSERVER_KEYS` identity doc, `_observer`), `src/keepwatch/reference/observers.md`
- Modify: `tests/test_observers.py`, `tests/test_remote_files_observer.py`, `tests/test_config_observe.py`, `tests/test_docs.py`

**Interfaces:**
- Produces: `parse_event` returns `{"line": <text>}` for a JSON object that cannot be encoded as UTF-8 (lone surrogates from `\udcxx` escapes); `remote_command` adds `-o IdentitiesOnly=yes` right after `-i <identity>`; `watcher_options` includes `"rescan": FILES_RESCAN`; config error when a remote_files `heartbeat_timeout` is not longer than its `heartbeat`.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_observers.py`, append:

```python
def test_events_that_cannot_be_utf8_become_lines():
    text = '{"event": "file", "name": "caf\\udce9.gz"}\n'
    assert parse_event(text) == {"line": text.rstrip("\n")}
```

(b) `tests/test_remote_files_observer.py`: in `test_remote_command_options`, replace

```python
        "-i",
        str(Path("/keys/id")),
        "--",
```

with

```python
        "-i",
        str(Path("/keys/id")),
        "-o",
        "IdentitiesOnly=yes",
        "--",
```

and in `test_watcher_source_carries_the_options`, add `"rescan": 30.0,` after `"heartbeat": 30.0,` in the expected dict.

(c) `tests/test_config_observe.py`, append:

```python
def test_remote_heartbeat_timeout_must_exceed_heartbeat(make_watch):
    [problem] = problems(make_watch, REMOTE + "heartbeat = '60s'\nheartbeat_timeout = '30s'\n")
    assert "'heartbeat_timeout' (30s) must be longer than 'heartbeat' (1m)" in problem


def test_remote_heartbeat_timeout_is_checked_against_the_default_heartbeat(make_watch):
    [problem] = problems(make_watch, REMOTE + "heartbeat_timeout = '20s'\n")
    assert "must be longer than 'heartbeat' (30s)" in problem
```

(d) `tests/test_docs.py`: append `"not valid UTF-8"` and `"IdentitiesOnly"` to `KEY_FACTS["observers"]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_observers.py tests/test_remote_files_observer.py tests/test_config_observe.py tests/test_docs.py -q`
Expected: the new and changed tests FAIL.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/observers.py`, `parse_event`: replace

```python
    if value.get("event") == "heartbeat":
        return None
    return value
```

with

```python
    if value.get("event") == "heartbeat":
        return None
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return {"line": line}  # lone surrogates (\udcxx escapes) could not be logged or handed to hooks
    return value
```

and in its docstring, after "other text as {"line": text}." add: `So is a JSON object that cannot be encoded as UTF-8.`

(b) Same file, `remote_command`: replace

```python
        argv += ["-i", str(config.identity)]
```

with

```python
        argv += ["-i", str(config.identity), "-o", "IdentitiesOnly=yes"]
```

and in `watcher_options` add `"rescan": FILES_RESCAN,` after `"heartbeat": config.heartbeat,`.

(c) `src/keepwatch/config.py`: in `OBSERVER_KEYS`, change the `identity` doc to:

```python
        "kind = remote_files: a private key file for ssh -i; ssh then offers only this key (IdentitiesOnly=yes). "
        "Relative to the watch directory; `~` and environment variables are expanded.",
```

In `_observer`, right before the line `for required in _OBSERVER_REQUIRED[kind]:` add:

```python
    if kind == "remote_files" and "heartbeat_timeout" in values:
        heartbeat = values.get("heartbeat", by_name["heartbeat"].default)
        if values["heartbeat_timeout"] <= heartbeat:
            collector.add(
                f"'heartbeat_timeout' ({format_duration(values['heartbeat_timeout'])}) must be longer than "
                f"'heartbeat' ({format_duration(heartbeat)}), or every quiet connection is dropped",
                key="heartbeat_timeout",
                table=table,
                topic="observers",
            )
```

and change `from keepwatch.durations import DurationError, parse_duration` to `from keepwatch.durations import DurationError, format_duration, parse_duration`.

(d) `src/keepwatch/reference/observers.md`, in the `remote_files` section, replace the bullet

```markdown
- **How it decides:** the same settle rules as `files`. It rescans every `interval` (2s), and at once when the host has inotify (used directly, without inotify-tools).
```

with

```markdown
- **How it decides:** the same settle rules as `files`. Where the host has inotify (used directly, without inotify-tools) it rescans at once on every change and every 30s as a safety net; without inotify it rescans every `interval` (2s). Files whose names are not valid UTF-8 are skipped, and files the remote user cannot read are retried every 30s; both are noted once in an `observer.output` record.
```

and in the "Requirements on the keepwatch host" list, item 1, append the sentence: `With \`identity\`, ssh offers only that key (IdentitiesOnly=yes), so a crowded agent cannot use up the server's login attempts.`

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_observers.py tests/test_remote_files_observer.py tests/test_config_observe.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the seven files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/config.py src/keepwatch/reference/observers.md tests/test_observers.py tests/test_remote_files_observer.py tests/test_config_observe.py tests/test_docs.py
git commit -m "remote_files: UTF-8-safe events, IdentitiesOnly with identity, heartbeat_timeout check"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
