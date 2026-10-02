# keepwatch Plan 7d (Sweep Third Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the sweep spec's section 10: the marker becomes part of a file's version (no timers, no rescans every second while a file waits), reads no longer trigger rescans, queued entries without a sha256 are never deleted, a timed-out delete keeps ssh's error text, and the receive example checks a file before moving it.

**Architecture:** Edits in `observers.py` (FilesObserver), `recipes/pull.py`, `ctx.py`, the `relay-receive` example and the docs.

**Tech Stack:** as Plan 7c.

**Spec:** `docs/superpowers/specs/2026-10-02-keepwatch-sweep-design.md` (section 10). Builds on Plan 7c (implemented, up to `45c7f7f`).

## Global Constraints

- All Global Constraints of Plan 7 apply (branch `keepwatch-sweep`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, alphabetical imports, `git diff --stat` shows only the task's files, no formatter, do not push, stdlib-only for remote/transfer/recipes, stop and report on plan defects, attribution trailer).

## Review Focus

- A file with no marker must not cause scans every second. Test: Task 1 `test_a_file_without_a_marker_costs_no_rescans`.
- A queued entry without a sha256 must never reach the delete helper. Test: Task 2 `test_an_entry_without_a_sha256_is_never_deleted`.
- A timed-out delete keeps both the results printed before the deadline and ssh's stderr. Test: Task 2 `test_a_timed_out_delete_keeps_the_results_it_had`.

---

### Task 1: The files observer: the marker is part of the version

**Files:**
- Modify: `src/keepwatch/observers.py`, `src/keepwatch/reference/observers.md`, `tests/test_files_observer.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_files_observer.py`:

```python
def test_a_file_without_a_marker_costs_no_rescans(tmp_path, make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "FILES_RESCAN", 600.0)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    scans = []
    scan = observers.FilesObserver._scan
    monkeypatch.setattr(observers.FilesObserver, "_scan", lambda self: scans.append(1) or scan(self))
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        time.sleep(2.5)  # settled (settle is 1s) with no marker
        before = len(scans)
        time.sleep(3.0)
        assert len(scans) == before  # held until the file or its marker changes, not re-checked every second
        write_marker(inbox, "a.tar.gz")  # its marker is still noticed
        assert wait_for(lambda: events, timeout=8.0)
    finally:
        stop(observer)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_files_observer.py -k costs_no_rescans -q`
Expected: FAIL — `assert 6 == 3` or similar (the file waiting for its marker is rescanned every second).

- [ ] **Step 3: Implement** — in `src/keepwatch/observers.py`:

(a) Delete the line `FILES_MARKER_WAIT = 60.0  # …` (the whole line).

(b) In `class _Poke`, replace the docstring and `on_any_event` so it reads:

```python
    """Any filesystem change just asks for a rescan (reads do not: hashing a file must not trigger another scan)."""

    def __init__(self, changed: threading.Event) -> None:
        super().__init__()
        self._changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in ("opened", "closed_no_write"):
            self._changed.set()
```

(c) Directly above `class FilesObserver(Observer):` add (followed by two blank lines):

```python
Version = tuple[int, int, tuple[int, int] | None]  # a file's (size, mtime_ns) and its marker's, if any
```

(d) In `FilesObserver.__init__`, replace the two lines

```python
        self._mismatched: set[tuple[str, int, int, int, int]] = set()
        self._waiting: dict[str, float] = {}
```

with

```python
        self._held: set[tuple[str, Version]] = set()  # settled without a matching marker
```

(e) Replace the whole method `_verified` with these two methods:

```python
    def _marker_version(self, path: str) -> tuple[int, int] | None:
        """The marker's (size, mtime_ns); None without marker = sha256 or while it is missing."""
        if self.config.marker != "sha256":
            return None
        try:
            info = os.stat(path + ".sha256")
        except OSError:
            return None
        return (info.st_size, info.st_mtime_ns)

    def _verified(self, path: str, marker: tuple[int, int] | None) -> dict[str, Any] | None:
        """{} without markers; with marker = sha256 the event fields, or None when the marker is missing or wrong.

        Raises OSError when the file or its marker cannot be read (it is checked again after another settle period).
        """
        if self.config.marker != "sha256":
            return {}
        if marker is None:
            return None
        marker_path = Path(path + ".sha256")
        fields = marker_path.read_text(encoding="utf-8", errors="replace").split()
        expected = fields[0].lower() if fields else ""
        actual = sha256_file(path)
        if re.fullmatch(r"[0-9a-f]{64}", expected) and actual == expected:
            return {"sha256": actual, "marker": str(marker_path)}
        self._sink(
            make_record(
                "observer.output",
                level="WARNING",
                **self._tag(),
                stream="marker",
                text=f"{Path(path).name}: {marker_path.name} does not match (sha256 {actual}); waiting for a new upload",
            )
        )
        return None
```

(f) Replace the whole method `_watch` with:

```python
    def _watch(self, changed: threading.Event, idle: float) -> str:
        """Scan, report settled files, wait for a notification or a timeout; repeat until stopped."""
        settle = self.config.settle
        candidates: dict[str, tuple[Version, float]] = {}
        last_scan = 0.0
        while not self._stop.is_set():
            self._stop.wait(max(last_scan + FILES_MIN_RESCAN - time.monotonic(), 0.0))
            if self._stop.is_set():
                break
            changed.clear()
            last_scan = time.monotonic()
            try:
                found = self._scan()
            except OSError as exc:
                return f"cannot scan {self.config.path}: {exc.strerror or exc}"
            now, wall = time.monotonic(), time.time()
            current, versions = set(), set()
            for path, info in sorted(found.items()):
                key = (info.st_size, info.st_mtime_ns)
                current.add((path, *key))
                if (path, *key) in self._reported:
                    continue
                marker = self._marker_version(path)
                version = (*key, marker)  # a marker arriving or changing starts a new settle period
                versions.add((path, version))
                if (path, version) in self._held:
                    continue  # no matching marker: checked again when the file or its marker changes
                seen = candidates.get(path)
                if seen is None or seen[0] != version:
                    seen = candidates[path] = (version, now)
                newest = max(info.st_mtime_ns, marker[1] if marker else 0) / 1e9
                if now - seen[1] < settle or wall - newest < settle:
                    continue
                del candidates[path]
                try:
                    extra = self._verified(path, marker)
                except OSError:
                    continue  # unreadable for now: a candidate again at the next scan
                if extra is None:
                    self._held.add((path, version))
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
            for path in set(candidates) - set(found):
                del candidates[path]
            self._reported &= current
            self._held &= versions
            deadline = time.monotonic() + (FILES_SETTLE_STEP if candidates else idle)
            while not self._stop.is_set() and not changed.is_set() and time.monotonic() < deadline:
                changed.wait(min(0.2, max(deadline - time.monotonic(), 0.0)))
        return "stopped"
```

(g) `src/keepwatch/reference/observers.md`: after the sentence ``A mismatch is logged once (an `observer.output` WARNING) and the file is reported after a corrected upload.`` add (same paragraph, one space before it):

```text
The marker is part of the file's version: when it appears or changes, the pair settles again (`settle`) and is checked then. A file without a matching marker costs nothing while it waits; it is checked again when the file or its marker changes (a filesystem notification, or the rescan every 30 seconds where notifications are missed, as on some network shares).
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_files_observer.py tests/test_sweep_e2e.py tests/test_relay_e2e.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/observers.py src/keepwatch/reference/observers.md tests/test_files_observer.py
git commit -m "files observer: the marker is part of a file's version; reads do not trigger rescans"
```

---

### Task 2: Deletes without a sha256, timeout errors, the receive example

**Files:**
- Modify: `src/keepwatch/recipes/pull.py`, `src/keepwatch/ctx.py`, `src/keepwatch/examples/relay-receive/watch.py`, `src/keepwatch/reference/recipes.md`
- Modify: `tests/test_recipe_pull_delete.py`, `tests/test_examples.py`

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_recipe_pull_delete.py`: in `test_a_refused_delete_is_kept_and_not_retried`, change `ledger(xdg, "to_delete").add(pull.queue_key(key, None))` to

```python
    ledger(xdg, "to_delete").add(pull.queue_key(key, hashlib.sha256(b"x").hexdigest()))
```

and append:

```python
def test_an_entry_without_a_sha256_is_never_deleted(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "old.tar.gz"
    source.write_bytes(b"old")
    key = pull.event_key(file_event(source))
    ledger(xdg, "pulled").add(key)
    ledger(xdg, "to_delete").add(pull.queue_key(key, None))  # no verified sha256 (an earlier build queued these)
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n"), [], records=records)
    assert not report.failed and source.exists()
    assert key in ledger(xdg, "kept") and len(ledger(xdg, "to_delete")) == 0
    messages = [r["message"] for r in records if r["event"] == "plugin.log"]
    assert any("no verified sha256 was recorded" in m for m in messages)


def test_a_timed_out_delete_keeps_the_results_it_had(tmp_path, caplog):
    from keepwatch import Ctx
    from keepwatch.recipes import pull

    slow = tmp_path / "slow_ssh.py"
    slow.write_text(
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'event': 'deleted', 'index': 0, 'path': '/out/a.tar.gz'}), flush=True)\n"
        "print('ssh: still talking to the host', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    settings = {
        "remote": "u@h",
        "remote_dir": "/out",
        "port": None,
        "identity": None,
        "known_hosts": None,
        "ssh_options": [],
        "ssh_command": [PY, str(slow)],
        "remote_python": "python3",
    }
    ctx = Ctx(
        watch="sweep",
        hook="on_true",
        poll_id="p1",
        condition=True,
        payload=None,
        settings=settings,
        watch_dir=tmp_path,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        deadline=time.time() + 3,
    )
    entries = [pull.queue_key(f"/out/{name}|4|1.5", "0" * 64) for name in ("a.tar.gz", "b.tar.gz")]
    for entry in entries:
        ctx.ledger("to_delete").add(entry)
    with caplog.at_level("WARNING"):
        pull.delete_remote(ctx, entries)
    assert list(ctx.ledger("to_delete")) == [entries[1]]  # a.tar.gz was deleted before the deadline
    assert "/out/a.tar.gz|4|1.5" in ctx.ledger("skipped")
    assert "ssh: still talking to the host" in caplog.text and "timed out" in caplog.text
