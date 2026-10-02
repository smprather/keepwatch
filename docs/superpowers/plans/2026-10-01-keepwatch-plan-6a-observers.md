# keepwatch Plan 6a (Observers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add observers: event sources the service keeps running for a watch (`command` and `files` kinds), whose events reach hooks as `ctx.events` / `KEEPWATCH_EVENTS_FILE`, are acknowledged only by a successful poll, and can wake the watch at once. Also `keepwatch poll --events`, `keepwatch observe`, and the docs.

**Architecture:** `config.py` parses `[observe.<name>]` tables into `ObserverConfig`s on `WatchConfig.observers`. A new `observers.py` holds `parse_event`, `EventQueue`, the `Observer` base class (one thread per observer, restarting with backoff), `CommandObserver` (a long-lived child process in its own process group / Job Object, one event per stdout line) and `FilesObserver` (`watchdog` notifications plus rescans, reporting settled files). `WatchRunner` (scheduler.py) owns one `EventQueue` per watch, starts/stops/restarts its observers from its own thread, hands pending events to `PollEngine.poll(events=...)`, and acknowledges them only when the poll succeeds. `HookCall.events` carries them into the Python worker (`ctx.events`) and command hooks (`KEEPWATCH_EVENTS_FILE`).

**Tech Stack:** Python 3.12+, rich-click, pytest, ruff; new runtime dependency `watchdog>=6`.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 2; sections 3-6 are Plans 6b-6d). The `remote_files` kind is Plan 6b: in this plan `kind` accepts only `command` and `files`.

## Global Constraints

- Branch `keepwatch-observers` (already checked out). Do not push. Do not create other branches.
- Stage files explicitly (`git add <paths>`); never `git commit -a` or `git add -A` (the working tree has an unrelated `.gitignore` change that must stay uncommitted).
- Before every commit run, **without a pipe**: `uv run pytest -q` (the whole suite must be green) and `uv run ruff check src tests`. Read the final pytest summary line yourself.
- All imports at the top of each module (ruff E402). 4-space indentation, no tabs.
- New tests are portable (they run on Linux and Windows in CI): commands are built from the current Python (`PY` / `literal` / `toml_path` / `py` from `tests/portable.py`), paths are written into TOML with `toml_path(...)` (single-quoted literal strings, forward slashes).
- If any step in this plan is wrong (a test cannot pass as written, an interface does not match the code), **stop and report** the problem and your diagnosis instead of improvising.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A watch whose observer program dies at once (bad path, crash on start) must not spin: it restarts at 5 s, 10 s, … 5 min. Test: Task 3 `test_restart_delays_double_up_to_the_cap`, `test_a_program_that_cannot_start_is_retried`.
- An event that arrives while a poll is running must be neither lost nor acknowledged by that poll, and must cause another poll right away. Test: Task 5 `test_event_arriving_during_a_poll_is_kept_and_wakes`.
- A missing or not-yet-created observed directory must be retried, and files must be found once it appears. Test: Task 4 `test_missing_directory_is_retried`.
- A file copied with its old modification time preserved (`cp -p`, `scp -p`, tar) must not be reported before it stops growing. Test: Task 4 `test_a_file_still_growing_is_not_reported_early`.
- Disabling a watch (or it going offline) must stop its observers, and enabling it must start them again. Test: Task 5 `test_offline_stops_observers_and_online_restarts_them`.

---

### Task 1: `[observe.<name>]` configuration

**Files:**
- Modify: `src/keepwatch/config.py`
- Modify: `src/keepwatch/reference.py` (`_default_text`, `config_topic`)
- Modify: `tests/test_docs.py` (`test_every_config_key_is_documented`)
- Test: `tests/test_config_observe.py` (new)

**Interfaces:**
- Produces:
  - `config.OBSERVER_KINDS = ("command", "files")`
  - `config.DEFAULT_IGNORE = (".*", "*.tmp", "*.part", "*~")`
  - `config.OBSERVER_KEYS: tuple[Key, ...]` (for docs)
  - `config.ObserverConfig` (frozen dataclass): `name: str`, `kind: str`, `wake: bool = True`, `command: Command | None = None`, `stdin: str | None = None`, `heartbeat_timeout: float | None = None`, `path: Path | None = None`, `pattern: str = "*"`, `ignore: tuple[str, ...] = DEFAULT_IGNORE`, `recursive: bool = False`, `settle: float = 10.0`
  - `WatchConfig.observers: Mapping[str, ObserverConfig]` (default empty; a `MappingProxyType` when loaded)

- [ ] **Step 1: Write the failing tests** — create `tests/test_config_observe.py`:

```python
from pathlib import Path

import pytest

from keepwatch.config import DEFAULT_IGNORE, Command, ConfigError, ObserverConfig, load_watch_config
from portable import PY, literal


def load(make_watch, text):
    return load_watch_config(make_watch("w", config=text))


def problems(make_watch, text):
    with pytest.raises(ConfigError) as info:
        load(make_watch, text)
    return [str(problem) for problem in info.value.problems]


def test_no_observers_by_default(make_watch):
    assert load(make_watch, "").observers == {}


def test_command_observer(make_watch):
    config = load(
        make_watch,
        f"[observe.feed]\nkind = 'command'\ncommand = [{literal(PY)}, 'feed.py']\n"
        "stdin = 'hello'\nheartbeat_timeout = '90s'\nwake = false\n",
    )
    assert config.observers["feed"] == ObserverConfig(
        name="feed",
        kind="command",
        wake=False,
        command=Command(argv=(PY, "feed.py")),
        stdin="hello",
        heartbeat_timeout=90.0,
    )


def test_command_observer_may_be_a_string(make_watch):
    config = load(make_watch, "[observe.feed]\nkind = 'command'\ncommand = 'tail -F x.log'\n")
    assert config.observers["feed"].command == Command(shell="tail -F x.log")


def test_files_observer_defaults_and_relative_path(make_watch):
    config = load(make_watch, "[observe.inbox]\nkind = 'files'\npath = 'inbox'\n")
    observer = config.observers["inbox"]
    assert observer.path == config.watch_dir / "inbox"
    assert (observer.pattern, observer.recursive, observer.settle, observer.wake) == ("*", False, 10.0, True)
    assert observer.ignore == DEFAULT_IGNORE == (".*", "*.tmp", "*.part", "*~")


def test_files_path_expands_home(make_watch):
    config = load(make_watch, "[observe.inbox]\nkind = 'files'\npath = '~/drop'\n")
    assert config.observers["inbox"].path == Path.home() / "drop"


def test_files_observer_keys(make_watch):
    config = load(
        make_watch,
        "[observe.inbox]\nkind = 'files'\npath = 'in'\npattern = '*.gz'\nignore = []\nrecursive = true\nsettle = '0s'\n",
    )
    observer = config.observers["inbox"]
    assert (observer.pattern, observer.ignore, observer.recursive, observer.settle) == ("*.gz", (), True, 0.0)


def test_missing_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\npath = 'in'\n")
    assert "[observe.x] needs kind = one of command, files; got missing" in problem
    assert "config.toml:1:" in problem
    assert "keepwatch docs observers" in problem


def test_unknown_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'remote'\n")
    assert "got 'remote'" in problem and "config.toml:2:" in problem


def test_key_of_the_other_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\ncommand = ['a']\npattern = '*.gz'\n")
    assert "'pattern' only applies to kind = \"files\"; this observer is kind = \"command\"" in problem
    assert "config.toml:4:" in problem


def test_unknown_key_suggests(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'files'\npath = 'in'\nsetle = '5s'\n")
    assert "unknown key 'setle' in [observe.x] (did you mean 'settle'?)" in problem


def test_required_key(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\n")
    assert "[observe.x] (kind = \"command\") needs 'command'" in problem


def test_bad_values_are_all_reported(make_watch):
    found = problems(make_watch, "[observe.x]\nkind = 'files'\npath = ''\nsettle = 'soon'\nrecursive = 'yes'\n")
    assert len(found) == 3
    assert any("'path' must not be empty" in problem for problem in found)


def test_heartbeat_timeout_minimum(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\ncommand = ['a']\nheartbeat_timeout = '0s'\n")
    assert "at least 1s" in problem


def test_bad_observer_name(make_watch):
    [problem] = problems(make_watch, "[observe.'a b']\nkind = 'files'\npath = 'in'\n")
    assert "observer name 'a b' may contain only letters, digits, '_' and '-'" in problem


def test_observe_must_be_a_table(make_watch):
    [problem] = problems(make_watch, "observe = 3\n")
    assert "'observe' must be a table of observers" in problem


def test_each_observer_must_be_a_table(make_watch):
    [problem] = problems(make_watch, "[observe]\nx = 3\n")
    assert "'observe.x' must be a table ([observe.x])" in problem
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_observe.py -q`
Expected: collection error, `ImportError: cannot import name 'DEFAULT_IGNORE'`.

- [ ] **Step 3: Implement** — in `src/keepwatch/config.py`:

(a) After the `ExitCodes` dataclass (before `class Key`), add nothing yet. After `WATCH_TABLES` (and before `class WatchConfig`), add:

```python
OBSERVER_KINDS = ("command", "files")
DEFAULT_IGNORE = (".*", "*.tmp", "*.part", "*~")

OBSERVER_KEYS = (
    Key(
        "kind",
        "str",
        None,
        "Required. `command`: a long-running program; each line it prints is an event. "
        "`files`: a local directory; each settled file is an event.",
    ),
    Key("wake", "bool", True, "Poll the watch as soon as an event arrives (unless it is backing off or offline)."),
    Key(
        "command",
        "command",
        None,
        "kind = command, required: the program. A list runs directly (a relative program is resolved against the "
        "watch directory); a string runs through the watch's `shell`.",
    ),
    Key("stdin", "str", None, "kind = command: text written to the program's standard input once at start; then stdin is closed."),
    Key(
        "heartbeat_timeout",
        "interval",
        None,
        "kind = command: restart the program when it prints no line (heartbeats included) for this long.",
    ),
    Key(
        "path",
        "str",
        None,
        "kind = files, required: the directory to observe. Relative to the watch directory; `~` and environment "
        "variables are expanded.",
    ),
    Key("pattern", "str", "*", "kind = files: report only file names matching this glob."),
    Key("ignore", "str_list", DEFAULT_IGNORE, "kind = files: never report file names matching any of these globs."),
    Key("recursive", "bool", False, "kind = files: also observe subdirectories."),
    Key(
        "settle",
        "duration",
        10.0,
        "kind = files: report a file once its size and modification time have not changed for this long and it "
        "was last modified at least this long ago.",
    ),
)
_OBSERVER_KIND_KEYS = {
    "command": ("command", "stdin", "heartbeat_timeout"),
    "files": ("path", "pattern", "ignore", "recursive", "settle"),
}
_OBSERVER_REQUIRED = {"command": "command", "files": "path"}
_OBSERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class ObserverConfig:
    """One [observe.<name>] table. Only the keys of its kind are meaningful."""

    name: str
    kind: str
    wake: bool = True
    command: Command | None = None
    stdin: str | None = None
    heartbeat_timeout: float | None = None
    path: Path | None = None
    pattern: str = "*"
    ignore: tuple[str, ...] = DEFAULT_IGNORE
    recursive: bool = False
    settle: float = 10.0
```

