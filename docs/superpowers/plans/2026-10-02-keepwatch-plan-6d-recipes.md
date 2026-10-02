# keepwatch Plan 6d (Recipes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Built-in recipes, so the common "move files between machines" watches need only a config.toml: `recipe = "pull"` (a remote directory watched over ssh → verified pulls into a local staging folder) and `recipe = "push"` (a local folder → uploads with a `.sha256` marker when the destination is reachable, then delete/archive/keep). Also: remote paths that survive scp's remote shell, and reconnects that do not re-hash files already pulled.

**Architecture:** `config.py` gains the watch key `recipe`, per-recipe settings schemas (`RECIPE_SETTINGS`, validated, defaults filled into `[settings]`) and the observers each recipe needs (`pull`: a `remote_files` observer with `skip_ledger = "pulled"`; `push`: a `files` observer). The hooks live in `keepwatch/recipes/pull.py` and `push.py` (stdlib only, run by the Python worker like watch.py). `hooks.watch_hooks` reports a recipe's hooks; the runner sends `recipe` in the worker request; the worker imports the recipe module instead of watch.py. The remote watcher learns a `skip` option, fed from a ledger at every (re)connect.

**Tech Stack:** as Plan 6c.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (sections 4 "Remote paths", 5). Builds on Plans 6a-6c (implemented). Spec deviations, recorded in Task 6: the `pull` recipe has no `dest` (direct `scp -3`) and no `password_env` (its observer needs key authentication anyway); `push` gains `reachable_host` (a Host alias in `~/.ssh/config` is not a DNS name).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests, stop and report on plan defects, attribution trailer).
- `remote_watcher.py` and its selftest stay Python 3.6-compatible and ASCII. `keepwatch/recipes/*.py` and `transfer.py` stay stdlib-only (they run inside the worker).
- Ledger keys for remote files are `"<path>|<size>|<mtime!r>"` on both sides (the remote watcher and the pull recipe); `repr` of the same float is the same text on Python 3.6 and 3.12.

## Review Focus

- A remote file name with a space must transfer with the classic protocol. Test: Task 1 `test_pull_a_name_with_a_space`.
- After a reconnect, files already pulled must be neither hashed nor reported. Tests: Task 2 `test_skipped_files_are_not_reported`, `test_skip_ledger_files_are_not_reported`.
- When the destination is unreachable, `push` must answer unknown (no failure, no backoff) and leave the files. Test: Task 5 `test_unreachable_destination_is_unknown`.
- A watch with `recipe` and its own `watch.py` or `[hooks]` must be a config error, not a silent mix. Test: Task 3 `test_recipe_watches_have_no_hooks_of_their_own`.
- A pull that fails verification must not be recorded as pulled. Test: Task 4 `test_pull_recipe_does_not_record_a_failed_pull`.

---

### Task 1: Remote paths survive scp's remote shell

**Files:**
- Modify: `src/keepwatch/transfer.py` (`Endpoint.scp_arg`)
- Modify: `tests/test_transfer.py`, `tests/test_transfer_scp.py`

**Interfaces:**
- Produces: `transfer.escape_remote_path(path: str) -> str` — a `\` before every character outside `[A-Za-z0-9_./:@%+,=-]`, keeping a leading `~/` (or a lone `~`) for tilde expansion. `Endpoint.scp_arg()` applies it to `user@host:path` endpoints (not to local paths or `scp://` URIs).

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_transfer.py`: add `escape_remote_path` to the `keepwatch.transfer` import and append:

```python
def test_escape_remote_path():
    assert escape_remote_path("/data/out/a.tar.gz") == "/data/out/a.tar.gz"
    assert escape_remote_path("/data/a b.tar.gz") == "/data/a\\ b.tar.gz"
    assert escape_remote_path("it's $HOME;(x)") == "it\\'s\\ \\$HOME\\;\\(x\\)"
    assert escape_remote_path("~/incoming/a b") == "~/incoming/a\\ b"
    assert escape_remote_path("~") == "~"
    assert escape_remote_path("dir/~x") == "dir/\\~x"


def test_remote_paths_are_escaped_in_scp_arguments():
    assert parse_endpoint("me@h:/out/a b.gz").scp_arg() == "me@h:/out/a\\ b.gz"
    assert parse_endpoint("/local/a b.gz").scp_arg() == "/local/a b.gz"
    assert parse_endpoint("me@h:in/").child("a b.gz").scp_arg() == "me@h:in/a\\ b.gz"
```

(b) `tests/test_transfer_scp.py`, append:

```python
def test_pull_a_name_with_a_space(tmp_path, server, ssh_config):
    (server.root / "a b.tar.gz").write_bytes(b"spaced")
    final = pull(REMOTE + "a b.tar.gz", tmp_path / "stage", size=6, options=key_options(server, ssh_config))
    assert final.name == "a b.tar.gz" and final.read_bytes() == b"spaced"


def test_push_a_name_with_a_space(tmp_path, server, ssh_config):
    local = tmp_path / "c d.txt"
    local.write_bytes(b"up")
    assert push(local, REMOTE, options=key_options(server, ssh_config)) == "u@127.0.0.1:c\\ d.txt"
    assert (server.root / "c d.txt").read_bytes() == b"up"
    assert (server.root / "c d.txt.sha256").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py -q`
Expected: FAIL — `cannot import name 'escape_remote_path'`.

- [ ] **Step 3: Implement** — in `src/keepwatch/transfer.py`, add after the `_VERSION = …` line:

```python
_SAFE = re.compile(r"[A-Za-z0-9_./:@%+,=-]")


def escape_remote_path(path: str) -> str:
    """Backslash-escape a remote path for scp, whose classic protocol hands it to the remote shell.

    Backslashes are the one form that works in both protocols (spike 2026-10-02: single quotes fail in both).
    A leading `~/` (or a lone `~`) is kept so that it still means the remote home.
    """
    if path == "~":
        return path
    prefix = "~/" if path.startswith("~/") else ""
    rest = path[len(prefix) :]
    return prefix + "".join(char if _SAFE.fullmatch(char) else "\\" + char for char in rest)