```

(b) `tests/test_examples.py`: append:

```python
def test_relay_receive_example_leaves_a_file_that_changed_since_its_report(xdg):
    install(xdg, "relay-receive")
    incoming = Path.home() / "incoming"
    incoming.mkdir(parents=True)
    digest = hashlib.sha256(b"payload").hexdigest()
    (incoming / "a.tar.gz").write_bytes(b"payload, then more")  # a new upload under the same name has begun
    (incoming / "a.tar.gz.sha256").write_text(f"{digest}  a.tar.gz\n", encoding="utf-8")
    event = {
        "event": "file",
        "path": str(incoming / "a.tar.gz"),
        "name": "a.tar.gz",
        "size": len(b"payload"),
        "mtime": 1.5,
        "sha256": digest,
        "marker": str(incoming / "a.tar.gz.sha256"),
    }
    result = run("poll", "relay-receive", "--events", json.dumps([event]), "--json")
    assert json.loads(result.output)["polls"][0]["failed"] is False, result.output
    assert (incoming / "a.tar.gz").exists() and not (incoming / "done" / "a.tar.gz").exists()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_examples.py -q`
Expected: FAIL — `old.tar.gz` is deleted; the timeout warning lacks ssh's stderr; the changed file is moved to `done/`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/ctx.py`: rename `_as_text` to `as_text` (its definition and its two uses in `run`) and give it the docstring `"""Captured output as text (subprocess leaves bytes in TimeoutExpired even in text mode)."""` as its first line.