(b) In `WATCH_TABLES`, add the entry (last):

```python
    "observe": "Event sources the service keeps running for this watch, one [observe.<name>] table each; "
    "see `keepwatch docs observers`.",
```

(c) In `WatchConfig`, add the field after `settings`:

```python
    observers: Mapping[str, ObserverConfig] = field(default_factory=dict)
```

(d) After `_settings_table` (before `load_watch_config`), add:

```python
def _observed_path(text: str, watch_dir: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    return expanded if expanded.is_absolute() else watch_dir / expanded


def _observer(collector: _Collector, name: str, raw: dict[str, Any], watch_dir: Path) -> ObserverConfig | None:
    table = f"observe.{name}"
    kind = raw.get("kind")
    if kind not in OBSERVER_KINDS:
        shown = "missing" if kind is None else repr(kind)
        collector.add(
            f"[{table}] needs kind = one of {', '.join(OBSERVER_KINDS)}; got {shown}",
            key="kind" if kind is not None else None,
            table=table,
            topic="observers",
        )
        return None
    by_name = {key.name: key for key in OBSERVER_KEYS}
    allowed = ("kind", "wake", *_OBSERVER_KIND_KEYS[kind])
    before = len(collector.problems)
    values: dict[str, Any] = {}
    for key_name, item in raw.items():
        if key_name == "kind":
            continue
        if key_name not in by_name:
            _unknown_key(collector, key_name, allowed, table=table)
            continue
        if key_name not in allowed:
            other = next(owner for owner, names in _OBSERVER_KIND_KEYS.items() if key_name in names)
            collector.add(
                f"'{key_name}' only applies to kind = \"{other}\"; this observer is kind = \"{kind}\"",
                key=key_name,
                table=table,
                topic="observers",
            )
            continue
        if key_name == "command":
            command = _command(collector, item, key="command", table=table, topic="observers")
            if command is not None:
                values["command"] = command
            continue
        converted = _convert(collector, by_name[key_name], item, table=table)
        if converted is _INVALID:
            continue
        if key_name == "path" and converted == "":
            collector.add("'path' must not be empty", key="path", table=table, topic="observers")
            continue
        values[key_name] = converted
    required = _OBSERVER_REQUIRED[kind]
    if required not in raw:
        collector.add(f"[{table}] (kind = \"{kind}\") needs '{required}'", table=table, topic="observers")
    if len(collector.problems) > before:
        return None
    if "path" in values:
        values["path"] = _observed_path(values["path"], watch_dir)
    return ObserverConfig(name=name, kind=kind, **values)


def _observe_table(collector: _Collector, value: Any, watch_dir: Path) -> dict[str, ObserverConfig]:
    if not isinstance(value, dict):
        collector.add(
            "'observe' must be a table of observers, one [observe.<name>] table each",
            key="observe",
            topic="observers",
        )
        return {}
    observers = {}
    for name, raw in value.items():
        if not _OBSERVER_NAME.fullmatch(name):
            collector.add(
                f"observer name '{name}' may contain only letters, digits, '_' and '-', starting with a letter or digit",
                table=f"observe.{name}",
                topic="observers",
            )
            continue
        if not isinstance(raw, dict):
            collector.add(
                f"'observe.{name}' must be a table ([observe.{name}])", key=name, table="observe", topic="observers"
            )
            continue
        observer = _observer(collector, name, raw, watch_dir)
        if observer is not None:
            observers[name] = observer
    return observers
```

(e) In `load_watch_config`: add `observers: dict[str, ObserverConfig] = {}` next to `settings: dict[str, Any] = {}`; add a branch before the final `else:`

```python
        elif name == "observe":
            observers = _observe_table(collector, value, watch_dir)
```

and pass `observers=MappingProxyType(observers),` to `WatchConfig(...)` (after `settings=...`).

Then in `src/keepwatch/reference.py`:

(f) In `_default_text`, replace

```python
    if isinstance(value, tuple):
        return "`[]`"
```

with

```python
    if isinstance(value, tuple):
        return "`[" + ", ".join(f'"{item}"' for item in value) + "]`"
```

(g) In `config_topic`, change the import to `from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, OBSERVER_KEYS, WATCH_KEYS, WATCH_TABLES` and, right after the `lines += [f"- `[{name}]`: {doc}" for name, doc in WATCH_TABLES.items()]` line for the watch tables (the first one), add:

```python
    lines += ["", "### `[observe.<name>]`", "", "One table per observer; see `keepwatch docs observers`.", ""]
    lines += _key_table(OBSERVER_KEYS, defaultable_column=False)
```

(h) In `tests/test_docs.py`, `test_every_config_key_is_documented`: import `OBSERVER_KEYS` too (`from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, OBSERVER_KEYS, WATCH_KEYS, WATCH_TABLES`) and change the loop to `for key in (*WATCH_KEYS, *GLOBAL_KEYS, *LOG_KEYS, *OBSERVER_KEYS):`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_observe.py tests/test_docs.py tests/test_config_watch.py -q`
Expected: all pass. (The topic `observers` named in the messages is added in Task 7.)

- [ ] **Step 5: Full suite, lint, commit**

Run: `uv run pytest -q` and `uv run ruff check src tests` (no pipes). Then:

```bash
git add src/keepwatch/config.py src/keepwatch/reference.py tests/test_docs.py tests/test_config_observe.py
git commit -m "Parse [observe.<name>] observer tables"
```

---

### Task 2: Events reach the hooks

**Files:**
- Modify: `src/keepwatch/runner.py`, `src/keepwatch/worker.py`, `src/keepwatch/ctx.py`, `src/keepwatch/pollengine.py`
- Test: `tests/test_event_delivery.py` (new)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `runner.base_environment(environment: Mapping[str, str], watch: WatchConfig) -> dict[str, str]` — the service environment without inherited `KEEPWATCH_*` (except `KEEPWATCH_CONFIG`), then the global `[environment]`, the watch's `[environment]` and `KEEPWATCH_SETTING_*`
  - `HookCall.events: tuple[dict[str, Any], ...] = ()`
  - `PollEngine.poll(..., events: Sequence[dict[str, Any]] = ())`; the `poll.start` record gains `events` (the count)
  - `PollEngine.global_config` (read-only property: the current `GlobalConfig`)
  - `Ctx.events: list[dict[str, Any]]` (constructor keyword `events`, default empty)
  - command hooks get `KEEPWATCH_EVENTS_FILE` (always; a JSON array, `[]` without events)

- [ ] **Step 1: Write the failing tests** — create `tests/test_event_delivery.py`:

```python
import json
import os

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import py

EVENTS = ({"event": "file", "name": "a.gz", "observer": "inbox"}, {"line": "hi", "observer": "feed"})
COPY_EVENTS = py('import os, shutil; shutil.copy(os.environ["KEEPWATCH_EVENTS_FILE"], "events.json")')
WATCH_PY = """
    import json


    def check(ctx):
        return True, [event.get("name") or event.get("line") for event in ctx.events]


    def on_true(ctx):
        (ctx.watch_dir / "seen.json").write_text(json.dumps(ctx.events))
"""


def engine_for(xdg, records):
    return PollEngine(
        runner=Runner(),
        paths=xdg,
        global_config=load_global_config(xdg.config_file, xdg),
        sink=records.append,
        pid=os.getpid(),
    )


def test_python_hooks_get_ctx_events(make_watch, xdg):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    records = []
    report = engine_for(xdg, records).poll(load_watch_config(watch_dir), WatchState(False), events=EVENTS)
    assert report.outcome is Outcome.TRUE and report.payload == ["a.gz", "hi"]
    assert json.loads((watch_dir / "seen.json").read_text()) == list(EVENTS)
    assert [r["events"] for r in records if r["event"] == "poll.start"] == [2]


def test_python_hooks_without_events_get_an_empty_list(make_watch, xdg):
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    return ctx.events == []\n"})
    report = engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False))
    assert report.outcome is Outcome.TRUE


def test_command_hooks_get_the_events_file(make_watch, xdg):
    watch_dir = make_watch("w", config=f"[hooks]\ncheck = {COPY_EVENTS}\n")
    engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False), events=EVENTS)
    assert json.loads((watch_dir / "events.json").read_text()) == list(EVENTS)