```

and in `Endpoint.scp_arg`, change the last line `return f"{who}:{self.path}"` to:

```python
        return f"{who}:{escape_remote_path(self.path)}"
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_ctx_transfer.py tests/test_cli_kit.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/transfer.py tests/test_transfer.py tests/test_transfer_scp.py
git commit -m "Backslash-escape remote paths for scp"
```

---

### Task 2: `skip`: reconnects without re-hashing

**Files:**
- Modify: `src/keepwatch/remote_watcher.py`, `tests/remote_watcher_selftest.py`
- Modify: `src/keepwatch/config.py` (`ObserverConfig.skip_ledger`, `OBSERVER_KEYS`, `_OBSERVER_KIND_KEYS`, `_observer`), `src/keepwatch/observers.py` (`RemoteFilesObserver`, `build_observer`)
- Modify: `tests/test_config_observe.py`, `tests/test_remote_files_observer.py`

**Interfaces:**
- Produces:
  - remote watcher option `skip` (default `[]`): keys `"<path>|<size>|<mtime!r>"` of files that count as already reported (not hashed, not reported) while they keep that size and mtime
  - `ObserverConfig.skip_ledger: str | None = None`; observer key `skip_ledger` (kind `remote_files`; a ledger name)
  - `RemoteFilesObserver(..., env, data_dir: Path | None = None)`: at every (re)connect it reads ledger `data_dir/ledgers/<skip_ledger>.json` and sends its keys as `skip`

- [ ] **Step 1: Write the failing tests**

(a) `tests/remote_watcher_selftest.py`, append to `WatcherTests` (before `if __name__ == "__main__":`):

```python
    def test_skipped_files_are_not_reported(self):
        done = self.write("done.gz")
        self.write("new.gz")
        key = "%s|%d|%r" % (os.path.abspath(done), os.stat(done).st_size, os.stat(done).st_mtime)
        running = self.start(skip=[key], heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["new.gz"])
```

(b) `tests/test_config_observe.py`, append:

```python
def test_remote_skip_ledger(make_watch):
    config = load(make_watch, REMOTE + "skip_ledger = 'pulled'\n")
    assert config.observers["r"].skip_ledger == "pulled"


def test_remote_skip_ledger_must_be_a_ledger_name(make_watch):
    [problem] = problems(make_watch, REMOTE + "skip_ledger = '../x'\n")
    assert "'skip_ledger' must be a ledger name" in problem
