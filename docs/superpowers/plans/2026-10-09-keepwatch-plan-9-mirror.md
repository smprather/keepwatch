# keepwatch Plan 9 (Mirror) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The mirror: kit transfers carry the source mtime (#12), a check may read a persistent checkpoint it cannot write (#13), and `recipe = "mirror"` syncs a recursive tree in place over ssh/scp — copy what changed, propagate deletions, one live connection for the remote side (#11).

**Architecture:** Three releases. `.18` adds `preserve_mtime` to `ScpOptions` (scp `-p`, sftp `-p`) and tolerates a `set times` failure when the data provably landed. `.19` adds `ctx.checkpoint(name)`, a JSON document under the watch's data dir, readable in check and writable only in actions, atomic, following the ledger's discipline. `.20` extends `remote_watcher.py` with a `mode: "tree"` (recursive walk against an expected state from the checkpoint, chunked `files`/`dirs`/`gone` deltas, reconnect on a dropped event) and adds `src/keepwatch/recipes/mirror.py`: the destination is mirror-owned and never scanned, the source side is walked (push, locally) or watched over one live ssh (pull), and the checkpoint is promoted in bounded batches as files land.

**Tech Stack:** as existing keepwatch: Python 3.12 stdlib in the worker (recipes and `transfer.py` stay stdlib-only), `remote_watcher.py` stays Python 3.6-compatible ASCII stdlib, pytest, ruff, ty, OpenSSH `scp`/`sftp`/`ssh`.

**Spec:** `docs/superpowers/specs/2026-10-09-keepwatch-mirror-design.md` — the plan argues from the spec, so read both. Builds on release 2026.10.17 (main, clean at b433845).

## Global Constraints

- Branch `main`, clean tree. Stage files explicitly (`git add <paths>`); never `git add -A`. Do not push, tag or publish a release without asking.
- Before every commit run, without a pipe: `uv run pytest -q` and `uv run ruff check --no-cache src tests`; for `src` changes also `uv run ty check src`. `git diff --stat` shows only the task's files.
- Every commit message ends with the attribution trailer `Co-Authored-By: pi (deepseek-v4.1-flash via ollama-cloud)` (no address: none is configured; say if you want a different form).
- Imports at the top of each module, alphabetical; 4-space indentation; no formatter.
- `remote_watcher.py` and its tests stay Python 3.6-compatible and ASCII (`tests/test_remote_watcher.py` enforces both — extend those guards for tree mode). `remote.py`, `transfer.py` and `recipes/*.py` stay stdlib-only.
- Tests are portable unless marked; TOML paths via `toml_path(...)`; commands built from the current Python. End-to-end tests use the existing ssh fixture in `tests/conftest.py` (see `tests/test_remote_files_observer.py`).
- Version bumps change `pyproject.toml` and `src/keepwatch/__init__.py` (`__version__`) together.
- If a step is wrong, stop and report the diagnosis instead of improvising.
- Where the implementation differs from what an issue asked for, that issue gets a comment saying so as its release
  lands (section *Issue-log notes* holds the list): the deviation, why it was taken, and what it means for the
  issue's original intent. Do not close an issue in silence; the creator weighs in after the work is done.

## Review Focus

- A mirror must never delete from an incomplete view — an unreadable subtree, a walk that raised, or an inferred absence. Tests: Task 8 `test_an_unreadable_subtree_is_not_a_deletion`, Task 10 `test_no_events_never_deletes`.
- A transfer that dies mid-file must not poison the destination: the entry is not promoted, so the next diff copies it again. Test: Task 9 `test_a_failed_push_is_not_promoted`.
- With `delete = false` a `gone` path must still leave the checkpoint, or the watcher re-reports it on every reconnect. Test: Task 8 `test_a_gone_is_still_work_with_delete_off`.
- A destination that refuses times must warn, not fail, and a pull must repair the local mtime from what the watcher reported. Tests: Task 1 `test_a_set_times_failure_after_the_data_is_a_warning`, Task 10 `test_pull_repairs_a_local_mtime`.
- Windows: every batched remote command line stays bounded (≤100 paths), so a 5000-directory tree never approaches the ~32k CreateProcess limit. Test: Task 9 `test_mkdir_batches_are_bounded`.

---

## Release 2026.10.18 — kit carries mtimes (#12)

### Task 1: `preserve_mtime` and the `set times` tolerance

**Files:**
- Modify: `src/keepwatch/transfer.py` (`ScpOptions` ~278-317, `scp_argv` ~332-374, `sftp_argv` ~235-253, `copy` ~632-658, `push` ~778-830, `Transfer.copy/pull/push` ~893-933, the `Transfer` docstring ~845-859)
- Test: `tests/test_transfer.py`, `tests/test_transfer_scp.py`