def test_command_hooks_without_events_get_an_empty_array(make_watch, xdg):
    watch_dir = make_watch("w", config=f"[hooks]\ncheck = {COPY_EVENTS}\n")
    engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False))
    assert json.loads((watch_dir / "events.json").read_text()) == []
    assert not list(xdg.run_dir(os.getpid(), "w").glob("*-events.json"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_event_delivery.py -q`
Expected: FAIL — `TypeError: PollEngine.poll() got an unexpected keyword argument 'events'` (and `KEEPWATCH_EVENTS_FILE` missing / `ctx.events` missing).

- [ ] **Step 3: Implement**

(a) `src/keepwatch/runner.py` — in `HookCall`, add after `mode: str = "call"`:

```python
    events: tuple[dict[str, Any], ...] = ()
```

Add this module-level function right after `setting_variables`:

```python
def base_environment(environment: Mapping[str, str], watch: WatchConfig) -> dict[str, str]:
    """What hooks and observers start from: the service's environment without inherited KEEPWATCH_*
    (except KEEPWATCH_CONFIG), then the global [environment], the watch's [environment] and KEEPWATCH_SETTING_*."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("KEEPWATCH_") or key == "KEEPWATCH_CONFIG"}
    env.update(environment)
    env.update(watch.environment)
    env.update(setting_variables(watch.settings))
    return env
```

Replace the first part of `Runner._environment` (everything up to and including `env.update(setting_variables(call.watch.settings))`) with:

```python
        env = base_environment(call.environment, call.watch)
```

(the `env.update({"KEEPWATCH_WATCH": ...})` block and `return env` stay).

In `_run_python`'s `request` dict, add after `"payload": call.payload,`:

```python
            "events": list(call.events),
```

In `_run_command`, right after the three lines that write the settings file and set `KEEPWATCH_SETTINGS_FILE`, add:

```python
                events_file = call.run_dir / f"{stem}-events.json"
                events_file.write_text(json.dumps(list(call.events)), encoding="utf-8")
                temp_files.append(events_file)
                env["KEEPWATCH_EVENTS_FILE"] = str(events_file)
```

(b) `src/keepwatch/ctx.py` — `Ctx.__init__` gains a keyword parameter after `shell`:

```python
        events: Sequence[Mapping[str, Any]] | None = None,
```

and in the body (after `self.payload = payload`):

```python
        self.events: list[dict[str, Any]] = [dict(event) for event in events or ()]
```

In the `Ctx` class docstring, replace `payload (what check returned with its answer; actions only),` with
`payload (what check returned with its answer; actions only), events (observer events delivered with this poll, oldest first; see keepwatch docs observers),`.

(c) `src/keepwatch/worker.py` — in `run_request`, add to the `Ctx(...)` call after `shell=request.get("shell"),`:

```python
        events=request.get("events") or [],
```

(d) `src/keepwatch/pollengine.py` — add `from collections.abc import Sequence` to the imports. Add a property to `PollEngine` after `set_global_config`:

```python
    @property
    def global_config(self) -> GlobalConfig:
        return self._global
```

Change `poll`'s signature to add `events: Sequence[dict[str, Any]] = (),` after `trial: bool = False,`. At the top of the body (after `poll_id = ...`), add `events = tuple(events)`; add `events=len(events)` to the `poll.start` record (after `trial=trial`). Change both `self._run(...)` calls to pass `events` as a new last argument:

```python
            result = self._run(watch, CHECK, poll_id, state.condition, None, watch.check_timeout, events)
```

```python
                result = self._run(watch, hook, poll_id, plan.state.condition, payload, watch.action_timeout, events)
```

and change `_run`'s signature to end with `timeout: float, events: tuple[dict[str, Any], ...],` and pass `events=events,` to `HookCall(...)` (after `on_message=...`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_event_delivery.py tests/test_pollengine.py tests/test_runner_command.py tests/test_runner_python.py tests/test_ctx.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/runner.py src/keepwatch/worker.py src/keepwatch/ctx.py src/keepwatch/pollengine.py tests/test_event_delivery.py
git commit -m "Deliver events to hooks: ctx.events and KEEPWATCH_EVENTS_FILE"
```

---

### Task 3: Observer runtime and the `command` kind

**Files:**
- Create: `src/keepwatch/observers.py`
- Test: `tests/test_observers.py` (new)

**Interfaces:**
- Consumes: `ObserverConfig`, `WatchConfig.observers` (Task 1); `runner.base_environment` (Task 2); `platform.start_process`, `platform.command_argv`, `HookProcess` (existing); `runner.KILL_GRACE`, `runner.DRAIN_GRACE`.
- Produces:
  - `observers.Deliver = Callable[[dict[str, Any]], None]`
  - `observers.parse_event(text: str) -> dict[str, Any] | None`
  - `observers.EventQueue(cap: int = QUEUE_CAP)`: `put(event) -> bool` (True if the oldest was dropped), `pending() -> tuple[int, list[dict]]`, `ack(mark: int)`, `len()`, attributes `cap`, `dropped`
  - `observers.observer_environment(watch, name, *, environment, data_dir) -> dict[str, str]`
  - `observers.Observer`: `start()`, `stop()`, `join(timeout) -> bool`, `running` (property), `restarts: int`, `last_event: float | None` (epoch), `kind: str`, `name: str`
  - `observers.CommandObserver(Observer)`
  - `observers.build_observer(watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink, environment: Mapping[str, str], data_dir: Path) -> Observer`
  - module constants read at use time (tests monkeypatch them): `BACKOFF_START = 5.0`, `BACKOFF_CAP = 300.0`, `RESET_AFTER = 300.0`, `QUEUE_CAP = 10_000`, `MAX_LINE = 1024 * 1024`, `OUTPUT_RECORDS_PER_MINUTE = 20`
  - records: `observer.started` (`kind`, `argv`, `pid`), `observer.event` (DEBUG, `data`), `observer.output` (`stream`, `text`; a WARNING one with `suppressed`), `observer.stopped` (`exit_code`, `reason`, `duration`, `stderr_tail`; INFO when `reason == "stopped"`, else WARNING), `observer.restarting` (`delay`), `observer.crash` (ERROR, `error`, `traceback`). All carry `watch` and `observer`.
  - delivered events are the source's object plus `"observer": <name>` and `"received": <iso_time>` (these two overwrite same-named keys)

- [ ] **Step 1: Write the failing tests** — create `tests/test_observers.py`:

```python
import time

import pytest

from keepwatch import observers
from keepwatch.config import load_watch_config
from keepwatch.observers import EventQueue, build_observer, parse_event
from portable import PY, literal

EMITTER = """
import json, sys, time
print(json.dumps({"event": "file", "name": "a"}))
print("plain text")
print(json.dumps({"event": "heartbeat"}))
print("to stderr", file=sys.stderr, flush=True)
time.sleep(60)
"""
SILENT = "import time\ntime.sleep(60)\n"
BEATER = """
import json, time
while True:
    print(json.dumps({"event": "heartbeat"}), flush=True)
    time.sleep(0.3)
"""
ECHO_STDIN = """
import sys, time
for line in sys.stdin:
    print(line.strip(), flush=True)
print("eof", flush=True)
time.sleep(60)
"""
SHOW_ENV = """
import json, os, time
names = ("KEEPWATCH_WATCH", "KEEPWATCH_OBSERVER", "KEEPWATCH_DATA_DIR", "FROM_WATCH")
print(json.dumps({name: os.environ.get(name) for name in names}), flush=True)
time.sleep(60)
"""
NOISY = """
import sys
for number in range(10):
    print(number, file=sys.stderr)
"""
LONG_LINE = """
import time
print("x" * 300)
print("ok", flush=True)
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def command_observer(make_watch, xdg, script=None, *, argv=None, extra=""):
    command = argv or f"[{literal(PY)}, 'source.py']"
    config = f"[observe.src]\nkind = 'command'\ncommand = {command}\n{extra}"
    watch_dir = make_watch("w", config=config, files={"source.py": script} if script else None)
    watch = load_watch_config(watch_dir)
    events, records = [], []
    observer = build_observer(
        watch,
        watch.observers["src"],
        deliver=events.append,
        sink=records.append,
        environment={},
        data_dir=xdg.watch_data_dir("w"),
    )
    return observer, events, records


def stop(observer):
    observer.stop()
    assert observer.join(15)


def test_parse_event():
    assert parse_event('{"event": "file", "name": "a"}\n') == {"event": "file", "name": "a"}
    assert parse_event("hello world\n") == {"line": "hello world"}
    assert parse_event("[1, 2]\r\n") == {"line": "[1, 2]"}
    assert parse_event('{"event": "heartbeat"}\n') is None
    assert parse_event("  \n") is None


def test_event_queue_ack_keeps_later_events():
    queue = EventQueue(cap=3)
    queue.put({"n": 1})
    queue.put({"n": 2})
    mark, pending = queue.pending()
    assert pending == [{"n": 1}, {"n": 2}]
    queue.put({"n": 3})
    queue.ack(mark)
    assert queue.pending()[1] == [{"n": 3}] and len(queue) == 1


def test_event_queue_drops_the_oldest():
    queue = EventQueue(cap=2)
    assert queue.put({"n": 1}) is False
    queue.put({"n": 2})
    assert queue.put({"n": 3}) is True
    assert queue.pending()[1] == [{"n": 2}, {"n": 3}] and queue.dropped == 1


def test_command_observer_delivers_events(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, EMITTER)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 2 and kinds(records, "observer.output"))
        assert observer.running
    finally:
        stop(observer)
    assert [event.get("name") or event.get("line") for event in events] == ["a", "plain text"]
    assert all(event["observer"] == "src" and event["received"] for event in events)
    assert observer.last_event is not None
    assert kinds(records, "observer.output")[0]["text"] == "to stderr"
    assert kinds(records, "observer.started")[0]["kind"] == "command"
    [stopped] = kinds(records, "observer.stopped")
    assert stopped["reason"] == "stopped" and stopped["level"] == "INFO"
    assert not kinds(records, "observer.restarting")
    assert len(kinds(records, "observer.event")) == 2


def test_command_observer_restarts_after_exit(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv=f"[{literal(PY)}, '-c', 'print(1)']")
    observer.start()
    try:
        assert wait_for(lambda: len(events) >= 2)
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["reason"] == "exited" and first["exit_code"] == 0 and first["level"] == "WARNING"
    assert kinds(records, "observer.restarting")[0]["delay"] == 0.2
    assert observer.restarts >= 1
    assert events[0]["line"] == "1"


def test_restart_delays_double_up_to_the_cap(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv=f"[{literal(PY)}, '-c', 'pass']")
    observer.start()
    try:
        assert wait_for(lambda: len(kinds(records, "observer.restarting")) >= 3)
    finally:
        stop(observer)
    assert [record["delay"] for record in kinds(records, "observer.restarting")][:3] == [0.2, 0.4, 0.4]


def test_a_program_that_cannot_start_is_retried(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv="['./does-not-exist']")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["reason"].startswith("cannot start") and first["exit_code"] is None
    assert not kinds(records, "observer.started")


def test_heartbeat_timeout_restarts_a_silent_program(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SILENT, extra="heartbeat_timeout = '1s'\n")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.stopped"))
    finally:
        stop(observer)
    assert "heartbeat_timeout" in kinds(records, "observer.stopped")[0]["reason"]


def test_heartbeats_keep_a_program_alive_and_are_not_delivered(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, BEATER, extra="heartbeat_timeout = '1s'\n")
    observer.start()
    try:
        time.sleep(2.5)
        assert not kinds(records, "observer.stopped")
    finally:
        stop(observer)
    assert events == []


def test_stdin_is_written_then_closed(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, ECHO_STDIN, extra='stdin = "one\\ntwo\\n"\n')
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 3)
    finally:
        stop(observer)
    assert [event["line"] for event in events] == ["one", "two", "eof"]