```

(c) `tests/test_remote_files_observer.py`: add `from keepwatch.ctx import Ledger` to the imports and append:

```python
def test_skip_ledger_files_are_not_reported(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    (remote_dir / "b.tar.gz").write_bytes(b"more")
    age(remote_dir / "b.tar.gz")
    done = remote_dir / "a.tar.gz"
    info = done.stat()
    key = f"{os.path.join(os.path.abspath(remote_dir), 'a.tar.gz')}|{info.st_size}|{info.st_mtime!r}"
    Ledger(xdg.watch_data_dir("w") / "ledgers" / "pulled.json").add(key)
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, "skip_ledger = 'pulled'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.5)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["b.tar.gz"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_remote_watcher.py tests/test_config_observe.py tests/test_remote_files_observer.py -q`
Expected: FAIL — `unknown option(s): skip`, `unknown key 'skip_ledger'`.

- [ ] **Step 3: Implement the watcher** — in `src/keepwatch/remote_watcher.py`:

(a) `DEFAULTS`: add `"skip": [],` after `"rescan": 30.0,`.

(b) `Watcher.__init__`: add after `self.noted = set()`:

```python
        self.skip = set(options["skip"])  # "path|size|mtime" keys the caller has already handled
```

(c) `Watcher.scan`: replace

```python
            if (path,) + key in self.reported:
                continue
            failed_at = self.unreadable.get((path,) + key)
```

with

```python
            if (path,) + key in self.reported:
                continue
            if self.skip and "%s|%d|%r" % (decode(path), info.st_size, info.st_mtime) in self.skip:
                self.reported.add((path,) + key)
                continue
            failed_at = self.unreadable.get((path,) + key)
```

- [ ] **Step 4: Implement keepwatch's side**

(a) `src/keepwatch/config.py`: in `ObserverConfig` add `skip_ledger: str | None = None` after `ssh_command`. In `OBSERVER_KEYS`, add after the `ssh_command` key:

```python
    Key(
        "skip_ledger",
        "str",
        None,
        "kind = remote_files: a ledger of this watch (ctx.ledger(name)) whose keys \"path|size|mtime\" are files "
        "already handled; they are neither hashed nor reported again, which keeps reconnects cheap.",
    ),
```

add `"skip_ledger",` to the end of `_OBSERVER_KIND_KEYS["remote_files"]`, and in `_observer`, right after the `if key_name == "port" and not 1 <= converted <= 65535:` block (before `values[key_name] = converted`), add:

```python
        if key_name == "skip_ledger" and not _LEDGER.fullmatch(converted):
            collector.add(
                f"'skip_ledger' must be a ledger name (letters, digits, '_', '.', '-'), got {converted!r}",
                key="skip_ledger",
                table=table,
                topic="observers",
            )
            continue
```

with, next to `_OBSERVER_NAME = …`:

```python
_LEDGER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
```

(b) `src/keepwatch/observers.py`: change `from keepwatch.config import Command, ObserverConfig, WatchConfig` to keep it, and add `from keepwatch.ctx import Ledger, LedgerCorrupt`. Replace `RemoteFilesObserver.__init__` with:

```python
    def __init__(
        self,
        watch: WatchConfig,
        config: ObserverConfig,
        *,
        deliver: Deliver,
        sink: Sink,
        env: Mapping[str, str],
        data_dir: Path | None = None,
    ) -> None:
        timeout = config.heartbeat_timeout if config.heartbeat_timeout is not None else 3 * config.heartbeat
        self._options = watcher_options(config)
        self._skip_ledger = (
            None if config.skip_ledger is None or data_dir is None else data_dir / "ledgers" / f"{config.skip_ledger}.json"
        )
        command_config = replace(
            config,
            command=Command(argv=tuple(remote_command(config))),
            stdin=watcher_source(self._options),
            heartbeat_timeout=timeout,
        )
        super().__init__(watch, command_config, deliver=deliver, sink=sink, env=env)

    def run_once(self) -> None:
        if self._skip_ledger is not None:
            # Read at every (re)connect: files handled since the last connection are skipped too.
            self.config = replace(self.config, stdin=watcher_source({**self._options, "skip": self._skipped()}))
        super().run_once()

    def _skipped(self) -> list[str]:
        assert self._skip_ledger is not None
        try:
            return list(Ledger(self._skip_ledger, writable=False))
        except LedgerCorrupt as exc:
            self._output.line(str(exc))
            return []
```

and in `build_observer`, replace

```python
        cls = CommandObserver if config.kind == "command" else RemoteFilesObserver
        return cls(watch, config, deliver=deliver, sink=sink, env=env)
```

with

```python
        if config.kind == "command":
            return CommandObserver(watch, config, deliver=deliver, sink=sink, env=env)
        return RemoteFilesObserver(watch, config, deliver=deliver, sink=sink, env=env, data_dir=data_dir)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_remote_watcher.py tests/test_config_observe.py tests/test_remote_files_observer.py tests/test_docs.py -q` and `uv run python tests/remote_watcher_selftest.py`
Expected: all pass.

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py src/keepwatch/config.py src/keepwatch/observers.py tests/test_config_observe.py tests/test_remote_files_observer.py
git commit -m "remote_files skip_ledger: reconnects skip files already handled"
```

---

### Task 3: `recipe = "…"`: configuration

**Files:**
- Modify: `src/keepwatch/config.py`
- Test: `tests/test_config_recipe.py` (new)

**Interfaces:**
- Consumes: `transfer.MARKERS`, `transfer.PROTOCOLS`, `transfer.parse_endpoint` (Plan 6c), `ObserverConfig.skip_ledger` (Task 2).
- Produces:
  - watch key `recipe` (str, default none); `WatchConfig.recipe: str | None = None`
  - `RECIPES = ("pull", "push")`, `RECIPE_SETTINGS: dict[str, tuple[Key, ...]]`, `RECIPE_AFTER = ("delete", "archive", "keep")`
  - for a recipe watch: `settings` holds every recipe setting (defaults filled; durations in seconds; lists; path settings absolute strings), and `observers` holds the recipe's observers (`pull`: `"remote"`, kind `remote_files`, `skip_ledger = "pulled"`; `push`: `"local"`, kind `files`)
  - errors: unknown recipe, missing/unknown/invalid settings, `[hooks]`, `[observe.*]` or `watch.py` present, `push` `dest` not remote, `after = "archive"` without `archive_dir`

- [ ] **Step 1: Write the failing tests** — `tests/test_config_recipe.py`:

```python
from pathlib import Path

import pytest

from keepwatch.config import ConfigError, load_watch_config

PULL = """
recipe = "pull"
[settings]
remote = "me@linux1"
remote_dir = "/data/out"
local_dir = "staging"
pattern = "*.tar.gz"
"""
PUSH = """
recipe = "push"
[settings]
local_dir = "staging"
dest = "me@linux2:incoming/"
password_env = "RELAY_PASSWORD"
"""


def load(make_watch, text, files=None):
    return load_watch_config(make_watch("w", config=text, files=files))


def problems(make_watch, text, files=None):
    with pytest.raises(ConfigError) as info:
        load(make_watch, text, files)
    return [str(problem) for problem in info.value.problems]


def test_pull_recipe_settings_and_observer(make_watch):
    config = load(make_watch, PULL)
    assert config.recipe == "pull"
    settings = config.settings
    assert settings["local_dir"] == str(config.watch_dir / "staging")
    assert (settings["pattern"], settings["settle"], settings["checksum"], settings["remote_python"]) == (
        "*.tar.gz",
        10.0,
        True,
        "auto",
    )
    assert settings["ignore"] == [".*", "*.tmp", "*.part", "*~"] and settings["port"] is None
    [observer] = config.observers.values()
    assert (observer.name, observer.kind, observer.remote, observer.dir) == ("remote", "remote_files", "me@linux1", "/data/out")
    assert (observer.pattern, observer.skip_ledger, observer.checksum) == ("*.tar.gz", "pulled", True)


def test_pull_known_hosts_and_identity_reach_the_observer(make_watch):
    config = load(make_watch, PULL + "known_hosts = 'kh'\nidentity = '~/.ssh/id_relay'\nport = 2222\n")
    observer = config.observers["remote"]
    assert observer.identity == Path.home() / ".ssh" / "id_relay" and observer.port == 2222
    assert observer.ssh_options == ("-o", f'UserKnownHostsFile="{(config.watch_dir / "kh").as_posix()}"')


def test_push_recipe_settings_and_observer(make_watch):
    config = load(make_watch, PUSH)
    settings = config.settings
    assert (settings["dest"], settings["password_env"], settings["protocol"]) == ("me@linux2:incoming/", "RELAY_PASSWORD", "scp")
    assert (settings["marker"], settings["after"], settings["keep_for"], settings["reachable_timeout"]) == ("sha256", "delete", 7 * 86400.0, 5.0)
    assert (settings["reachable_host"], settings["reachable_port"]) == (None, None)
    [observer] = config.observers.values()
    assert (observer.name, observer.kind, observer.path) == ("local", "files", config.watch_dir / "staging")


def test_unknown_recipe(make_watch):
    [problem] = problems(make_watch, 'recipe = "relay"\n')
    assert "'recipe' must be one of pull, push, got 'relay'" in problem


def test_missing_and_unknown_settings(make_watch):
    found = problems(make_watch, 'recipe = "pull"\n[settings]\nremote = "h"\nlocaldir = "x"\n')
    assert any("unknown key 'localdir' in [settings] (did you mean 'local_dir'?)" in p for p in found)
    assert any("needs 'remote_dir' in [settings]" in p for p in found)
    assert any("needs 'local_dir' in [settings]" in p for p in found)


def test_invalid_setting_values(make_watch):
    found = problems(make_watch, PUSH + 'after = "shred"\nmarker = "md5"\nprotocol = "ftp"\nport = 70000\n')
    assert len(found) == 4


def test_push_dest_must_be_remote(make_watch):
    [problem] = problems(make_watch, 'recipe = "push"\n[settings]\nlocal_dir = "s"\ndest = "/tmp/out"\n')
    assert "'dest' must be a remote directory" in problem


def test_archive_needs_archive_dir(make_watch):
    [problem] = problems(make_watch, PUSH + 'after = "archive"\n')
    assert "after = \"archive\" needs 'archive_dir'" in problem


def test_recipe_watches_have_no_hooks_of_their_own(make_watch):
    found = problems(make_watch, PULL + "[hooks]\ncheck = ['true']\n[observe.x]\nkind = 'files'\npath = 'in'\n",
                     files={"watch.py": "def check(ctx):\n    return True\n"})
    assert any("must not have [hooks]" in p for p in found)
    assert any("must not have [observe.*] tables" in p for p in found)
    assert any("must not have watch.py" in p for p in found)


def test_watches_without_a_recipe_are_unchanged(make_watch):
    config = load(make_watch, "[settings]\nanything = 1\n")
    assert config.recipe is None and config.settings == {"anything": 1}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_recipe.py -q`
Expected: FAIL — `unknown key 'recipe'`.

- [ ] **Step 3: Implement** — in `src/keepwatch/config.py`:

(a) Imports: change `from keepwatch.hooks import HOOK_NAMES` to `from keepwatch.hooks import HOOK_NAMES, WATCH_PY` and add `from keepwatch.transfer import MARKERS, PROTOCOLS, parse_endpoint`.

(b) `WATCH_KEYS`: add as the last entry:

```python
    Key(
        "recipe",
        "str",
        None,
        "Use a built-in recipe (`pull` or `push`) instead of hooks of your own; it is configured in [settings]. "
        "See `keepwatch docs recipes`.",
    ),
```

and add `recipe: str | None = None` to `WatchConfig` after `shell`.

(c) After `ObserverConfig` (and the observer constants), add:

```python
RECIPES = ("pull", "push")
RECIPE_AFTER = ("delete", "archive", "keep")
_SSH_SETTINGS = (
    Key("port", "int", None, "ssh port (default: ssh's own, normally 22)."),
    Key("identity", "str", None, "Private key file (ssh -i; only this key is offered). Relative to the watch directory."),
    Key("known_hosts", "str", None, "known_hosts file for host keys (default: ~/.ssh/known_hosts). Relative to the watch directory."),
    Key("ssh_options", "str_list", (), 'Extra ssh/scp arguments, e.g. ["-o", "ProxyJump=bastion"].'),
)
RECIPE_SETTINGS: dict[str, tuple[Key, ...]] = {
    "pull": (
        Key("remote", "str", None, "Required. The source host: `user@host` or a Host alias from ~/.ssh/config."),
        Key(
            "remote_dir",
            "str",
            None,
            "Required. The directory to watch on the source host (not recursive; `~` and relative paths start at "
            "the remote home).",
        ),
        Key(
            "local_dir",
            "str",
            None,
            "Required. Where pulled files go (the staging folder). Relative to the watch directory; `~` and "
            "environment variables are expanded.",
        ),
        Key("pattern", "str", "*", "Pull only file names matching this glob."),
        Key("ignore", "str_list", DEFAULT_IGNORE, "Never pull file names matching any of these globs."),
        Key("settle", "duration", 10.0, "A remote file must be unchanged this long before it is pulled."),
        Key("checksum", "bool", True, "Compute sha256 on the source host and verify every pull against it."),
        Key("remote_python", "str", "auto", "The source host's Python 3.6+ (see `keepwatch docs observers`)."),
        *_SSH_SETTINGS,
        Key("ssh_command", "argv", None, 'The ssh program for the observer. Default: ["ssh"].'),
    ),
    "push": (
        Key(
            "local_dir",
            "str",
            None,
            "Required. The folder whose files are pushed. Relative to the watch directory; `~` and environment "
            "variables are expanded.",
        ),
        Key("dest", "str", None, "Required. The destination directory: `user@host:dir` or `scp://user@host:port/dir`."),
        Key("pattern", "str", "*", "Push only file names matching this glob."),
        Key("ignore", "str_list", DEFAULT_IGNORE, "Never push file names matching any of these globs."),
        Key("settle", "duration", 10.0, "A file must be unchanged this long before it is pushed."),
        Key("protocol", "str", "scp", "`scp` (classic; scp-only servers accept it) or `sftp`."),
        Key("password_env", "str", None, "Name of the environment variable holding the destination password."),
        *_SSH_SETTINGS,
        Key("marker", "str", "sha256", "`sha256`: upload NAME.sha256 after each file as a completion marker; `none`."),
        Key("after", "str", "delete", "After a push: `delete` the local file, `archive` it, or `keep` it (a ledger remembers it)."),
        Key("archive_dir", "str", None, "With after = \"archive\": where pushed files go. Relative to the watch directory."),
        Key("keep_for", "duration", 7 * 86400.0, "With after = \"archive\": delete archived files after this long."),
        Key("reachable_host", "str", None, "Host to probe before pushing (default: the host in `dest`; set it when `dest` uses a Host alias)."),
        Key("reachable_port", "int", None, "Port to probe (default: `port`, else 22)."),
        Key("reachable_timeout", "duration", 5.0, "How long the probe waits. No answer means unknown: files wait, nothing fails."),
    ),
}
_RECIPE_REQUIRED = {"pull": ("remote", "remote_dir", "local_dir"), "push": ("local_dir", "dest")}
_RECIPE_PATHS = ("local_dir", "identity", "known_hosts", "archive_dir")
_RECIPE_CHOICES = {"protocol": PROTOCOLS, "marker": MARKERS, "after": RECIPE_AFTER}


def _recipe_settings(collector: _Collector, recipe: str, raw: Mapping[str, Any], watch_dir: Path) -> dict[str, Any]:
    """[settings] of a recipe watch: validated, with every default filled in (JSON-safe)."""
    keys = {key.name: key for key in RECIPE_SETTINGS[recipe]}
    values: dict[str, Any] = {name: key.default for name, key in keys.items()}
    for name, item in raw.items():
        if name not in keys:
            _unknown_key(collector, name, keys, table="settings")
            continue
        converted = _convert(collector, keys[name], item, table="settings")
        if converted is _INVALID:
            continue
        choices = _RECIPE_CHOICES.get(name)
        if choices is not None and converted not in choices:
            collector.add(f"'{name}' must be one of {', '.join(choices)}, got {converted!r}", key=name, table="settings", topic="recipes")
            continue
        if name in ("port", "reachable_port") and not 1 <= converted <= 65535:
            collector.add(f"'{name}' must be between 1 and 65535, got {converted}", key=name, table="settings", topic="recipes")
            continue
        values[name] = converted
    for name in _RECIPE_REQUIRED[recipe]:
        if values.get(name) in (None, ""):
            collector.add(f"recipe = \"{recipe}\" needs '{name}' in [settings]", table="settings", topic="recipes")
    if recipe == "push":
        if values["dest"] and not parse_endpoint(values["dest"]).remote:
            collector.add(
                f"'dest' must be a remote directory (user@host:dir or scp://user@host/dir), got {values['dest']!r}",
                key="dest",
                table="settings",
                topic="recipes",
            )
        if values["after"] == "archive" and not values["archive_dir"]:
            collector.add("after = \"archive\" needs 'archive_dir' in [settings]", key="after", table="settings", topic="recipes")
    for name in _RECIPE_PATHS:
        if isinstance(values.get(name), str) and values[name]:
            values[name] = str(_observed_path(values[name], watch_dir))
    return {name: list(value) if isinstance(value, tuple) else value for name, value in values.items()}


def _recipe_observers(recipe: str, settings: Mapping[str, Any]) -> dict[str, ObserverConfig]:
    if recipe == "push":
        return {
            "local": ObserverConfig(
                name="local",
                kind="files",
                path=Path(settings["local_dir"]),
                pattern=settings["pattern"],
                ignore=tuple(settings["ignore"]),
                settle=settings["settle"],
            )
        }
    ssh_options = list(settings["ssh_options"])
    if settings["known_hosts"]:
        ssh_options += ["-o", f'UserKnownHostsFile="{Path(settings["known_hosts"]).as_posix()}"']
    return {
        "remote": ObserverConfig(
            name="remote",
            kind="remote_files",
            remote=settings["remote"],
            dir=settings["remote_dir"],
            pattern=settings["pattern"],
            ignore=tuple(settings["ignore"]),
            settle=settings["settle"],
            checksum=settings["checksum"],
            remote_python=settings["remote_python"],
            port=settings["port"],
            identity=Path(settings["identity"]) if settings["identity"] else None,
            ssh_options=tuple(ssh_options),
            ssh_command=tuple(settings["ssh_command"]) if settings["ssh_command"] else None,
            skip_ledger="pulled",
        )
    }
```

(d) In `load_watch_config`, right before `if collector.problems:` (the one that raises), add:

```python
    recipe = values.get("recipe")
    if recipe is not None:
        if recipe not in RECIPES:
            collector.add(f"'recipe' must be one of {', '.join(RECIPES)}, got {recipe!r}", key="recipe", topic="recipes")
        else:
            provides = f"a watch with recipe = \"{recipe}\" must not have"
            if "hooks" in data:
                collector.add(f"{provides} [hooks]: the recipe provides its hooks", key="hooks", topic="recipes")
            if "observe" in data:
                collector.add(f"{provides} [observe.*] tables: the recipe provides its observers", topic="recipes")
            if (watch_dir / WATCH_PY).exists():
                collector.add(f"{provides} watch.py: the recipe provides its hooks (remove or rename {WATCH_PY})", topic="recipes")
            settings = _recipe_settings(collector, recipe, settings, watch_dir)
            if not collector.problems:
                observers = _recipe_observers(recipe, settings)
```

(`settings` and `observers` are the variables the function already passes to `WatchConfig`; the dataclass gets `recipe` from `**values`.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_recipe.py tests/test_config_watch.py tests/test_config_observe.py tests/test_docs.py -q`
Expected: all pass. (The `recipes` docs topic named in the messages comes in Task 6.)

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/config.py tests/test_config_recipe.py
git commit -m "recipe = \"pull\" | \"push\": settings schemas and observers"
```

---

### Task 4: Recipe hooks run; the `pull` recipe

**Files:**
- Create: `src/keepwatch/recipes/__init__.py`, `src/keepwatch/recipes/pull.py`
- Modify: `src/keepwatch/hooks.py` (`RECIPE_HOOKS`, `watch_hooks`), `src/keepwatch/pollengine.py`, `src/keepwatch/runner.py` (`_run_python`), `src/keepwatch/worker.py` (`run_request`), `src/keepwatch/validation.py` (`check_watch`)
- Test: `tests/test_recipe_pull.py` (new)

**Interfaces:**
- Produces:
  - `hooks.RECIPE_HOOKS = {"pull": frozenset({"check", "on_true"}), "push": frozenset({"check", "on_true"})}`; `hooks.watch_hooks(watch_dir: Path, command_hooks: Collection[str], recipe: str | None) -> tuple[frozenset[str], str | None]`
  - worker request key `recipe`; the worker imports `keepwatch.recipes.<recipe>`; `hook.end` `target` is `recipe <name>:<hook>`
  - `recipes.pull.LEDGER = "pulled"`, `recipes.pull.event_key(event) -> str` (`"<path>|<size>|<mtime!r>"`), `check`, `on_true`
  - `keepwatch validate` on a recipe watch checks that `ssh` and `scp` (pull) / `scp` (push) are on PATH, and skips the watch.py checks

- [ ] **Step 1: Write the failing tests** — `tests/test_recipe_pull.py`:

```python
import hashlib
import os
import shutil

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.config import load_global_config, load_watch_config
from keepwatch.ctx import Ledger
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import toml_path
from scp_server import ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def pull_watch(make_watch, server, tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return make_watch(
        "relay-pull",
        config=(
            'recipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\nremote_dir = "/"\n'
            f"local_dir = {toml_path(tmp_path / 'stage')}\nport = {server.port}\n"
            f"identity = {toml_path(server.client_key)}\nknown_hosts = {toml_path(server.known_hosts)}\n"
            f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\n"
        ),
    )


def file_event(name, data, sha=None):
    return {
        "event": "file",
        "path": f"/{name}",
        "name": name,
        "size": len(data),
        "mtime": 1790000000.5,
        "sha256": sha or hashlib.sha256(data).hexdigest(),
        "remote": "u@127.0.0.1",
        "observer": "remote",
        "received": "2026-10-02T00:00:00.000+00:00",
    }


def engine(xdg, records):
    global_config = load_global_config(xdg.config_file, xdg)
    return PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())


def test_pull_recipe_pulls_new_files_once(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    events = [file_event("a.tar.gz", b"data")]
    first = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert first.outcome is Outcome.TRUE and not first.failed, records
    assert (tmp_path / "stage" / "a.tar.gz").read_bytes() == b"data"
    assert "/a.tar.gz|4|1790000000.5" in Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")
    targets = [r["target"] for r in records if r["event"] == "hook.end"]
    assert targets == ["recipe pull:check", "recipe pull:on_true"]
    second = engine(xdg, []).poll(watch, first.after, events=events)
    assert second.outcome is Outcome.FALSE


def test_pull_recipe_does_not_record_a_failed_pull(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"data", sha="0" * 64)])
    assert report.failed
    assert not list((tmp_path / "stage").iterdir())
    assert len(Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")) == 0


def test_pull_recipe_ignores_other_events(make_watch, server, tmp_path, xdg):
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[{"line": "noise", "observer": "remote"}])
    assert report.outcome is Outcome.FALSE