**Interfaces:**
- Produces: `ScpOptions.preserve_mtime: bool = False`; `_set_times_only(stderr: str) -> bool`; `copy(source, destination, *, options=ScpOptions(), report=None, warn: Callable[[str], None] | None = None) -> None` (warn only for the tolerated case).
- Consumes: `ScpOptions.__post_init__` validation style; `_refused_overwrite` as the precedent for a tolerated stderr shape; `argv_for(...)` in `tests/test_transfer.py`.

- [ ] **Step 1: Write the failing argv tests** — `tests/test_transfer.py` (use the file's existing `argv_for` helper):

```python
def test_preserve_mtime_adds_dash_p():
    assert "-p" in argv_for("a.txt", "me@h:/tmp/", preserve_mtime=True)
    assert "-p" not in argv_for("a.txt", "me@h:/tmp/")


def test_preserve_mtime_adds_dash_p_to_the_sftp_protocol_too():
    assert "-p" in argv_for("a.txt", "me@h:/tmp/", protocol="sftp", preserve_mtime=True)


def test_the_resume_command_preserves_times_with_dash_p(tmp_path):
    # sftp_argv(host, options, batch): -p appears before the host when preserve_mtime is set
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_transfer.py -k preserve_mtime -v` — Expected: FAIL (`-p` absent / unexpected keyword).

- [ ] **Step 3: Implement** — add the field to `ScpOptions`; in `scp_argv()` append `-p` (next to the other flags, before `"--"`) when set; in `sftp_argv()` append `-p` after the shared options when set. Do not reorder existing flags: the default argv stays byte-for-byte identical.

- [ ] **Step 4: Write the failing tolerance tests** — `tests/test_transfer_scp.py` (the file's fake-scp harness writes a destination file and exits with a chosen status/stderr):

```python
def test_a_set_times_failure_after_the_data_is_a_warning(tmp_path):
    # fake scp writes the destination, prints "local set times: Input/output error", exits 1
    warnings = []
    transfer.copy(local, "me@h:/tmp/x", options=ScpOptions(preserve_mtime=True), warn=warnings.append)
    assert (out / "x").read_text() == "data"
    assert warnings and "times" in warnings[0]


def test_a_set_times_failure_without_preserve_mtime_still_fails(tmp_path):
    with pytest.raises(CommandFailed):
        transfer.copy(local, "me@h:/tmp/x", options=ScpOptions())


def test_a_failed_transfer_whose_stderr_mentions_times_still_fails(tmp_path):
    # fake scp prints the same text but writes nothing: still CommandFailed
```

- [ ] **Step 5: Run them to verify they fail** — `uv run pytest tests/test_transfer_scp.py -k set_times -v`

- [ ] **Step 6: Implement the tolerance** — `_set_times_only(stderr)` matches `set times:` case-insensitively (`local set times: Input/output error`, `set times: Operation not permitted`). In `copy()`, where a non-zero return currently raises: if `options.preserve_mtime and _set_times_only(stderr)`, call `warn` with the file's name, what happened and the consequence (the copy carries the transfer time), then return. Nothing else changes: a data failure prints a different stderr and still raises. Thread `warn` through `Transfer.copy` (`warn=self._warn`), `push()`'s two `copy()` calls and `pull()`'s `copy()` call.

- [ ] **Step 7: Write the pull test** — the data landed but times did not, and the part verifies:

```python
def test_pull_tolerates_set_times_when_the_part_verifies(tmp_path):
    final = transfer.pull("me@h:/tmp/x", str(out), size=4, options=ScpOptions(preserve_mtime=True))
    assert final.read_text() == "data"


def test_pull_still_fails_when_the_part_does_not_verify(tmp_path):
    with pytest.raises(TransferMismatch):
        transfer.pull("me@h:/tmp/x", str(out), size=999, options=ScpOptions(preserve_mtime=True))
```

- [ ] **Step 8: Run the tests, then the gates**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py -q`, then `uv run pytest -q`, `uv run ruff check --no-cache src tests`, `uv run ty check src`.

- [ ] **Step 9: Document `preserve_mtime` in the `Transfer` class docstring** (it is rendered by `keepwatch docs ctx`) and add the section to `src/keepwatch/reference/transfers.md` under `## Options`: what `-p` carries (times and mode bits), the `set times` shapes and when they are warnings, and that the caller who knows a remote mtime repairs a pull with `os.utime`.

- [ ] **Step 10: Commit**

```bash
git add src/keepwatch/transfer.py src/keepwatch/reference/transfers.md tests/test_transfer.py tests/test_transfer_scp.py
git commit -m "Transfers carry mtimes (scp -p), and a refused set times is a warning"
```

### Task 2: `--preserve-mtime` on the kit commands

**Files:**
- Modify: `src/keepwatch/cli.py` (`_scp_options` ~940-961, `_scp` ~964-981)
- Test: `tests/test_cli_kit.py`

**Interfaces:**
- Consumes: `ScpOptions.preserve_mtime` from Task 1.
- Produces: `--preserve-mtime` on `keepwatch kit copy/pull/push`.

- [ ] **Step 1: Write the failing test** — follow the file's pattern of monkeypatching the transfer function and capturing the options:

```python
def test_kit_copy_passes_preserve_mtime(monkeypatch):
    seen = {}
    monkeypatch.setattr(transfer, "copy", lambda *a, **kw: seen.update(options=kw["options"]))
    runner.invoke(cli, ["kit", "copy", "a", "me@h:/tmp/a", "--preserve-mtime"])
    assert seen["options"].preserve_mtime is True
    # and without the flag the default stays False
```

- [ ] **Step 2: Run it to verify it fails** — `uv run pytest tests/test_cli_kit.py -k preserve_mtime -v`

- [ ] **Step 3: Implement** — add the `click.option("--preserve-mtime", is_flag=True, help=...)` to `_scp_options`, thread it through `_scp(...)` into `ScpOptions`.

- [ ] **Step 4: Run the tests and the gates** — `uv run pytest tests/test_cli_kit.py -q`, then the full gates.

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/cli.py tests/test_cli_kit.py
git commit -m "keepwatch kit --preserve-mtime"
```

### Task 3: Release 2026.10.18

- [ ] **Step 1:** Bump `pyproject.toml` and `src/keepwatch/__init__.py` to `2026.10.18`.
- [ ] **Step 2:** Run the full gates (`uv run pytest -q`, ruff, ty).
- [ ] **Step 3:** Commit and tag locally; ask before pushing.

```bash
git add pyproject.toml src/keepwatch/__init__.py
git commit -m "Release 2026.10.18: kit transfers carry mtimes (scp -p)"
git tag v2026.10.18
```

- [ ] **Step 4:** Post the #12 deviations from *Issue-log notes* as an issue comment (release, flag, deviations), then close #12.

---

## Release 2026.10.19 — `ctx.checkpoint` (#13)

### Task 4: `Checkpoint` in `ctx.py`

**Files:**
- Modify: `src/keepwatch/ctx.py` (beside `ledger_file`/`Ledger` ~66-160, and `Ctx.ledger` ~310)
- Modify: `src/keepwatch/reference/ctx.md` (the prose companion to the generated member list)
- Test: `tests/test_ctx.py`

**Interfaces:**
- Produces: `checkpoint_file(data_dir: Path, name: str) -> Path`; `class Checkpoint` with `__init__(path, *, writable=True)`, `.value`, `.save(document)`, `.clear()`; `CheckpointReadOnly`, `CheckpointCorrupt`; `Ctx.checkpoint(name: str) -> Checkpoint`.
- Consumes: the `Ledger` discipline exactly — `LEDGER_NAME` pattern (rename to `STATE_NAME` and keep `LEDGER_NAME = STATE_NAME` if anything imports it), `_save`'s tempfile+fsync+`platform.replace` shape, `writable=self.hook != "check"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_ctx.py`, using its `make_ctx` helper:

```python
def test_checkpoint_round_trips_a_document(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.checkpoint("mirror").save({"files": {"a": [1, 2.5]}})
    assert make_ctx(tmp_path).checkpoint("mirror").value == {"files": {"a": [1, 2.5]}}


def test_an_unsaved_checkpoint_is_none(tmp_path):
    assert make_ctx(tmp_path).checkpoint("mirror").value is None


def test_ctx_checkpoint_is_read_only_in_check(tmp_path):
    ctx = make_ctx(tmp_path, hook="check")
    with pytest.raises(CheckpointReadOnly):
        ctx.checkpoint("mirror").save({})
    with pytest.raises(CheckpointReadOnly):
        ctx.checkpoint("mirror").clear()


def test_a_corrupt_checkpoint_names_the_file(tmp_path):
    # write "{" into data/checkpoints/mirror.json; CheckpointCorrupt mentions the path


def test_a_bad_checkpoint_name_is_refused(tmp_path):
    with pytest.raises(ValueError):
        make_ctx(tmp_path).checkpoint("a/b")


def test_checkpoint_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    # after save, data/checkpoints holds only mirror.json; a save that raises mid-write cleans its temp
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_ctx.py -k checkpoint -v`

- [ ] **Step 3: Implement** `checkpoint_file` (`data_dir / "checkpoints" / f"{name}.json"`), `Checkpoint` (`_load` at construction like `Ledger`, `value` property, `save` with `json.dump(..., indent=1, sort_keys=True)` through the atomic dance, `clear` unlinking), the two exceptions, and `Ctx.checkpoint(name)`. Do not add expiry or per-key methods.

- [ ] **Step 4: Add the prose** — `src/keepwatch/reference/ctx.md`: a `### Checkpoints` section: a check may read, only an action may save; the *promote on success* pattern (propose in check, promote as each unit lands, so a killed poll retries the remainder and a restart repeats nothing); the difference from a ledger (whole documents vs timestamped keys).

- [ ] **Step 5: Run the tests and the gates**, then check the doc renders: `uv run keepwatch docs ctx | grep -A3 checkpoint`.

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/ctx.py src/keepwatch/reference/ctx.md tests/test_ctx.py
git commit -m "ctx.checkpoint: a persistent document a check may read but not write"
```

### Task 5: Release 2026.10.19

- [ ] **Step 1:** Bump both version files to `2026.10.19`.
- [ ] **Step 2:** Full gates; commit and tag locally (`git commit -m "Release 2026.10.19: ctx.checkpoint"`, `git tag v2026.10.19`); ask before pushing.
- [ ] **Step 3:** Post the #13 deviations from *Issue-log notes* as an issue comment (release, members, deviations), then close #13.

---

## Release 2026.10.20 — the tree watcher and `recipe = "mirror"` (#11)

### Task 6: `remote_watcher.py` tree mode

**Files:**
- Modify: `src/keepwatch/remote_watcher.py` (`DEFAULTS`, `Watcher.scan`, `Watcher.run`, `main`, the module docstring, `WATCHER_VERSION`)
- Test: `tests/test_remote_watcher.py`

**Interfaces:**
- Produces: `mode: "tree"` with `expected_files: [{"path", "size", "mtime"}]`, `expected_dirs: [...]`, `settle`; delivered events `{"event": "files", "entries": [[path, size, mtime], ...]}`, `{"event": "dirs", "paths": [...]}`, `{"event": "gone", "paths": [...]}`; an observer-level (not delivered) `{"event": "ready", "files": n, "dirs": n, "sent": n, "gone": n, "skipped": n, "walk_ms": n}` after each complete walk. Chunk constant `CHUNK = 2000` entries.
- Consumes: nothing outside the file (stdlib, Python 3.6, ASCII).
- Untouched: `mode: "watch"` behaves and is reported exactly as today; the `pull` recipe does not move.

- [ ] **Step 1: Write the failing tests** — `tests/test_remote_watcher.py`, following the file's existing "run the helper and read its JSON lines" pattern:

```python
def test_tree_mode_reports_new_files_dirs_and_gone(tmp_path):
    # a tree with a/b.txt; expected_files holding a stale path -> events: files (a/b.txt if absent from
    # expected), dirs, gone (the stale path)


def test_tree_mode_skips_an_unchanged_expected_file(tmp_path):
    # expected matches the live size and mtime -> no files entry, but a gone for it if it disappears


def test_tree_mode_prunes_ignored_directories(tmp_path):
    # ignore [".git"]: nothing under .git is walked or reported


def test_tree_mode_chunks_a_large_walk(tmp_path):
    # 2500 files -> more than one files event, every line under MAX_LINE, entries total 2500


def test_tree_mode_ready_reports_counts_once_per_walk(tmp_path):
    # ready is emitted after the walk with files/dirs/sent/skipped counts


def test_tree_mode_settle_defers_a_fresh_file(tmp_path):
    # a file touched now with settle 60 is not reported


def test_tree_mode_is_ascii_and_36_compatible():
    # the existing guards cover this file already; assert DEFAULTS["mode"] set handles "tree"
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_remote_watcher.py -k tree -v`

- [ ] **Step 3: Implement** — `parse_options` accepts `"tree"` and the three new keys; a tree walk (`os.walk`, `ignore` pruned on names, no symlinks followed) builds the live `{path: (size, mtime)}` and `{path}` dir sets; compare against `expected_*` and the walker's own `reported` memory: unchanged expected → silence; new or changed → `files` chunk; expected or previously reported but absent → `gone` chunk; new directory → `dirs` chunk. Emit `ready` with the counts. `settle` skips files younger than it (checked against `time.time() - mtime`). Keep the inotify loop: tree mode rescans on the notification path and the `rescan` safety net.

- [ ] **Step 4: Run the tests and the 3.6/ASCII guards**, then the full gates.

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/remote_watcher.py tests/test_remote_watcher.py
git commit -m "The remote watcher speaks trees: expected state in, chunked deltas out"
```

### Task 7: the `remote_tree` observer, and a dropped event reconnects

**Files:**
- Modify: `src/keepwatch/observers.py` (`Deliver` ~44, `Observer.emit` ~191, `remote_command`/`watcher_options`/`RemoteFilesObserver` ~647-737, `build_observer` ~738-757)
- Modify: `src/keepwatch/scheduler.py` (`add_event` ~111-125: return the dropped count)
- Modify: `src/keepwatch/config.py` (`ObserverConfig.skip_checkpoint: str | None = None`)
- Test: `tests/test_remote_files_observer.py` (or a new `tests/test_remote_tree_observer.py`), `tests/test_config_observe.py`

**Interfaces:**
- Consumes: `remote_watcher` tree mode (Task 6), `Checkpoint` (Task 4).
- Produces: `Deliver = Callable[[dict], int]`; `RemoteTreeObserver(RemoteFilesObserver)` with `kind = "remote_tree"`; `ObserverConfig.skip_checkpoint`; `build_observer` handles `"remote_tree"`. `[observe.*]` in a user's `config.toml` still rejects the kind (do not add it to `_OBSERVER_KIND_KEYS`).

- [ ] **Step 1: Write the failing tests:**

```python
def test_tree_options_come_from_the_checkpoint(tmp_path, make_watch, xdg):
    # a checkpoint document {"files": {"a/b": [1, 2.0]}, "dirs": ["a"]} for the watch
    # -> watcher options carry mode tree, expected_files [{path: "<remote>/a/b", size: 1, mtime: 2.0}],
    #    expected_dirs ["<remote>/a"]


def test_a_dropped_event_reconnects_the_watcher(tmp_path, make_watch, xdg, monkeypatch):
    # deliver returns 1 once; assert the observer's process was terminated (a restart follows its backoff)


def test_remote_tree_is_not_a_user_observer_kind(...):
    # validate a watch config with [observe.x] kind = "remote_tree" -> error naming the valid kinds
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement** — `Deliver` returns `int`; `scheduler.add_event` returns `self.events.put(...)` after its existing logging; `RemoteTreeObserver.run_once` builds the tree options from the checkpoint (absolute paths joined to `dir`), `emit` passes `files`/`dirs`/`gone` through and, when the deliver count is non-zero, logs and terminates its process under the existing lock; `build_observer` dispatches; `ObserverConfig` gains the field.

- [ ] **Step 4: Run the tests and the gates.**

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/observers.py src/keepwatch/scheduler.py src/keepwatch/config.py tests/test_remote_files_observer.py tests/test_config_observe.py
git commit -m "A remote tree observer whose checkpoint is its expected state; a dropped event reconnects"
```

### Task 8: mirror recipe — settings and the pure diff

**Files:**
- Create: `src/keepwatch/recipes/mirror.py`
- Modify: `src/keepwatch/config.py` (`RECIPES` ~302, `RECIPE_SETTINGS`, `_RECIPE_REQUIRED`, `_RECIPE_CHOICES`, `_recipe_observers` ~535)
- Test: `tests/test_mirror.py` (new), `tests/test_config_recipe.py`

**Interfaces:**
- Produces: `Plan` (frozen dataclass: `mkdir: tuple[str, ...]`, `copy: tuple[str, ...]`, `deleted: tuple[str, ...]`, `as_dict()`/`from_payload(payload)`); `walk_local(root: Path, ignore: Sequence[str], settle: float, *, now: float | None = None) -> tuple[dict[str, list[float]], set[str], list[str]]` (files `{rel: [size, mtime]}`, dirs, problems); `plan_push(live_files, live_dirs, state_files, state_dirs, *, delete: bool) -> Plan`; `plan_pull(events, state_files, state_dirs, *, delete: bool) -> Plan`; `as_document(files, dirs) -> dict` (`{"version": 1, "files": ..., "dirs": [...]}`); `files_of(document) -> dict`, `dirs_of(document) -> list`; `expected_state(remote_dir: str, files, dirs) -> dict` (the watcher's `expected_files`/`expected_dirs`).
- Settings: `direction` (`push`|`pull`, required), `remote`, `remote_dir`, `local_dir` (required), `delete = false`, `ignore = ()`, `mtime_tolerance = 2.0`, `settle = 0.0`, `file_timeout = 0.0`, the `_SSH_SETTINGS` block, `protocol`, `password_env`, `password_mode`, `remote_python`, `ssh_command`, `reachable_host`/`reachable_port`/`reachable_timeout`, `part_max_age`. Observers: pull → the tree observer with `skip_checkpoint = "mirror"`; push → the `files` observer with `recursive=True`, `settle = settings["settle"]`, `marker = "none"`, `ignore` from the settings.
- Consumes: `keepwatch.recipes.file_budget`, `pull.cleanup_parts` (reuse it for `.NAME.part` cleanup rather than re-implementing).

- [ ] **Step 1: Write the failing diff tests** — `tests/test_mirror.py`:

```python
def test_plan_push_copies_new_and_changed_and_creates_directories():
    plan = mirror.plan_push({"a/b.txt": [2, 10.0]}, {"a"}, {}, set(), delete=True)
    assert plan.copy == ("a/b.txt",) and plan.mkdir == ("a",) and plan.deleted == ()


def test_a_difference_within_tolerance_is_not_a_change():
    assert mirror.plan_push({"a.txt": [2, 10.0]}, set(), {"a.txt": [2, 11.0]}, set(), delete=True).copy == ()


def test_a_path_the_walk_did_not_see_is_deleted_always_and_removed_only_with_delete():
    live, state = {}, {"a.txt": [2, 10.0]}
    assert mirror.plan_push(live, set(), state, set(), delete=False).deleted == ("a.txt",)
    assert mirror.plan_push(live, set(), state, set(), delete=True).deleted == ("a.txt",)


def test_an_unreadable_subtree_is_not_a_deletion(tmp_path):
    # walk_local returns the problem; plan_push must be called by check with problems -> Unknown (Task 10 covers the call)


def test_walk_local_prunes_ignored_names_and_skips_recent_files(tmp_path):
    # .git/ and *.tmp absent; a file touched seconds ago absent when settle is 60


def test_the_checkpoint_document_round_trips():
    assert mirror.files_of(mirror.as_document({"a": [1, 2.0]}, {"b"})) == {"a": [1, 2.0]}


def test_plan_pull_reads_events_and_gone():
    events = [{"event": "files", "entries": [["/r/a.txt", 2, 10.0]]}, {"event": "gone", "paths": ["/r/b.txt"]}]
    plan = mirror.plan_pull(events, {"b.txt": [2, 10.0]}, set(), delete=True)
    assert plan.copy == ("a.txt",) and plan.deleted == ("b.txt",)


def test_no_events_never_deletes():
    assert mirror.plan_pull([], {"b.txt": [2, 10.0]}, set(), delete=True).deleted == ()
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_mirror.py -v`

- [ ] **Step 3: Implement the pure functions** — relative POSIX paths throughout; `mtime_tolerance` compared with `>`; `ignore` matched with `fnmatch` on names, directories pruned; `plan_pull` makes event paths relative to `remote_dir` and ignores `hello`/`ready`/`heartbeat`.

- [ ] **Step 4: Write the failing config tests** — `tests/test_config_recipe.py`, following the file's `validate`-style cases: `direction` required and a choice; `remote`/`remote_dir`/`local_dir` required; `mtime_tolerance` non-negative; `password_mode` needing `password_env`; the observers are `remote_tree` for pull and a recursive `files` for push.

- [ ] **Step 5: Implement the settings** — register `"mirror"` in `RECIPES`, `RECIPE_SETTINGS`, `_RECIPE_REQUIRED`, `_RECIPE_CHOICES`, and `_recipe_observers`; the recipe provides its hooks.

- [ ] **Step 6: Run the tests and the gates.**

- [ ] **Step 7: Commit**

```bash
git add src/keepwatch/recipes/mirror.py src/keepwatch/config.py tests/test_mirror.py tests/test_config_recipe.py
git commit -m "recipe = \"mirror\": the settings and the diff"
```

### Task 9: mirror recipe — the push direction

**Files:**
- Modify: `src/keepwatch/recipes/mirror.py`, `src/keepwatch/remote.py` (`ssh_run_argv(remote, command, *, port=None, identity=None, ssh_options=(), ssh_command=None) -> list[str]`, sharing the option block)
- Test: `tests/test_mirror.py`, `tests/test_recipe_mirror.py` (new)

**Interfaces:**
- Produces: `check(ctx) -> tuple[bool, dict]` (push branch), `on_true(ctx) -> None`, `batches(items: Sequence[str], size: int = 100) -> list[list[str]]`, `promote(state: Checkpoint, files: dict, dirs: set[str], *, pending: list[tuple[str, int]]) -> list` (returns the still-pending list, promoting when it holds ≥500 entries or ≥64 MiB).
- Consumes: `Plan` (Task 8), `ctx.transfer.push(path, f"{remote}:{remote_dir}/{parent}", marker="none", preserve_mtime=True, deadline=...)`, `ctx.checkpoint("mirror")`, `remote.ssh_run_argv` (Task 9 adds it), `file_budget`.

- [ ] **Step 1: Write the failing action tests** — `tests/test_recipe_mirror.py`, with fake `ctx.run`/`ctx.transfer` capturing argv and calls:

```python
def test_check_answers_unknown_when_the_host_is_unreachable(...):
    # tcp_open False -> Unknown, and no walk is reported as deletions


def test_mkdir_batches_are_bounded(...):
    # 250 directories -> 3 ssh calls, each with <=100 quoted paths


def test_rm_batches_and_the_empty_directory_sweep(...):
    # delete=True: rm -f batches then find <remote_dir> -type d -empty -delete


def test_a_failed_push_is_not_promoted(...):
    # one file's transfer raises; the others land; the checkpoint holds only the landed ones; the
    # raised error is the first failure


def test_promotion_happens_in_bounded_batches(...):
    # 1200 small files -> the checkpoint is saved at least twice, never once per file
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement** — `check`: `tcp_open` → `Unknown`; `walk_local`; a non-empty `problems` list → `Unknown` naming the unreadable paths (never a deletion); plan; FALSE when empty. `on_true`: mkdir batches → per-file `push` under `file_budget` → `deleted` files in `rm` batches and the `find … -empty -delete` sweep (only with `delete`) → `deleted` paths leave the checkpoint regardless → `promote` in batches → re-raise the first failure.

- [ ] **Step 4: Run the tests and the gates.**

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/recipes/mirror.py src/keepwatch/remote.py tests/test_mirror.py tests/test_recipe_mirror.py
git commit -m "recipe = \"mirror\": push, in batches, promoted as it lands"
```

### Task 10: mirror recipe — the pull direction

**Files:**
- Modify: `src/keepwatch/recipes/mirror.py`
- Test: `tests/test_recipe_mirror.py`, `tests/test_sweep_e2e.py`-style end-to-end (the ssh fixture in `tests/conftest.py`)

**Interfaces:**
- Produces: the pull branch of `check`/`on_true`.
- Consumes: `ctx.events`, `ctx.transfer.pull(..., on_conflict="overwrite", preserve_mtime=True)`, `os.utime`, `pull.cleanup_parts`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_pull_check_plans_from_events_and_answers_false_when_there_are_none(...):
    # no events -> FALSE (and no deletion), files events -> TRUE with copy


def test_no_events_never_deletes(...):
    # a checkpoint holding a file, no events: FALSE, nothing deleted


def test_pull_repairs_a_local_mtime(...):
    # pull returns a file whose mtime is now; the event says 10.0; after on_true the file's mtime is 10.0


def test_delete_off_forgets_but_keeps_the_local_file(...):
    # gone event, delete=False: the file stays, the checkpoint no longer holds it, the check answers TRUE


def test_pull_deletes_gone_paths_and_then_empty_directories(...):
    # delete=True: unlink the file, rmdir the now-empty directory the checkpoint knew
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement** — the pull branch of `check` (`tcp_open` → `Unknown`; `plan_pull` over `ctx.events`; TRUE when anything is to copy, create or forget) and of `on_true` (`os.makedirs`; `pull` under `file_budget`; `os.utime` when the local mtime differs by more than the tolerance; unlink + rmdir deepest-first with `delete`; cleanup parts once something was pulled; forget always; batched promotion).

- [ ] **Step 4: Add the end-to-end test** — create `tests/test_mirror_e2e.py` with the existing ssh fixture: a source tree with nested directories, a deleted file and a changed file; run check/on_true twice; assert the destination matches the source tree including mtimes, and that a second poll answers FALSE. Extend it with `delete = true` and a modified destination file.

- [ ] **Step 5: Run the tests and the gates.**

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/recipes/mirror.py tests/test_recipe_mirror.py tests/test_mirror_e2e.py
git commit -m "recipe = \"mirror\": pull from the watcher's deltas, with the local mtime repaired"
```

### Task 11: Documentation

**Files:**
- Modify: `src/keepwatch/reference/recipes.md` (a `## mirror` section), `README.md` (one clause in the introduction)
- Test: `tests/test_docs.py` (extend if it pins topics), `tests/test_examples.py` if an example is added

- [ ] **Step 1:** Write the `mirror` prose: the settings table is generated, so the section carries the semantics — the mirror-owned destination and what `delete` does and does not remove (a file that pre-dated the mirror is never removed), the live connection on the pull side and what happens when it is down (nothing is deleted; nothing is lost), mtime fidelity and the Windows `set times` repair, remote paths quoted and batched as user-facing advice, and the promote-on-success record.
- [ ] **Step 2:** README: the introduction's recipe sentence gains the mirror.
- [ ] **Step 3:** Run `uv run keepwatch docs recipes | grep -A20 '^## mirror'` and the gates.
- [ ] **Step 4: Commit**

```bash
git add src/keepwatch/reference/recipes.md README.md
git commit -m "Docs: the mirror recipe, and what delete means to it"
```

### Task 12: Release 2026.10.20

- [ ] **Step 1:** Bump both version files to `2026.10.20`.
- [ ] **Step 2:** Full gates, and the end-to-end mirror test explicitly (`uv run pytest tests/test_recipe_mirror.py -q`).
- [ ] **Step 3:** Commit and tag locally; ask before pushing.

```bash
git add pyproject.toml src/keepwatch/__init__.py
git commit -m "Release 2026.10.20: the mirror recipe, and the remote watcher's tree mode"
git tag v2026.10.20
```

- [ ] **Step 4:** Post the #11 deviations from *Issue-log notes* as an issue comment (release, settings, deviations), then close #11.

## Issue-log notes (post when each release lands)

These are the deviations to state plainly in the issue logs, with the release that carried them. They are notes
for the creator, not a defence: the original suggestion, what shipped instead, and why.

### #12 — kit mtimes (2026.10.18)

- Shipped `preserve_mtime` (default off) on `ScpOptions`, `ctx.transfer.copy/pull/push` and
  `keepwatch kit … --preserve-mtime`. The issue's other suggestion, a `restore_mtime` option, was **not** added:
  it needs a remote mtime keepwatch does not know, and every caller that does know it (the mirror, from its walk
  or its events) can set it with one `os.utime`, which is also the robust Windows repair.
- `-p` is the whole mechanism, in both protocol modes: scp sets the times itself through `SSH_FXP_SETSTAT` in
  sftp mode, and `sftp(1)` has its own `-p` for the resume path. So the `-M sftp -o setmtime` pairs the issue
  anticipated were not needed — this is a simplification, not an omission.
- Mode bits travel with the times (scp's `-p` preserves them too). Kept, not separable, and documented.
- A `set times` failure on a transfer whose data landed (Windows' `local set times: Input/output error`, a
  server's `set times: Operation not permitted`) is a **warning**, not a failure — but only with
  `preserve_mtime`, and a pull's part must still pass its size/sha256 checks.
- Default off: turning it on for everyone would have changed every existing transfer's mode bits and atimes.

### #13 — checkpoint (2026.10.19)

- Shipped the issue's second suggestion — `ctx.checkpoint("name")` — not a `ctx.state_dir`/`ctx.cache_dir` path.
  A path cannot enforce "checks do not write", while an object can refuse the save exactly as `ctx.ledger`
  already refuses `add` in a check; and this way the discipline is auditable in one place.
- Checks are always read-only (no check-writing opt-in): checks run for real under `--dry-run`, so anything a
  check writes outlives the dry run and would not be rolled back.
- The document lives at `data/checkpoints/NAME.json` (a subdirectory of the watch's data dir, not directly in
  it), written atomically, with `CheckpointReadOnly`/`CheckpointCorrupt` mirroring the ledger's two exceptions.
- The issue's phrasing "candidate → promote on success" shipped as **batched** promotion (500 files or 64 MiB),
  not per file: a whole-document checkpoint rewritten per file makes a 20 000-file first sync quadratic
  (tens of GB of writes). The worst case after a kill is re-copying the last batch, not the whole tree.
- The promote-on-success pattern is documented (`keepwatch docs ctx`), which was the issue's third suggestion.

### #11 — mirror recipe (2026.10.20)

- Settings differ from the sketch: `direction` + `remote` + `remote_dir` + `local_dir` (the pull recipe's
  convention, because a pull needs the endpoint too, unlike `push`'s single `dest`); `delete = false` by default
  (the sketch's `delete = true` is the config, not the default — deletion propagation is opt-in); `ignore`
  rather than `excludes` (keepwatch's existing name for name globs); `recursive` dropped (a mirror is recursive,
  so the knife is not offered); `mtime_tolerance = 2.0` kept.
- The one live ssh connection is used for the **pull** side's deltas (remote watcher tree mode), which is where
  the recurring cost was. The push direction's source is local (a walk that costs no ssh), and its remote
  mutations — `mkdir -p`, `rm -f` batches, the empty-directory sweep — stay discrete batched ssh calls, as the
  issue's own item 4 proposed. Riding those on the live session would make the helper a command channel; not
  done, and not needed.
- **The notable semantic deviation:** `delete` propagates deletions of files **the mirror synced**. A file that
  was already in the destination before the mirror ever ran is never removed, because the destination is never
  scanned — the checkpoint is what the mirror knows, and that is the price of the cheap poll. A `verify` mode
  that scans both sides is the next step if that ever matters.
- One further consequence of the same invariant: a deletion that happens on the source while keepwatch is down
  is still reported when the watcher reconnects, because the checkpoint is the watcher's expected state. Nothing
  is ever deleted from an incomplete view — not from an unreadable subtree, not from a quiet stream.
- Windows lessons from the issue were dissolved rather than handled: no `find -printf` (a Python helper walks
  the tree and writes JSON lines, so no null-byte text pipes and no `-print0`), no ssh multiplexing, and every
  batched command line is ≤100 paths. `os.utime` from the known source mtime is the repair the issue asked for,
  and the pull side does it.
- Not in this release: `resume` for mirror transfers (a failed copy is repaired by the next diff, since its
  entry is not promoted); symlinks (skipped and logged — the issue's port followed them with `find -L`); a
  Windows remote host; and making the tree watcher a public observer kind.

## Post-plan

- The user's real workload replaces the custom `downloads-push`/`downloads-pull` watches: two `config.toml` files, a scratch tree first, then `Downloads`, with `delete` chosen deliberately.
- A `verify` mode that scans both sides (spec §5.2, decision 3) is the next step if drift ever matters.