(b) `src/keepwatch/recipes/pull.py`: change `from keepwatch.ctx import Ctx` to `from keepwatch.ctx import Ctx, as_text`; in the module docstring change `Files pulled while delete_remote was off have no recorded sha256 and are never deleted.` to `Files pulled while delete_remote was off have no recorded sha256 and are never deleted (an entry without one goes to "kept").`; replace the whole function `delete_remote` with:

```python
def delete_remote(ctx: Ctx, entries: list[str]) -> None:
    """Delete these pulled files from the source host in one ssh call; problems are logged and retried."""
    if not entries:
        return
    settings = ctx.settings
    pending, pulled, kept = ctx.ledger(TO_DELETE), ctx.ledger(LEDGER), ctx.ledger(KEPT)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    remote = settings["remote"]
    for entry in [entry for entry in entries if entry.endswith(f"|{UNKNOWN_SHA}")]:
        key, item = split_queue_key(entry)
        pending.discard(entry)
        kept.add(key)
        name = item["path"].rsplit("/", 1)[-1]
        ctx.log.warning("not deleting %s on %s: no verified sha256 was recorded for it (delete it by hand)", name, remote)
    entries = [entry for entry in entries if not entry.endswith(f"|{UNKNOWN_SHA}")]
    if not entries:
        return
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
    except subprocess.TimeoutExpired as exc:
        # Keep what the helper reported before the deadline: files it already deleted are not retried.
        stdout = as_text(exc.stdout)
        stderr = as_text(exc.stderr) + "\ntimed out (for large files raise action_timeout: the helper re-reads each one)"
    except OSError as exc:
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
        elif event == "refused":
            pending.discard(entry)
            kept.add(key)
            ctx.log.warning("not deleting %s on %s: %s", name, remote, result.get("reason"))
        else:
            pending.add(entry)  # refreshes its stamp: retried after delete_retry
            reason = result.get("reason") if result else (" | ".join(stderr.strip().splitlines()[-3:]) or "no answer")
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, reason)
```