def test_validate_a_recipe_watch(make_watch, server, tmp_path, xdg):
    pull_watch(make_watch, server, tmp_path)
    result = CliRunner().invoke(cli, ["validate", "relay-pull"])
    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_recipe_pull.py -q`
Expected: FAIL — the check reports "the watch has no check".

- [ ] **Step 3: Implement**

(a) `src/keepwatch/hooks.py`, append:

```python
RECIPE_HOOKS = {"pull": frozenset({CHECK, "on_true"}), "push": frozenset({CHECK, "on_true"})}


def watch_hooks(watch_dir: Path, command_hooks: Collection[str], recipe: str | None) -> tuple[frozenset[str], str | None]:
    """The hooks a watch has: its recipe's, or those of watch.py and [hooks] (with a problem message if broken)."""
    if recipe is not None:
        return RECIPE_HOOKS[recipe], None
    return resolve_hooks(watch_dir, command_hooks)
```

(b) `src/keepwatch/pollengine.py`: import `watch_hooks` instead of `resolve_hooks` (`from keepwatch.hooks import CHECK, NO_CHECK, watch_hooks`) and replace `hooks, problem = resolve_hooks(watch.watch_dir, watch.hooks)` with `hooks, problem = watch_hooks(watch.watch_dir, watch.hooks, watch.recipe)`.

(c) `src/keepwatch/runner.py`, `_run_python`: replace

```python
        target = f"watch.py:{call.hook}" if call.mode == "call" else "watch.py (describe)"