def test_observer_environment(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SHOW_ENV, extra="[environment]\nFROM_WATCH = 'yes'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    seen = events[0]
    assert (seen["KEEPWATCH_WATCH"], seen["KEEPWATCH_OBSERVER"], seen["FROM_WATCH"]) == ("w", "src", "yes")
    assert seen["KEEPWATCH_DATA_DIR"] == str(xdg.watch_data_dir("w"))
    assert xdg.watch_data_dir("w").is_dir()


def test_stderr_records_are_rate_limited(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "OUTPUT_RECORDS_PER_MINUTE", 3)
    observer, events, records = command_observer(make_watch, xdg, NOISY)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.stopped"))
    finally:
        stop(observer)
    outputs = kinds(records, "observer.output")
    assert [record["text"] for record in outputs[:3]] == ["0", "1", "2"]
    assert outputs[3]["suppressed"] == 7 and outputs[3]["level"] == "WARNING"
    assert kinds(records, "observer.stopped")[0]["stderr_tail"] == ["5", "6", "7", "8", "9"]


def test_overlong_stdout_lines_are_dropped(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "MAX_LINE", 100)
    observer, events, records = command_observer(make_watch, xdg, LONG_LINE)
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert events[0]["line"] == "ok" and len(events) == 1
    assert any("longer than 100 bytes" in record["text"] for record in kinds(records, "observer.output"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_observers.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'keepwatch.observers'`.

- [ ] **Step 3: Implement** — create `src/keepwatch/observers.py`:

```python
"""Observers: event sources the service keeps running for a watch (see `keepwatch docs observers`).

A command observer runs a long-lived program and turns each line it prints into an event. Each
observer runs on its own thread, restarts its source with backoff when it stops, and hands events
to a `deliver` callback.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from keepwatch import platform
from keepwatch.config import ObserverConfig, WatchConfig
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import iso_time
from keepwatch.platform import HookProcess
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, base_environment

Deliver = Callable[[dict[str, Any]], None]

BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
RESET_AFTER = 300.0
QUEUE_CAP = 10_000
MAX_LINE = 1024 * 1024
OUTPUT_RECORDS_PER_MINUTE = 20
STDERR_TAIL = 5
_SUPERVISE_STEP = 0.2


def parse_event(text: str) -> dict[str, Any] | None:
    """One line of a command observer's output as an event: a JSON object as-is, other text as {"line": text}.

    Returns None for blank lines and heartbeats ({"event": "heartbeat"}), which are not delivered.
    """
    if not text.strip():
        return None
    line = text.rstrip("\r\n")
    try:
        value = json.loads(line)
    except ValueError:
        return {"line": line}
    if not isinstance(value, dict):
        return {"line": line}
    if value.get("event") == "heartbeat":
        return None
    return value


class EventQueue:
    """Pending events for one watch, oldest first, at most `cap` (the oldest are dropped).

    Not thread-safe: the WatchRunner guards it with its lock.
    """

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self.cap = cap
        self.dropped = 0
        self._items: deque[tuple[int, dict[str, Any]]] = deque()
        self._next = 1

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: dict[str, Any]) -> bool:
        """Add an event; True if the oldest one was dropped to make room."""
        self._items.append((self._next, event))
        self._next += 1
        if len(self._items) > self.cap:
            self._items.popleft()
            self.dropped += 1
            return True
        return False

    def pending(self) -> tuple[int, list[dict[str, Any]]]:
        """(a mark for ack(), the pending events oldest first)."""
        return self._next - 1, [event for _, event in self._items]

    def ack(self, mark: int) -> None:
        """Remove the events up to `mark` (from pending()); later ones stay."""
        while self._items and self._items[0][0] <= mark:
            self._items.popleft()


def observer_environment(
    watch: WatchConfig, name: str, *, environment: Mapping[str, str], data_dir: Path
) -> dict[str, str]:
    """What an observer's program runs with: a hook's environment without the per-poll variables."""
    env = base_environment(environment, watch)
    env.update(
        {
            "KEEPWATCH_WATCH": watch.name,
            "KEEPWATCH_OBSERVER": name,
            "KEEPWATCH_WATCH_DIR": str(watch.watch_dir),
            "KEEPWATCH_DATA_DIR": str(data_dir),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


class Observer:
    """One observer's thread: runs the source, restarts it with backoff, and stops on request."""

    kind = ""

    def __init__(self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink) -> None:
        self.watch = watch
        self.config = config
        self.name = config.name
        self.restarts = 0
        self.last_event: float | None = None
        self._deliver = deliver
        self._sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tag(self) -> dict[str, Any]:
        return {"watch": self.watch.name, "observer": self.name}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"keepwatch-{self.watch.name}-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the observer to stop; join() waits until it has."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def emit(self, event: dict[str, Any]) -> None:
        """Tag an event with this observer's name and arrival time, log it (DEBUG) and deliver it."""
        now = time.time()
        self.last_event = now
        self._sink(make_record("observer.event", level="DEBUG", **self._tag(), data=event))
        try:
            self._deliver({**event, "observer": self.name, "received": iso_time(now)})
        except Exception as exc:
            self._crash(exc)

    def _crash(self, exc: Exception) -> None:
        self._sink(
            make_record(
                "observer.crash",
                level="ERROR",
                **self._tag(),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        )

    def _stopped(self, started: float, *, exit_code: int | None, reason: str, stderr_tail: list[str]) -> None:
        self._sink(
            make_record(
                "observer.stopped",
                level="INFO" if reason == "stopped" else "WARNING",
                **self._tag(),
                exit_code=exit_code,
                reason=reason,
                duration=round(time.monotonic() - started, 3),
                stderr_tail=stderr_tail,
            )
        )

    def _loop(self) -> None:
        delay = BACKOFF_START
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                self._crash(exc)
            if self._stop.is_set():
                break
            if time.monotonic() - started >= RESET_AFTER:
                delay = BACKOFF_START
            self.restarts += 1
            self._sink(make_record("observer.restarting", **self._tag(), delay=delay))
            self._stop.wait(delay)
            delay = min(delay * 2, BACKOFF_CAP)

    def run_once(self) -> None:
        """Run the source until it ends or stop() is called, logging observer.started and observer.stopped."""
        raise NotImplementedError


class _OutputLimiter:
    """observer.output records: at most OUTPUT_RECORDS_PER_MINUTE a minute, then one record counting the rest."""

    def __init__(self, sink: Sink, tag: dict[str, Any]) -> None:
        self._sink = sink
        self._tag = tag
        self._lock = threading.Lock()
        self._window = time.monotonic()
        self._count = 0
        self._suppressed = 0

    def line(self, text: str, stream: str = "stderr") -> None:
        with self._lock:
            if time.monotonic() - self._window >= 60.0:
                self._flush_locked()
                self._window, self._count = time.monotonic(), 0
            if self._count >= OUTPUT_RECORDS_PER_MINUTE:
                self._suppressed += 1
                return
            self._count += 1
        self._sink(make_record("observer.output", **self._tag, stream=stream, text=text))

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._suppressed:
            self._sink(
                make_record(
                    "observer.output",
                    level="WARNING",
                    **self._tag,
                    stream="stderr",
                    text=f"{self._suppressed} more line(s) not logged (limit {OUTPUT_RECORDS_PER_MINUTE} a minute)",
                    suppressed=self._suppressed,
                )
            )
            self._suppressed = 0


def _write_and_close(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class CommandObserver(Observer):
    """Runs `command` in its own process group / Job Object; each stdout line is an event."""

    kind = "command"

    def __init__(
        self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink, env: Mapping[str, str]
    ) -> None:
        super().__init__(watch, config, deliver=deliver, sink=sink)
        self._env = dict(env)
        self._process: HookProcess | None = None
        self._process_lock = threading.Lock()
        self._last_line = time.monotonic()
        self._output = _OutputLimiter(sink, self._tag())

    def argv(self) -> list[str]:
        command = self.config.command
        assert command is not None
        argv = command.to_argv(self.watch.shell)
        if command.argv is not None:
            argv = platform.command_argv(argv, self.watch.watch_dir)
        return argv

    def stop(self) -> None:
        super().stop()
        with self._process_lock:
            if self._process is not None:
                self._process.terminate()

    def run_once(self) -> None:
        argv = self.argv()
        started = time.monotonic()
        try:
            process = platform.start_process(
                argv,
                cwd=self.watch.watch_dir,
                env=self._env,
                stdin=subprocess.PIPE if self.config.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self._stopped(started, exit_code=None, reason=f"cannot start: {exc.strerror or exc}", stderr_tail=[])
            return
        with self._process_lock:
            self._process = process
        if self._stop.is_set():
            process.terminate()
        self._sink(make_record("observer.started", **self._tag(), kind=self.kind, argv=argv, pid=process.pid))
        self._last_line = time.monotonic()
        tail: deque[str] = deque(maxlen=STDERR_TAIL)
        threads = [
            threading.Thread(target=self._read_stdout, args=(process.popen.stdout,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(process.popen.stderr, tail), daemon=True),
        ]
        if self.config.stdin is not None:
            data = self.config.stdin.encode("utf-8")
            threads.append(threading.Thread(target=_write_and_close, args=(process.popen.stdin, data), daemon=True))
        for thread in threads:
            thread.start()
        reason = self._supervise(process)
        drain_until = time.monotonic() + DRAIN_GRACE
        for thread in threads:
            thread.join(max(drain_until - time.monotonic(), 0.0))
        if any(thread.is_alive() for thread in threads):
            # A leftover child still holds a pipe open: stop the whole tree.
            process.kill()
            for thread in threads:
                thread.join(DRAIN_GRACE)
        with self._process_lock:
            self._process = None
        process.close()
        if reason is None:
            reason = "stopped" if self._stop.is_set() else "exited"
        self._output.flush()
        self._stopped(started, exit_code=process.popen.returncode, reason=reason, stderr_tail=list(tail))

    def _supervise(self, process: HookProcess) -> str | None:
        """Wait for the process to end; end it on stop() or a heartbeat timeout. Returns why keepwatch ended it."""
        timeout = self.config.heartbeat_timeout
        reason: str | None = None
        kill_at: float | None = None
        while True:
            try:
                process.popen.wait(timeout=_SUPERVISE_STEP)
                return reason
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if kill_at is not None:
                if now >= kill_at:
                    process.kill()
                    kill_at = float("inf")
                continue
            if self._stop.is_set():
                reason = "stopped"
            elif timeout is not None and now - self._last_line > timeout:
                reason = f"no output for {format_duration(timeout)} (heartbeat_timeout)"
            if reason is not None:
                process.terminate()
                kill_at = now + KILL_GRACE

    def _read_stdout(self, stream: IO[bytes]) -> None:
        skipping = False
        try:
            while True:
                raw = stream.readline(MAX_LINE)
                if not raw:
                    return
                self._last_line = time.monotonic()
                complete = raw.endswith(b"\n")
                if skipping:
                    skipping = not complete
                    continue
                if not complete and len(raw) >= MAX_LINE:
                    skipping = True
                    self._output.line(f"dropped a line longer than {MAX_LINE} bytes", stream="stdout")
                    continue
                event = parse_event(raw.decode("utf-8", errors="replace"))
                if event is not None:
                    self.emit(event)
        finally:
            stream.close()

    def _read_stderr(self, stream: IO[bytes], tail: deque[str]) -> None:
        try:
            while True:
                raw = stream.readline(MAX_LINE)
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text.strip():
                    tail.append(text)
                    self._output.line(text)
        finally:
            stream.close()


def build_observer(
    watch: WatchConfig,
    config: ObserverConfig,
    *,
    deliver: Deliver,
    sink: Sink,
    environment: Mapping[str, str],
    data_dir: Path,
) -> Observer:
    """The Observer for one [observe.<name>] table (not started)."""
    if config.kind == "command":
        with contextlib.suppress(OSError):
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = observer_environment(watch, config.name, environment=environment, data_dir=data_dir)
        return CommandObserver(watch, config, deliver=deliver, sink=sink, env=env)
    raise ValueError(f"unknown observer kind {config.kind!r}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_observers.py -q`
Expected: all pass (about 15 s).

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/observers.py tests/test_observers.py
git commit -m "Observer runtime and command observers"
```

---

### Task 4: The `files` kind

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add`)
- Modify: `src/keepwatch/observers.py`
- Test: `tests/test_files_observer.py` (new)

**Interfaces:**
- Consumes: `Observer`, `build_observer`, `_stopped`, `emit` (Task 3); `ObserverConfig.path/pattern/ignore/recursive/settle` (Task 1).
- Produces:
  - `observers.FilesObserver(Observer)`; `build_observer` returns it for `kind == "files"`
  - events `{"event": "file", "path": <absolute str>, "name", "size": int, "mtime": float}`, once per (path, size, mtime_ns) while the file stays unchanged (an observer remembers across restarts of its source)
  - `observer.started` for files: `kind`, `path`, `native` (bool), `native_error` (str or None; WARNING level when native notifications failed and the observer falls back to rescanning every `FILES_POLL` seconds)
  - module constants: `FILES_RESCAN = 30.0`, `FILES_POLL = 2.0`, `FILES_SETTLE_STEP = 1.0`, `FILES_MIN_RESCAN = 0.5`

- [ ] **Step 1: Add the dependency**

Run: `uv add 'watchdog>=6'`
Expected: `pyproject.toml` `dependencies` becomes `["rich-click>=1.9", "watchdog>=6"]` (order as uv writes it) and `uv.lock` is updated.

- [ ] **Step 2: Write the failing tests** — create `tests/test_files_observer.py`:

```python
import os
import time
from pathlib import Path

import pytest

from keepwatch import observers
from keepwatch.config import load_watch_config
from keepwatch.observers import FilesObserver, build_observer
from portable import toml_path


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def age(path, seconds=60):
    old = time.time() - seconds
    os.utime(path, (old, old))


def files_observer(make_watch, xdg, inbox, extra=""):
    config = f"[observe.src]\nkind = 'files'\npath = {toml_path(inbox)}\nsettle = '1s'\n{extra}"
    watch = load_watch_config(make_watch("w", config=config))
    events, records = [], []
    observer = build_observer(
        watch,
        watch.observers["src"],
        deliver=events.append,
        sink=records.append,
        environment={},
        data_dir=xdg.watch_data_dir("w"),
    )
    return observer, events, records


def stop(observer):
    observer.stop()
    assert observer.join(15)


def test_reports_settled_files_once(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.gz").write_text("x")
    age(inbox / "old.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox)
    assert isinstance(observer, FilesObserver)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 1)
        (inbox / "new.gz").write_text("yy")
        assert wait_for(lambda: len(events) == 2)
        time.sleep(2.5)
        assert len(events) == 2
    finally:
        stop(observer)
    first = events[0]
    assert (first["event"], first["name"], first["size"], first["observer"]) == ("file", "old.gz", 1, "src")
    assert Path(first["path"]) == inbox / "old.gz"
    assert isinstance(first["mtime"], float)
    assert (events[1]["name"], events[1]["size"]) == ("new.gz", 2)
    started = kinds(records, "observer.started")[0]
    assert started["kind"] == "files" and started["native"] is True


def test_ignored_names_are_never_reported(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.gz", ".hidden", "x.tmp", "y.part", "z~"):
        (inbox / name).write_text("x")
        age(inbox / name)
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]


def test_pattern(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.gz", "b.txt"):
        (inbox / name).write_text("x")
        age(inbox / name)
    observer, events, records = files_observer(make_watch, xdg, inbox, "pattern = '*.gz'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]


def test_a_file_still_growing_is_not_reported_early(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    target = inbox / "copy.gz"
    target.write_text("x")
    age(target)  # like cp -p: the copy carries an old modification time
    observer, events, records = files_observer(make_watch, xdg, inbox, "settle = '2s'\n")
    observer.start()
    try:
        time.sleep(0.5)
        with target.open("a") as handle:
            handle.write("y")
        age(target)
        assert wait_for(lambda: events)
        time.sleep(1.0)
    finally:
        stop(observer)
    assert [event["size"] for event in events] == [2]


def test_recursive(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "a.gz").write_text("x")
    age(inbox / "sub" / "a.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "recursive = true\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert Path(events[0]["path"]) == inbox / "sub" / "a.gz"


def test_not_recursive_by_default(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "a.gz").write_text("x")
    age(inbox / "sub" / "a.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.started"))
        time.sleep(2.0)
    finally:
        stop(observer)
    assert events == []


def test_missing_directory_is_retried(tmp_path, make_watch, xdg):
    inbox = tmp_path / "later"
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
        inbox.mkdir()
        (inbox / "a.gz").write_text("x")
        age(inbox / "a.gz")
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert "does not exist" in first["reason"] and first["level"] == "WARNING"
    assert events[0]["name"] == "a.gz"
    assert kinds(records, "observer.stopped")[-1]["reason"] == "stopped"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_files_observer.py -q`
Expected: collection error, `ImportError: cannot import name 'FilesObserver'`.

- [ ] **Step 4: Implement** — in `src/keepwatch/observers.py`:

(a) Imports: add `import fnmatch`, `import os`, `import stat` to the standard-library block, and after the stdlib block add the third-party block (ruff orders it):

```python
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer as NativeObserver
```

(b) Constants, after `_SUPERVISE_STEP = 0.2`:

```python
FILES_RESCAN = 30.0  # rescan this often even without notifications (network shares can miss them)
FILES_POLL = 2.0  # rescan this often when native notifications are unavailable
FILES_SETTLE_STEP = 1.0  # rescan this often while a file is settling
FILES_MIN_RESCAN = 0.5  # at most two scans a second, however many notifications arrive
```

(c) Update the module docstring's first paragraph to: `A command observer runs a long-lived program and turns each line it prints into an event; a files observer reports settled files in a local directory. Each observer ...` (rest unchanged).

(d) Add before `build_observer`:

```python
class _Poke(FileSystemEventHandler):
    """Any filesystem notification just asks for a rescan."""

    def __init__(self, changed: threading.Event) -> None:
        super().__init__()
        self._changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._changed.set()


class FilesObserver(Observer):
    """Reports settled files in a local directory: native notifications (watchdog) trigger rescans."""

    kind = "files"

    def __init__(self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink) -> None:
        super().__init__(watch, config, deliver=deliver, sink=sink)
        self._reported: set[tuple[str, int, int]] = set()

    def _wanted(self, name: str) -> bool:
        if not fnmatch.fnmatch(name, self.config.pattern):
            return False
        return not any(fnmatch.fnmatch(name, pattern) for pattern in self.config.ignore)

    def _scan(self) -> dict[str, os.stat_result]:
        """Regular files to consider, by absolute path. Raises OSError if the directory is gone."""
        root = self.config.path
        assert root is not None
        names = os.listdir(root)
        if self.config.recursive:
            listing = [(Path(directory), files) for directory, _, files in os.walk(root)]
        else:
            listing = [(root, names)]
        found = {}
        for directory, entries in listing:
            for name in entries:
                if not self._wanted(name):
                    continue
                path = directory / name
                try:
                    info = path.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found[str(path)] = info
        return found

    def run_once(self) -> None:
        started = time.monotonic()
        root = self.config.path
        assert root is not None
        if not root.is_dir():
            self._stopped(started, exit_code=None, reason=f"directory {root} does not exist", stderr_tail=[])
            return
        changed = threading.Event()
        native: NativeObserver | None = NativeObserver()
        native_error = None
        try:
            native.schedule(_Poke(changed), str(root), recursive=self.config.recursive)
            native.start()
        except Exception as exc:  # OSError, or a platform-specific watchdog error
            native_error = f"{type(exc).__name__}: {exc}"
            native = None
        self._sink(
            make_record(
                "observer.started",
                level="INFO" if native_error is None else "WARNING",
                **self._tag(),
                kind=self.kind,
                path=str(root),
                native=native_error is None,
                native_error=native_error,
            )
        )
        try:
            reason = self._watch(changed, FILES_RESCAN if native_error is None else FILES_POLL)
        finally:
            if native is not None:
                native.stop()
                native.join(5.0)
        self._stopped(started, exit_code=None, reason=reason, stderr_tail=[])

    def _watch(self, changed: threading.Event, idle: float) -> str:
        """Scan, report settled files, wait for a notification or a timeout; repeat until stopped."""
        settle = self.config.settle
        candidates: dict[str, tuple[tuple[int, int], float]] = {}
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
            current = set()
            for path, info in sorted(found.items()):
                key = (info.st_size, info.st_mtime_ns)
                current.add((path, *key))
                if (path, *key) in self._reported:
                    continue
                seen = candidates.get(path)
                if seen is None or seen[0] != key:
                    seen = candidates[path] = (key, now)
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
            for path in set(candidates) - set(found):
                del candidates[path]
            self._reported &= current
            deadline = time.monotonic() + (FILES_SETTLE_STEP if candidates else idle)
            while not self._stop.is_set() and not changed.is_set() and time.monotonic() < deadline:
                changed.wait(min(0.2, max(deadline - time.monotonic(), 0.0)))
        return "stopped"
```

(e) In `build_observer`, before the final `raise`, add:

```python
    if config.kind == "files":
        return FilesObserver(watch, config, deliver=deliver, sink=sink)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_files_observer.py tests/test_observers.py -q`
Expected: all pass.

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add pyproject.toml uv.lock src/keepwatch/observers.py tests/test_files_observer.py
git commit -m "Files observers (watchdog notifications, settled files)"
```

---

### Task 5: Watches own their observers and events

**Files:**
- Modify: `src/keepwatch/scheduler.py`
- Test: `tests/test_scheduler_observers.py` (new)

**Interfaces:**
- Consumes: `EventQueue`, `Observer`, `build_observer` (Tasks 3-4); `PollEngine.poll(events=...)`, `PollEngine.global_config` (Task 2).
- Produces:
  - `WatchRunner.events: EventQueue`
  - `WatchRunner.add_event(event: dict[str, Any], *, wake: bool = True) -> None` (thread-safe)
  - `WatchRunner.sync_observers() -> None` (runner thread only; called each loop iteration)
  - `seconds_until_due` returns 0 when a waking event is pending, the watch is online and `state.failures == 0`
  - pending events go to every poll; they are acknowledged only when the poll did not fail and answered TRUE or FALSE
  - `snapshot()` gains `"pending_events": int` and `"observers": {name: {"kind", "running", "restarts", "last_event"}}`
  - record `observer.dropped` (WARNING; `observer`, `dropped` total, `cap`) on the first drop and every 1000th

- [ ] **Step 1: Write the failing tests** — create `tests/test_scheduler_observers.py`:

```python
import os
import time

import pytest

from keepwatch import observers
from keepwatch.config import load_global_config, load_watch_config
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, write_offline
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.scheduler import WatchRunner
from portable import PY, literal

RECORDING_WATCH = """
    def check(ctx):
        if (ctx.watch_dir / "fail").exists():
            raise RuntimeError("asked to fail")
        if (ctx.watch_dir / "unknown").exists():
            return None
        return True, [event["n"] for event in ctx.events]
"""
FEED = """
import json, os, time
print(json.dumps({"n": int(os.environ.get("FEED_N", "1"))}), flush=True)
time.sleep(60)
"""


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


@pytest.fixture
def runner_for(xdg):
    def make(watch_dir, records, clock):
        global_config = load_global_config(xdg.config_file, xdg)
        engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())
        return WatchRunner(
            load_watch_config(watch_dir),
            engine=engine,
            paths=xdg,
            sink=records.append,
            alert=lambda event, watch, reason: None,
            clock=clock,
        )

    return make


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def feed_config(extra=""):
    return f"interval = '1h'\n[observe.feed]\nkind = 'command'\ncommand = [{literal(PY)}, 'feed.py']\n{extra}"


def test_events_are_delivered_and_acknowledged(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    runner.add_event({"n": 2})
    report = runner.poll_once(clock())
    assert report.payload == [1, 2]
    assert len(runner.events) == 0


def test_a_failed_poll_keeps_the_events(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "fail").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    assert runner.poll_once(clock()).failed
    assert len(runner.events) == 1
    (watch_dir / "fail").unlink()
    clock.now += 3600
    assert runner.poll_once(clock()).payload == [1]
    assert len(runner.events) == 0


def test_an_unknown_poll_keeps_the_events(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "unknown").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    runner.poll_once(clock())
    assert len(runner.events) == 1


def test_a_waking_event_makes_the_watch_due(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.poll_once(clock())
    assert runner.seconds_until_due(clock()) == 60
    runner.add_event({"n": 1}, wake=False)
    assert runner.seconds_until_due(clock()) == 60
    runner.add_event({"n": 2})
    assert runner.seconds_until_due(clock()) == 0
    assert runner.poll_once(clock()).payload == [1, 2]
    assert runner.seconds_until_due(clock()) == 60


def test_events_do_not_cut_backoff_short(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "fail").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.poll_once(clock())
    runner.add_event({"n": 1})
    assert runner.seconds_until_due(clock()) > 0


def test_event_arriving_during_a_poll_is_kept_and_wakes(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    real_poll = runner._engine.poll

    def poll(*args, **kwargs):
        runner.add_event({"n": 2})
        return real_poll(*args, **kwargs)

    runner._engine.poll = poll
    assert runner.poll_once(clock()).payload == [1]
    assert runner.events.pending()[1] == [{"n": 2}]
    assert runner.seconds_until_due(clock()) == 0


def test_a_full_queue_drops_the_oldest_with_a_warning(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    records = []
    runner = runner_for(watch_dir, records, Clock())
    runner.events.cap = 2
    for number in (1, 2, 3, 4):
        runner.add_event({"n": number, "observer": "feed"})
    assert runner.events.pending()[1] == [{"n": 3, "observer": "feed"}, {"n": 4, "observer": "feed"}]
    [dropped] = kinds(records, "observer.dropped")
    assert (dropped["level"], dropped["observer"], dropped["dropped"], dropped["cap"]) == ("WARNING", "feed", 1, 2)


def test_observer_events_wake_the_running_watch(make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: any(r.get("payload") == [1] for r in kinds(records, "check.outcome")))
        # The acknowledgement follows the poll's last record, so wait for it rather than racing it.
        assert wait_for(lambda: runner.snapshot(time.time())["pending_events"] == 0)
        snapshot = runner.snapshot(time.time())
        assert snapshot["observers"]["feed"]["running"] is True
        assert snapshot["observers"]["feed"]["kind"] == "command"
        assert snapshot["observers"]["feed"]["last_event"] is not None
    finally:
        runner.stop()
        assert runner.join(20)
    assert kinds(records, "observer.stopped")[-1]["reason"] == "stopped"
    assert runner.snapshot(time.time())["observers"] == {}


def test_offline_stops_observers_and_online_restarts_them(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.started"))
        marker = OfflineMarker(reason="disabled by user", since=iso_time(time.time()), by_user=True)
        write_offline(xdg, "w", marker)
        assert wait_for(lambda: kinds(records, "observer.stopped"))
        assert kinds(records, "observer.stopped")[0]["reason"] == "stopped"
        clear_offline(xdg, "w")
        assert wait_for(lambda: len(kinds(records, "observer.started")) == 2)
    finally:
        runner.stop()
        assert runner.join(20)


def test_parked_watch_runs_no_observers(make_watch, runner_for):
    watch_dir = make_watch(
        "w", config="enabled = false\n" + feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED}
    )
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        time.sleep(1.5)
    finally:
        runner.stop()
        assert runner.join(20)
    assert not kinds(records, "observer.started")


def test_a_config_change_restarts_the_observers(make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: any(r.get("payload") == [1] for r in kinds(records, "check.outcome")))
        (watch_dir / "config.toml").write_text(feed_config("[environment]\nFEED_N = '7'\n"))
        runner.update_config(load_watch_config(watch_dir))
        assert wait_for(lambda: any(r.get("payload") == [7] for r in kinds(records, "check.outcome")))
    finally:
        runner.stop()
        assert runner.join(20)
    assert len(kinds(records, "observer.started")) == 2
```

Note on `feed_config`: the `[environment]` table must come after the `[observe.feed]` keys, which it does because `extra` is appended last.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_scheduler_observers.py -q`
Expected: FAIL — `AttributeError: 'WatchRunner' object has no attribute 'add_event'`.

- [ ] **Step 3: Implement** — in `src/keepwatch/scheduler.py`:

(a) Imports: add `import functools`; add `from keepwatch.observers import EventQueue, Observer, build_observer` and `from keepwatch.runner import DRAIN_GRACE, KILL_GRACE`. After `CRASH_RETRY = 60.0` add:

```python
OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0
DROP_REPORT_EVERY = 1000
```

(b) In `WatchRunner.__init__`, after `self._thread: threading.Thread | None = None`, add:

```python
        self.events = EventQueue()
        self._wake_requested = False
        self._observers: dict[str, Observer] = {}
        self._observers_lock = threading.Lock()
        self._observer_key: Any = None
```

(c) Add after `update_config`:

```python
    def add_event(self, event: dict[str, Any], *, wake: bool = True) -> None:
        """Queue an observer event for the next poll; with wake, poll as soon as the schedule allows. Thread-safe."""
        with self._lock:
            dropped = self.events.put(event)
            total = self.events.dropped
            if wake:
                self._wake_requested = True
        if dropped and (total == 1 or total % DROP_REPORT_EVERY == 0):
            self._sink(
                make_record(
                    "observer.dropped",
                    level="WARNING",
                    watch=self.name,
                    observer=event.get("observer"),
                    dropped=total,
                    cap=self.events.cap,
                )
            )
        if wake:
            self._wake.set()

    def _observers_wanted(self) -> Any:
        """What the running observers depend on, or None when none should run."""
        config = self._config
        if self._stop.is_set() or not config.enabled or self.offline is not None or not config.observers:
            return None
        environment = self._engine.global_config.environment
        return (config.observers, config.watch_dir, config.shell, config.environment, config.settings, environment)

    def sync_observers(self) -> None:
        """Start, stop or restart this watch's observers to match its config and state. Runner thread only."""
        self._apply_pending()
        key = self._observers_wanted()
        if key == self._observer_key:
            return
        self._stop_observers()
        # Recorded before starting: a failure below is logged once instead of retried every second.
        self._observer_key = key
        if key is None:
            return
        config = self._config
        for name, observer_config in config.observers.items():
            observer = build_observer(
                config,
                observer_config,
                deliver=functools.partial(self.add_event, wake=observer_config.wake),
                sink=self._sink,
                environment=self._engine.global_config.environment,
                data_dir=self._paths.watch_data_dir(self.name),
            )
            with self._observers_lock:
                self._observers[name] = observer
            observer.start()

    def _stop_observers(self) -> None:
        with self._observers_lock:
            running, self._observers = list(self._observers.values()), {}
        for observer in running:
            observer.stop()
        for observer in running:
            observer.join(OBSERVER_JOIN)
```

(d) In `seconds_until_due`, replace the last line `return max(self.next_due - now, 0.0)` with:

```python
        if self._wake_requested and self.state.failures == 0:
            return 0.0
        return max(self.next_due - now, 0.0)
```

(e) In `poll_once`, right after `config = self._config` (before `with hold_lock(...)`), add:

```python
        with self._lock:
            self._wake_requested = False
            mark, events = self.events.pending()
```

change the engine call to `report = self._engine.poll(config, self.state, trial=trial, events=events)`, and right after `self.state = report.after` add:

```python
        if not report.failed and report.outcome in ANSWERS:
            with self._lock:
                self.events.ack(mark)
```

(f) In `snapshot`, add two entries after `"config_error": self.config_error,`:

```python
            "pending_events": len(self.events),
            "observers": self._observer_status(),
```

and add the method after `snapshot`:

```python
    def _observer_status(self) -> dict[str, Any]:
        with self._observers_lock:
            running = dict(self._observers)
        return {
            name: {
                "kind": observer.kind,
                "running": observer.running,
                "restarts": observer.restarts,
                "last_event": iso_time(observer.last_event) if observer.last_event is not None else None,
            }
            for name, observer in sorted(running.items())
        }
```

(g) Replace `_loop` with:

```python
    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.sync_observers()
                    report = self.poll_once(self._clock())
                except Exception as exc:
                    self._sink(
                        make_record(
                            "watch.crash",
                            level="ERROR",
                            watch=self.name,
                            error=f"{type(exc).__name__}: {exc}",
                            traceback=traceback.format_exc(),
                        )
                    )
                    self.next_due = self._clock() + CRASH_RETRY
                    report = None
                if report is None:
                    wait = self.seconds_until_due(self._clock())
                    self._wake.wait(1.0 if wait is None else min(wait, 1.0))
                    self._wake.clear()
        finally:
            self._stop_observers()
```

(h) Replace `stop` with:

```python
    def stop(self) -> None:
        """Stop after the current poll, and stop the observers. Polls that end after this never change offline state."""
        self._stop.set()
        self._wake.set()
        with self._observers_lock:
            running = list(self._observers.values())
        for observer in running:
            observer.stop()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_scheduler_observers.py tests/test_scheduler.py tests/test_service.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/scheduler.py tests/test_scheduler_observers.py
git commit -m "Watches run their observers; events wake polls and are acknowledged by successful polls"
```

---

### Task 6: `poll --events`, `keepwatch observe`, console formatting

**Files:**
- Modify: `src/keepwatch/cli.py`, `src/keepwatch/output.py`
- Test: `tests/test_cli_observe.py` (new)

**Interfaces:**
- Consumes: `build_observer` (Tasks 3-4), `PollEngine.poll(events=...)` (Task 2).
- Produces:
  - `keepwatch poll NAME --events JSON` (a JSON array of objects; each gets `"observer": "manual"` and `"received"` unless present; given to every poll of the run)
  - `keepwatch observe NAME [OBSERVER] [--for DURATION] [--count N]`; events as JSON lines on stdout, records on stderr
  - console formatters for `observer.started`, `observer.stopped`, `observer.restarting`, `observer.output`, `observer.event`, `observer.dropped`, `observer.crash`

- [ ] **Step 1: Write the failing tests** — create `tests/test_cli_observe.py`:

```python
import json

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.output import format_record
from portable import PY, literal

FEED = """
import json, time
print(json.dumps({"n": 1}))
print(json.dumps({"n": 2}), flush=True)
time.sleep(60)
"""
SILENT = "import time\ntime.sleep(60)\n"
NAMES_WATCH = """
    def check(ctx):
        return True, [event["observer"] + ":" + event["name"] for event in ctx.events]
"""


def run(*args):
    return CliRunner().invoke(cli, list(args))


def observed(make_watch, script, name="feed"):
    config = f"[observe.{name}]\nkind = 'command'\ncommand = [{literal(PY)}, 'source.py']\n"
    return make_watch("w", config=config, files={"source.py": script})


def test_observe_prints_events_as_json_lines(make_watch):
    observed(make_watch, FEED)
    result = run("observe", "w", "--count", "2", "--for", "30s")
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["n"] for line in lines] == [1, 2]
    assert all(line["observer"] == "feed" and line["received"] for line in lines)
    assert "observer feed started" in result.stderr


def test_observe_for_a_while(make_watch):
    observed(make_watch, SILENT)
    result = run("observe", "w", "feed", "--for", "1s")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_observe_unknown_observer(make_watch):
    observed(make_watch, SILENT)
    result = run("observe", "w", "nope", "--for", "1s")
    assert result.exit_code == 1
    assert "watch 'w' has no observer 'nope'; its observers: feed" in result.output


def test_observe_a_watch_without_observers(make_watch):
    make_watch("w", config="")
    result = run("observe", "w", "--for", "1s")
    assert result.exit_code == 1
    assert "has no observers" in result.output and "keepwatch docs observers" in result.output


def test_observe_bad_options(make_watch):
    observed(make_watch, SILENT)
    assert run("observe", "w", "--for", "soon").exit_code == 2
    assert run("observe", "w", "--count", "0").exit_code == 2


def test_poll_events_option(make_watch):
    make_watch("w", files={"watch.py": NAMES_WATCH})
    result = run("poll", "w", "--events", '[{"name": "a"}, {"name": "b", "observer": "inbox"}]', "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["polls"][0]["payload"] == ["manual:a", "inbox:b"]


def test_poll_events_must_be_an_array_of_objects(make_watch):
    make_watch("w", files={"watch.py": NAMES_WATCH})
    for bad in ('{"name": "a"}', "[1]", "not json"):
        result = run("poll", "w", "--events", bad)
        assert result.exit_code == 2, bad


def test_observer_records_are_formatted():
    stopped = {
        "ts": "2026-10-01T12:00:00.000+00:00",
        "event": "observer.stopped",
        "watch": "w",
        "observer": "feed",
        "reason": "exited",
        "exit_code": 255,
        "stderr_tail": ["ssh: connect to host x port 22: Connection refused"],
    }
    text = format_record(stopped)
    assert "observer feed stopped: exited [exit 255]" in text and "Connection refused" in text
    restarting = {"event": "observer.restarting", "observer": "feed", "delay": 10.0}
    assert "observer feed restarting in 10s" in format_record(restarting)
    started = {"event": "observer.started", "observer": "in", "kind": "files", "path": "/x", "native": True}
    assert "observer in started (files): /x" in format_record(started)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_observe.py -q`
Expected: FAIL — `No such command 'observe'` / `No such option: --events`.

- [ ] **Step 3: Implement `src/keepwatch/output.py`**

Add `from keepwatch.durations import format_duration` to the imports. Add before `_SKIP = ...`:

```python
def _observer_started(record: dict[str, Any], verbose: bool) -> str:
    source = record.get("path") or " ".join(str(item) for item in record.get("argv") or [])
    line = f"observer {record.get('observer')} started ({record.get('kind')}): {source}"
    if record.get("native") is False:
        line += f" [no notifications, rescanning: {record.get('native_error')}]"
    return line


def _observer_stopped(record: dict[str, Any], verbose: bool) -> str:
    line = f"observer {record.get('observer')} stopped: {record.get('reason')}"
    if record.get("exit_code") is not None:
        line += f" [exit {record['exit_code']}]"
    tail = record.get("stderr_tail") or []
    if tail and (verbose or record.get("reason") != "stopped"):
        line += _block("stderr", "\n".join(tail))
    return line


def _observer_restarting(record: dict[str, Any], verbose: bool) -> str:
    return f"observer {record.get('observer')} restarting in {format_duration(float(record.get('delay') or 0))}"


def _observer_output(record: dict[str, Any], verbose: bool) -> str:
    return f"observer {record.get('observer')} {record.get('stream')}: {record.get('text')}"


def _observer_event(record: dict[str, Any], verbose: bool) -> str:
    data = json.dumps(record.get("data"), ensure_ascii=False, default=str)
    return f"observer {record.get('observer')} event: {data[:2000]}"


def _observer_dropped(record: dict[str, Any], verbose: bool) -> str:
    return f"event queue full ({record.get('cap')} events): {record.get('dropped')} oldest event(s) dropped so far"
```

and add to `_FORMATTERS`:

```python
    "observer.started": _observer_started,
    "observer.stopped": _observer_stopped,
    "observer.restarting": _observer_restarting,
    "observer.output": _observer_output,
    "observer.event": _observer_event,
    "observer.dropped": _observer_dropped,
    "observer.crash": _watch_crash,
```

- [ ] **Step 4: Implement `src/keepwatch/cli.py`**

(a) Imports: add `from keepwatch.durations import DurationError, parse_duration`, `from keepwatch.observers import build_observer`, `from keepwatch.offline import iso_time`, and change `from keepwatch.runner import Runner` to `from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, Runner`.

(b) `COMMAND_GROUPS`: the Develop group becomes `{"name": "Develop", "commands": ["new", "validate", "poll", "observe"]},`.

(c) `poll`: add this option right after the `--payload` option:

```python
@click.option(
    "--events",
    "events_json",
    metavar="JSON",
    help='Observer events for the hooks (ctx.events, KEEPWATCH_EVENTS_FILE): a JSON array of objects, e.g. '
    '\'[{"event": "file", "name": "a.tar.gz"}]\'. Each gets "observer": "manual" and "received" unless it has '
    "them. Every poll of this run gets the same events. Default: none (a manual poll runs no observers).",
)
```

add `events_json: str | None,` to the function parameters after `payload_json: str | None,`; append this paragraph to the docstring, after the paragraph that ends "never run the same watch at once.":

```
    Observers do not run during a manual poll: hooks see the events given with --events, or none.
```

after the `--payload` validation block (before `global_config, watch = _load_one(app, name)`) add:

```python
    events: list[dict] = []
    if events_json is not None:
        try:
            parsed = json.loads(events_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--events") from None
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise click.BadParameter("must be a JSON array of objects", param_hint="--events")
        received = iso_time(time.time())
        events = [{"observer": "manual", "received": received, **item} for item in parsed]
```

and change the poll call to `report = engine.poll(watch, state, fake=fake, dry_run=dry_run, events=events)`.

(d) Add the command right after the `poll` command (before `validate`):

```python
@cli.command()
@click.argument("name")
@click.argument("observer", required=False)
@click.option(
    "--for",
    "duration",
    metavar="DURATION",
    help='Stop after this long, e.g. "30s" or "5m". Default: run until interrupted (Ctrl-C).',
)
@click.option("--count", type=int, metavar="N", help="Stop after N events.")
@click.pass_obj
def observe(app: App, name: str, observer: str | None, duration: str | None, count: int | None) -> None:
    """Run watch NAME's observers (or only OBSERVER) in the foreground and print their events.

    Each event is printed to stdout as one JSON line, exactly as hooks receive it in ctx.events. Observer
    records (started, stopped, restarting, stderr output) go to stderr; nothing is written to the log file
    and the watch is not polled. It runs its own copies of the observers, so it does not disturb a running
    service. Agents: always pass --for or --count, since there is no Ctrl-C.

    Exit status: 0 when stopped by --for, --count or Ctrl-C; 1 if the watch cannot be loaded, has no
    observers or has no observer named OBSERVER; 2 for bad usage.
    """
    seconds = None
    if duration is not None:
        try:
            seconds = parse_duration(duration)
        except DurationError as exc:
            raise click.BadParameter(str(exc), param_hint="--for") from None
    if count is not None and count < 1:
        raise click.BadParameter("must be at least 1", param_hint="--count")
    global_config, watch = _load_one(app, name)
    if not watch.observers:
        _fail(f"watch '{name}' has no observers; add an [observe.<name>] table (see: keepwatch docs observers)")
    if observer is not None and observer not in watch.observers:
        _fail(f"watch '{name}' has no observer '{observer}'; its observers: {', '.join(watch.observers)}")
    done = threading.Event()
    lock = threading.Lock()
    seen = 0

    def deliver(event: dict) -> None:
        nonlocal seen
        with lock:
            if done.is_set():
                return
            click.echo(json.dumps(event, ensure_ascii=False, default=str))
            seen += 1
            if count is not None and seen >= count:
                done.set()

    printer = ConsolePrinter(make_console(stderr=True))
    running = [
        build_observer(
            watch,
            watch.observers[key],
            deliver=deliver,
            sink=printer,
            environment=global_config.environment,
            data_dir=app.paths.watch_data_dir(watch.name),
        )
        for key in ([observer] if observer else list(watch.observers))
    ]
    for item in running:
        item.start()
    deadline = None if seconds is None else time.monotonic() + seconds
    try:
        while not done.wait(0.5):  # short waits: Ctrl-C cannot interrupt an endless wait on Windows
            if deadline is not None and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        for item in running:
            item.stop()
        for item in running:
            item.join(KILL_GRACE + DRAIN_GRACE + 1.0)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_cli_observe.py tests/test_cli_poll.py tests/test_docs.py tests/test_output.py -q`
Expected: all pass (`test_every_command_and_option_is_documented` covers the new command and options).

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/cli.py src/keepwatch/output.py tests/test_cli_observe.py
git commit -m "keepwatch observe, poll --events, console lines for observer records"
```

---

### Task 7: Documentation

**Files:**
- Create: `src/keepwatch/reference/observers.md`
- Modify: `src/keepwatch/reference.py` (`TOPICS`), `src/keepwatch/reference/executables.md`, `src/keepwatch/reference/ctx.md`, `src/keepwatch/reference/logging.md`, `src/keepwatch/reference/agent.md`
- Modify: `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (the `files` kind gains `ignore`)
- Test: `tests/test_docs.py` (`KEY_FACTS`)

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`, `KEY_FACTS`: add the entry

```python
    "observers": ["[observe.", "./feed.py", "uv\", \"run\", \"--script", "ctx.events", "KEEPWATCH_EVENTS_FILE", "at-least-once", "heartbeat_timeout", "keepwatch observe", "--events", "wake", "settle", "flush", "ledger"],
```

append `"KEEPWATCH_EVENTS_FILE"` to the `"executables"` list and `"observer.stopped"` to the `"logging"` list.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — `UnknownTopic: no docs topic 'observers'` and missing facts.

- [ ] **Step 3: Write the docs**

(a) `src/keepwatch/reference.py`, `TOPICS`: insert after the `("executables", ...)` entry:

```python
    ("observers", "Event sources the service keeps running: [observe.*], ctx.events, delivery, keepwatch observe"),
```

(b) Create `src/keepwatch/reference/observers.md`:

````markdown
# Observers: events instead of (or as well as) polling

A watch polls on a schedule. Some things are better learned the moment they happen: a file landing in a directory, a line appearing in a log, a message from a long-running ssh session. An **observer** is an event source the service keeps running for a watch, independently of its polling schedule. Its events are queued and handed to the watch's next poll, and by default an event makes that poll happen at once.

The check still decides. It sees the pending events in `ctx.events`, turns them into its answer and payload, and the actions run as usual. Everything else (state, retries, backoff, offline, logging) works as for any watch.

## Declaring observers

One `[observe.<name>]` table per observer in the watch's config.toml. Names use letters, digits, `_` and `-`. Every key is listed in `keepwatch docs config`.

```toml
[observe.inbox]
kind = "files"              # a local directory
path = 'C:/data/inbox'      # relative paths are relative to the watch directory
pattern = "*.tar.gz"
settle = "10s"

[observe.feed]
kind = "command"            # a long-running program
command = ["./feed.py"]     # see "Python programs" below
heartbeat_timeout = "2m"
wake = true                 # the default: poll as soon as an event arrives
```

### Python programs

Write `command = ["./feed.py"]`, not `["python", "feed.py"]`. A relative `.py` program runs with the Python that runs keepwatch: on Windows keepwatch starts it with that interpreter, and on POSIX the script's `#!` line does (make it executable). A bare `python` is unreliable: on Windows it may be the Microsoft Store placeholder, and elsewhere it may be missing or a different version. If the program needs third-party packages, declare them in the script itself (PEP 723 inline metadata) and run it with `command = ["uv", "run", "--script", "./feed.py"]`. uv then picks an interpreter and builds a cached environment on every OS.

### kind = "command"

`command` runs as a long-lived child process: working directory the watch directory, in its own process group (POSIX) or Job Object (Windows), with the hooks' environment minus the per-poll variables (`KEEPWATCH_WATCH`, `KEEPWATCH_OBSERVER`, `KEEPWATCH_WATCH_DIR`, `KEEPWATCH_DATA_DIR` are set; `PYTHONUNBUFFERED=1` too). With `stdin`, that text is written to its standard input once, then stdin is closed; otherwise stdin is empty.

- **Each line it prints on stdout is one event.** A line holding a JSON object is used as-is; any other line becomes `{"line": "<text>"}`. Lines longer than 1 MiB are dropped.
- **Flush stdout after every line.** Programs writing to a pipe often buffer output in blocks, so events would arrive late or in bursts (`print(..., flush=True)`, `stdbuf -oL`, `sys.stdout.flush()`).
- `{"event": "heartbeat"}` lines are not delivered; they only prove the program is alive. With `heartbeat_timeout`, a program that prints nothing (heartbeats included) for that long is stopped and restarted.
- stderr lines are logged as `observer.output` records (at most 20 a minute; the rest are counted). The last 5 are also on the `observer.stopped` record.
- When the program exits (or is stopped for silence) it is restarted after 5s, then 10s, 20s … up to 5 minutes; a run that lasted 5 minutes resets the delay to 5s.

### kind = "files"

Observes a local directory (`recursive = true` for subdirectories too) through the operating system's notifications (inotify, ReadDirectoryChangesW, FSEvents), rescanning on every notification and at least every 30 seconds. When notifications are unavailable (some network shares), it rescans every 2 seconds and says so on `observer.started` (`native = false`).

A file is reported once it has **settled**: its size and modification time have not changed for `settle` (default 10s) and it was last modified at least `settle` ago. The first condition catches copies that preserve an old modification time (`cp -p`, `scp -p`, tar). Names must match `pattern` and none of `ignore` (default `.*`, `*.tmp`, `*.part`, `*~`: hidden files and files still being written by tools that rename when done).

Each settled file is reported once per (path, size, modification time) while the service runs:

```json
{"event": "file", "path": "/data/inbox/a.tar.gz", "name": "a.tar.gz", "size": 1048576, "mtime": 1790000000.5, "observer": "inbox", "received": "2026-10-01T14:00:03.120+02:00"}
```

A missing directory is retried with the same backoff as a command, so an observer may be configured before its directory exists.

## What hooks receive

- **Python:** `ctx.events`, a list of dicts, oldest first. Empty when there are none.
- **Commands:** `KEEPWATCH_EVENTS_FILE`, a JSON file holding the array (`[]` when there are none).

Every event carries `"observer"` (the observer's name) and `"received"` (when keepwatch got it), which replace keys of the same name. All hooks of a poll (check and actions) see the same events; the check normally passes what the actions need on in its payload.

```python
def check(ctx):
    pulled = ctx.ledger("done")
    new = [e for e in ctx.events if e.get("event") == "file" and ctx.file_key(e["path"]) not in pulled]
    return bool(new), [e["path"] for e in new]


def on_true(ctx):
    done = ctx.ledger("done")
    for path in ctx.payload:
        ctx.run(["process", path])
        done.add(ctx.file_key(path))
```

## Delivery rules

- Events wait in a per-watch queue (at most 10 000; when full the oldest are dropped with an `observer.dropped` WARNING).
- **Events are acknowledged only by a successful poll:** one that did not fail and answered TRUE or FALSE. After a failed or unknown poll the same events (plus newer ones) come again: delivery is **at-least-once**. Record what you handled in a ledger and skip it next time, as above.
- With `wake = true` an event makes the watch poll at once, unless it is backing off after failures or is offline (then it waits for its schedule). With `wake = false` events just wait for the next scheduled poll.
- Observers start with their watch and stop when it is removed, parked (`enabled = false`), disabled or offline; any change to the watch's config restarts them. Events still queued when a watch goes offline are delivered to its trial polls.
- A **files** event can be stale by the time the hook runs (the file was moved or deleted); check that it still exists.

## Developing observers

- `keepwatch observe <watch> [<observer>] --for 30s` runs the observers in the foreground and prints each event as one JSON line on stdout, exactly as hooks receive it; observer records go to stderr. Agents: always pass `--for` or `--count`.
- `keepwatch poll <watch> --events '[{"event": "file", "path": "/tmp/a.gz", "name": "a.gz"}]'` hands events to a manual poll (manual polls run no observers, so `ctx.events` is otherwise empty).
- `keepwatch status --json` shows each watch's `pending_events` and its `observers` (running, restarts, last event).
- Records: `observer.started`, `observer.event` (DEBUG, every event), `observer.output`, `observer.stopped` (`reason`, `exit_code`, `stderr_tail`), `observer.restarting` (`delay`), `observer.dropped`, `observer.crash` (a bug in keepwatch). `keepwatch logs <watch> --event observer.stopped` shows why an observer keeps restarting.
````

(c) `src/keepwatch/reference/executables.md`: in the variable table, add a row after the `KEEPWATCH_SETTING_<KEY>` row:

```markdown
| `KEEPWATCH_EVENTS_FILE` | A JSON file holding this poll's observer events, oldest first (`[]` when there are none). See `keepwatch docs observers`. |
```

(d) `src/keepwatch/reference/ctx.md`: add a row after the `ctx.payload` row:

```markdown
| `ctx.events` | `list[dict]` | Observer events delivered with this poll, oldest first; empty without observers. See `keepwatch docs observers`. |
```

(e) `src/keepwatch/reference/logging.md`: change the `poll.start` row to ``| `poll.start` | `condition`, `faked`, `dry_run`, `trial`, `events` (how many observer events the poll got) |`` and add rows before the `alert.end` row:

```markdown
| `observer.started` | `observer`, `kind`; `argv` and `pid` (command) or `path`, `native`, `native_error` (files) |
| `observer.event` | DEBUG: `observer`, `data` (the event as the source produced it) |
| `observer.output` | `observer`, `stream`, `text`; `suppressed` on the record counting lines over the limit |
| `observer.stopped` / `observer.restarting` | `observer`; `reason`, `exit_code`, `duration`, `stderr_tail` / `delay` |
| `observer.dropped` | `observer`, `dropped` (total so far), `cap`: the event queue was full |
| `observer.crash` | `observer`, `error`, `traceback` (a bug in keepwatch itself; the observer restarts) |
```

(f) `src/keepwatch/reference/agent.md`, "Common mistakes": add a bullet at the end of the list:

```markdown
- Assuming each observer event arrives exactly once. Delivery is at-least-once (events come again after a failed or unknown poll); record handled items in a ledger. See `keepwatch docs observers`.
```

(g) Spec: in `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md`, section 2.1, change ``(`path`, optional `pattern`, `recursive = false`)`` to ``(`path`, optional `pattern`, `ignore` (default `.*`, `*.tmp`, `*.part`, `*~`), `recursive = false`)``.

- [ ] **Step 4: Run the docs tests and look at the result**

Run: `uv run pytest tests/test_docs.py -q`, then `uv run keepwatch docs observers` and `uv run keepwatch docs config` and check the observer key table renders (the `ignore` default shows as `` `[".*", "*.tmp", "*.part", "*~"]` ``).
Expected: tests pass; both topics print without errors.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/reference.py src/keepwatch/reference/observers.md src/keepwatch/reference/executables.md src/keepwatch/reference/ctx.md src/keepwatch/reference/logging.md src/keepwatch/reference/agent.md tests/test_docs.py docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md
git commit -m "Document observers"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, and anything in this plan you had to question. Do not push; the supervisor pushes the branch and checks the Windows CI jobs.