(c) `src/keepwatch/examples/relay-receive/watch.py`: replace the whole function `on_true` with:

```python
def on_true(ctx):
    handled = ctx.ledger("handled")
    done = Path(ctx.settings["done_dir"]).expanduser()
    done.mkdir(parents=True, exist_ok=True)
    for item in ctx.payload:
        if key(item) in handled:
            continue
        try:
            info = Path(item["path"]).stat()
        except OSError:
            continue  # moved or deleted since it was reported
        if (info.st_size, info.st_mtime) != (item["size"], item["mtime"]):
            ctx.log.warning("%s changed since it was reported; left for its next report", Path(item["path"]).name)
            continue
        for path in (item["path"], item["marker"]):
            if Path(path).exists():
                shutil.move(path, str(done / Path(path).name))
        handled.add(key(item))
        ctx.log.info("received %s", Path(item["path"]).name, extra={"sha256": item["sha256"]})
```

(d) `src/keepwatch/reference/recipes.md`, the `delete_remote` bullet: change `files pulled before stay on the source host.` to ``files pulled before stay on the source host, and a queued entry without a sha256 is moved to `kept` with a WARNING.``

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_pull_delete.py tests/test_recipe_pull.py tests/test_sweep_e2e.py tests/test_examples.py tests/test_ctx.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/recipes/pull.py src/keepwatch/ctx.py src/keepwatch/examples/relay-receive/watch.py src/keepwatch/reference/recipes.md tests/test_recipe_pull_delete.py tests/test_examples.py
git commit -m "Never delete without a recorded sha256; keep ssh errors on a delete timeout; receive example checks before moving"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