```

with

```python
        if call.watch.recipe is not None:
            target = f"recipe {call.watch.recipe}:{call.hook}"
        else:
            target = f"watch.py:{call.hook}" if call.mode == "call" else "watch.py (describe)"
```

and add `"recipe": call.watch.recipe,` to the `request` dict (after `"mode": call.mode,`).

(d) `src/keepwatch/worker.py`, `run_request`: add `import importlib` next to `import importlib.util` (keep both), and replace

```python
    try:
        module = _load_module(watch_dir)
    except BaseException as exc:
        return {
            "status": "error",
            "reason": f"importing watch.py failed: {type(exc).__name__}: {exc}",
            "exception": _exception_info(exc),
        }
```

with

```python
    recipe = request.get("recipe")
    try:
        module = importlib.import_module(f"keepwatch.recipes.{recipe}") if recipe else _load_module(watch_dir)
    except BaseException as exc:
        source = f"recipe {recipe}" if recipe else WATCH_PY
        return {
            "status": "error",
            "reason": f"importing {source} failed: {type(exc).__name__}: {exc}",
            "exception": _exception_info(exc),
        }
```

and change `return {"status": "error", "reason": f"watch.py has no function {hook}()"}` to

```python
        return {"status": "error", "reason": f"{f'recipe {recipe}' if recipe else WATCH_PY} has no function {hook}()"}
```

(e) `src/keepwatch/validation.py`, `check_watch`: right after the `except ConfigError` block (after `return report`), add:

```python
    if watch.recipe is not None:
        search = {**os.environ, **global_config.environment, **watch.environment}.get("PATH", "")
        for program in ("ssh", "scp") if watch.recipe == "pull" else ("scp",):
            if not shutil.which(program, path=search):
                report.problems.append(
                    f"{watch.config_file}: recipe = \"{watch.recipe}\" needs OpenSSH '{program}' on PATH; "
                    "see: keepwatch docs transfers"
                )
        return report
```

(f) Create `src/keepwatch/recipes/__init__.py`:

```python
"""Built-in recipes: hooks keepwatch provides for `recipe = "<name>"` watches (see `keepwatch docs recipes`).

Each module defines check(ctx) and on_true(ctx) and runs in the Python worker, so it uses only the
standard library and keepwatch's stdlib-only modules. Its settings are validated by keepwatch.config.
"""
```

Create `src/keepwatch/recipes/pull.py`:

```python
"""recipe = "pull": copy settled files from a remote directory into a local staging folder.

The `remote_files` observer reports settled remote files (with their sha256). check() answers TRUE with
the files not yet in the ledger "pulled"; on_true() pulls each one (verified) and records it. The same
ledger tells the observer which files to skip when it reconnects.
"""

from __future__ import annotations

from typing import Any

from keepwatch.ctx import Ctx

LEDGER = "pulled"


def event_key(event: dict[str, Any]) -> str:
    """The ledger key of a remote file: "<path>|<size>|<mtime!r>" (the remote watcher builds the same text)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    options = {name: settings[name] for name in ("port", "identity", "known_hosts") if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def check(ctx: Ctx) -> tuple[bool, list[dict[str, Any]]]:
    pulled = ctx.ledger(LEDGER)
    new: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in ctx.events:
        if event.get("event") != "file":
            continue
        key = event_key(event)
        if key in pulled or key in seen:
            continue
        seen.add(key)
        new.append(
            {"path": event["path"], "name": event["name"], "size": event["size"], "sha256": event.get("sha256"), "key": key}
        )
    return bool(new), new


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    options = transfer_options(settings)
    for item in ctx.payload:
        if item["key"] in pulled:
            continue
        sha256 = item["sha256"] if settings["checksum"] else None
        final = ctx.transfer.pull(
            f"{settings['remote']}:{item['path']}", settings["local_dir"], size=item["size"], sha256=sha256, **options
        )
        pulled.add(item["key"])
        ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
```

(`pull`'s default `on_conflict="skip-identical"` makes a repeated pull of a file still in staging a no-op when its sha256 matches.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_pull.py tests/test_pollengine.py tests/test_runner_python.py tests/test_cli_validate.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the eight files. Then:

```bash
git add src/keepwatch/recipes/__init__.py src/keepwatch/recipes/pull.py src/keepwatch/hooks.py src/keepwatch/pollengine.py src/keepwatch/runner.py src/keepwatch/worker.py src/keepwatch/validation.py tests/test_recipe_pull.py
git commit -m "Recipes run in the worker; the pull recipe"
```

---

### Task 5: The `push` recipe

**Files:**
- Create: `src/keepwatch/recipes/push.py`
- Test: `tests/test_recipe_push.py` (new)

**Interfaces:**
- Produces: `recipes.push.LEDGER = "pushed"`, `reachable(settings) -> tuple[str, int]`, `settled_files(settings) -> list[Path]`, `check`, `on_true`.

- [ ] **Step 1: Write the failing tests** — `tests/test_recipe_push.py`:

```python
import hashlib
import os
import shutil
import socket
import time

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import toml_path
from scp_server import ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def staged(tmp_path, name="c.tar.gz", data=b"payload"):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    path = stage / name
    path.write_bytes(data)
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def push_watch(make_watch, server, tmp_path, extra="", settle="1s"):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return load_watch_config(
        make_watch(
            "relay-push",
            config=(
                f'recipe = "push"\n[settings]\nlocal_dir = {toml_path(tmp_path / "stage")}\ndest = "u@127.0.0.1:"\n'
                f"port = {server.port}\nidentity = {toml_path(server.client_key)}\n"
                f"known_hosts = {toml_path(server.known_hosts)}\n"
                f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\nsettle = '{settle}'\n{extra}"
            ),
        )
    )


def poll(xdg, watch, state=None):
    global_config = load_global_config(xdg.config_file, xdg)
    engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=lambda record: None, pid=os.getpid())
    return engine.poll(watch, state or WatchState(False))


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_push_recipe_uploads_with_a_marker_then_deletes(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    report = poll(xdg, push_watch(make_watch, server, tmp_path))
    assert report.outcome is Outcome.TRUE and not report.failed
    assert (server.root / "c.tar.gz").read_bytes() == b"payload"
    digest = hashlib.sha256(b"payload").hexdigest()
    assert (server.root / "c.tar.gz.sha256").read_text(encoding="utf-8") == f"{digest}  c.tar.gz\n"
    assert not path.exists()


def test_unreachable_destination_is_unknown(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    watch = push_watch(make_watch, server, tmp_path, f"reachable_port = {closed_port()}\nreachable_timeout = '2s'\n")
    report = poll(xdg, watch)
    assert report.outcome is Outcome.UNKNOWN and not report.failed
    assert "not reachable" in report.reason
    assert path.exists() and not (server.root / "c.tar.gz").exists()


def test_unsettled_files_wait(make_watch, server, tmp_path, xdg):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "fresh.tar.gz").write_bytes(b"x")
    (stage / ".partial.part").write_bytes(b"x")
    report = poll(xdg, push_watch(make_watch, server, tmp_path, settle="1h"))
    assert report.outcome is Outcome.FALSE


def test_archive_moves_and_prunes(make_watch, server, tmp_path, xdg):
    archive = tmp_path / "archive"
    archive.mkdir()
    ancient = archive / "ancient.tar.gz"
    ancient.write_bytes(b"old")
    os.utime(ancient, (1, 1))
    path = staged(tmp_path)
    extra = f'after = "archive"\narchive_dir = {toml_path(archive)}\nkeep_for = "1d"\n'
    report = poll(xdg, push_watch(make_watch, server, tmp_path, extra))
    assert not report.failed
    assert not path.exists() and (archive / "c.tar.gz").read_bytes() == b"payload"
    assert not ancient.exists()


def test_keep_remembers_what_was_pushed(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    watch = push_watch(make_watch, server, tmp_path, 'after = "keep"\nmarker = "none"\n')
    first = poll(xdg, watch)
    assert first.outcome is Outcome.TRUE and path.exists()
    assert sorted(p.name for p in server.root.iterdir()) == ["c.tar.gz"]
    assert poll(xdg, watch, first.after).outcome is Outcome.FALSE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_recipe_push.py -q`
Expected: FAIL — `importing recipe push failed: ModuleNotFoundError`.

- [ ] **Step 3: Implement** — create `src/keepwatch/recipes/push.py`:

```python
"""recipe = "push": upload settled files from a local folder when the destination is reachable.

check() probes the destination first: no answer means unknown (no failure, no backoff; files wait). Then
it answers TRUE with the settled files (rescanned here; the files observer only wakes the watch). on_true()
pushes each one with its .sha256 marker, then deletes, archives or keeps it (a ledger remembers kept files).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from pathlib import Path
from typing import Any

from keepwatch.ctx import Ctx, Unknown
from keepwatch.transfer import parse_endpoint

LEDGER = "pushed"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    names = ("protocol", "password_env", "port", "identity", "known_hosts")
    options = {name: settings[name] for name in names if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def reachable(settings: dict[str, Any]) -> tuple[str, int]:
    """The host and port to probe: reachable_host/port, else dest's host and port (else `port`, else 22)."""
    dest = parse_endpoint(settings["dest"])
    host = settings["reachable_host"] or dest.host
    port = settings["reachable_port"] or settings["port"] or dest.port or 22
    return str(host), int(port)


def settled_files(settings: dict[str, Any]) -> list[Path]:
    """Regular files in local_dir matching pattern, not ignore, last modified at least `settle` ago."""
    directory = Path(settings["local_dir"])
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    now = time.time()
    found = []
    for path in entries:
        name = path.name
        if not fnmatch.fnmatch(name, settings["pattern"]):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in settings["ignore"]):
            continue
        try:
            if path.is_file() and now - path.stat().st_mtime >= settings["settle"]:
                found.append(path)
        except OSError:
            continue
    return found


def check(ctx: Ctx) -> tuple[bool, list[str]]:
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; files wait in {settings['local_dir']}")
    pushed = ctx.ledger(LEDGER) if settings["after"] == "keep" else None
    files = [str(path) for path in settled_files(settings) if pushed is None or ctx.file_key(path) not in pushed]
    return bool(files), files


def _archive(path: Path, directory: Path, keep_for: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    shutil.move(str(path), str(target))
    os.utime(target)  # the archive's own clock starts now
    cutoff = time.time() - keep_for
    for old in directory.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    options = transfer_options(settings)
    pushed = ctx.ledger(LEDGER)
    for name in ctx.payload:
        path = Path(name)
        if not path.is_file():
            continue  # moved or deleted since the check
        key = ctx.file_key(path)
        if settings["after"] == "keep" and key in pushed:
            continue
        remote = ctx.transfer.push(path, settings["dest"], marker=settings["marker"], **options)
        ctx.log.info("pushed %s", path.name, extra={"remote": remote})
        if settings["after"] == "delete":
            path.unlink()
        elif settings["after"] == "archive":
            _archive(path, Path(settings["archive_dir"]), settings["keep_for"])
        else:
            pushed.add(key)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_recipe_push.py tests/test_recipe_pull.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/recipes/push.py tests/test_recipe_push.py
git commit -m "The push recipe: reachability probe, markers, delete/archive/keep"
```

---

### Task 6: Documentation

**Files:**
- Create: `src/keepwatch/reference/recipes.md`
- Modify: `src/keepwatch/reference.py` (`TOPICS`, `recipes_topic`, `GENERATED`), `src/keepwatch/reference/transfers.md`, `src/keepwatch/reference/agent.md`
- Modify: `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 5)
- Test: `tests/test_docs.py`

- [ ] **Step 1: Write the failing tests** — in `tests/test_docs.py`, add `"recipes": ["recipe = \"pull\"", "recipe = \"push\"", "pulled", "pushed", "skip_ledger", "unknown", "reachable_host", "staging", "at-least-once"],` to `KEY_FACTS`, append `"must print nothing"` to `KEY_FACTS["transfers"]`, and add:

```python
def test_every_recipe_setting_is_documented():
    from keepwatch.config import RECIPE_SETTINGS

    text = render_topic("recipes")
    for recipe, keys in RECIPE_SETTINGS.items():
        assert f"## `{recipe}` settings" in text
        for key in keys:
            assert key.doc.strip() and f"`{key.name}`" in text, (recipe, key.name)
```

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — no topic `recipes`.

- [ ] **Step 2: Write the docs**

(a) `src/keepwatch/reference.py`: in `TOPICS`, insert after `("transfers", ...)`:

```python
    ("recipes", "Built-in watches configured in config.toml: pull and push (every setting)"),
```

and add, before the `_LANGUAGES = …` line:

```python
def recipes_topic() -> str:
    from keepwatch.config import RECIPE_SETTINGS

    lines = [narrative("recipes")]
    for recipe, keys in RECIPE_SETTINGS.items():
        lines += ["", f"## `{recipe}` settings", ""]
        lines += _key_table(keys, defaultable_column=False)
    return "\n".join(lines)


GENERATED["recipes"] = recipes_topic
```

(b) Create `src/keepwatch/reference/recipes.md`:

````markdown
# Recipes: built-in watches

Some watches are the same everywhere. A **recipe** is one keepwatch provides: set `recipe = "<name>"` in config.toml and configure it in `[settings]`. A recipe watch has no watch.py, no `[hooks]` and no `[observe.*]` tables (the recipe brings its own); everything else (`interval`, timeouts, `max_failures`, `retry_after`, `[environment]`) works as for any watch. `keepwatch validate`, `poll`, `observe`, `status` and `logs` work too; hook records show `recipe pull:check` and so on.

## pull: a remote directory → a local staging folder

```toml
description = "Pull finished exports from linux1"
recipe = "pull"
action_timeout = "2h"      # big files over a slow link

[settings]
remote = "me@linux1"
remote_dir = "/data/out"
local_dir = 'C:/relay/staging'
pattern = "*.tar.gz"
settle = "30s"
```

- A `remote_files` observer (named `remote`) keeps one ssh connection to `remote` and reports each settled file with its sha256 (see `keepwatch docs observers` for what the source host needs: Python 3.6+, key authentication, a known host key).
- **check** answers TRUE with the reported files not yet in the ledger `pulled`.
- **on_true** pulls each one into `local_dir` through `.NAME.part`, verifies size and sha256, renames it, and adds it to `pulled`. A failure stops the action (the files already done stay recorded) and the poll fails: the watch backs off and retries, and the events stay queued (at-least-once).
- The same ledger is the observer's `skip_ledger`: after a reconnect, files already pulled are neither hashed nor reported again. Files stay on the source host; pulled files are never pulled again while their size and modification time stay the same.

## push: a local folder → a remote directory, when it is reachable

```toml
description = "Push staged files to linux2 when it is connected"
recipe = "push"
action_timeout = "2h"

[settings]
local_dir = 'C:/relay/staging'
dest = "me@linux2:incoming/"
password_env = "RELAY_PASSWORD"
pattern = "*.tar.gz"
```

- A `files` observer (named `local`) on `local_dir` wakes the watch when a file settles.
- **check** first probes the destination (`reachable_host`, else the host in `dest`; `reachable_port`, else `port`, else 22). **No answer means unknown**: no failure, no backoff, no going offline; the files simply wait in `local_dir`, which is the queue. Set `reachable_host` when `dest` uses a Host alias from `~/.ssh/config` (an alias is not a DNS name). Otherwise check answers TRUE with the settled files in `local_dir`.
- **on_true** pushes each file, then its `NAME.sha256` marker (`marker = "none"` to skip), then deletes it (`after = "delete"`, the default), moves it to `archive_dir` (`"archive"`, pruning files older than `keep_for`) or keeps it (`"keep"`; the ledger `pushed` remembers it).

The two chain naturally through a staging folder: pull's `local_dir` is push's `local_dir`. See `keepwatch docs relay` for the whole setup.
````

(c) `src/keepwatch/reference/transfers.md`: in the "Errors" section, add as the first bullet:

```markdown
- **Remote login scripts must print nothing** for non-interactive sessions: an `echo` in the remote `.cshrc`/`.bashrc` corrupts scp in both protocols ("Received message too long"). Guard it: `if ($?prompt) then … endif` in csh/tcsh, `case $- in *i*) … ;; esac` in sh/bash. (The remote watcher of `remote_files` tolerates such output; scp cannot.) File names with spaces or shell characters are fine: keepwatch backslash-escapes remote paths.
```

(d) `src/keepwatch/reference/agent.md`, "Common mistakes": add at the end:

```markdown
- Writing a watch.py to move files between machines. Try a recipe first (`recipe = "pull"` or `"push"`, `keepwatch docs recipes`); it already handles settling, verification, markers, reachability and retries.
```

(e) Spec, section 5.1: replace `` `remote_python` (`auto`), `port`, `identity`, `password_env`, and a destination: `local_dir` (staging) `` and the following line(s) through `so it does not suit an scp-only destination such as linux2).` with:

```
`remote_python` (`auto`), `port`, `identity`, `known_hosts`, `ssh_options`, `ssh_command`, and `local_dir`
(staging). No `password_env` (the observer needs key authentication anyway) and no `dest` (direct `scp -3`
needs SFTP and keys on both hosts; a watch.py with `ctx.transfer.copy` can do it). The observer gets
`skip_ledger = "pulled"`, so reconnects do not re-hash files already pulled.
```

and in 5.2 add `reachable_host` after `` `reachable_port` (22) `` with the note `` (`reachable_host` defaults to the host in `dest`; set it when `dest` is a Host alias) ``.

- [ ] **Step 3: Run the docs tests and read the topic**

Run: `uv run pytest tests/test_docs.py -q`, then `uv run keepwatch docs recipes`.
Expected: tests pass; the topic shows both settings tables.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/reference.py src/keepwatch/reference/recipes.md src/keepwatch/reference/transfers.md src/keepwatch/reference/agent.md tests/test_docs.py docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md
git commit -m "Document recipes"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push. The relay examples, the `relay` docs topic, the end-to-end relay test and the install guards are Plan 6e.
