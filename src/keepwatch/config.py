"""Configuration files: schema, parsing and validation with line numbers."""

from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from keepwatch import platform
from keepwatch.ctx import LEDGER_NAME
from keepwatch.durations import DurationError, format_duration, parse_duration
from keepwatch.hooks import HOOK_NAMES, WATCH_PY
from keepwatch.paths import Paths
from keepwatch.transfer import CONFLICTS, MARKERS, PROTOCOLS, known_hosts_option, parse_endpoint

CONFIG_NAME = "config.toml"


@dataclass(frozen=True)
class ConfigProblem:
    path: Path
    line: int | None
    message: str
    topic: str = "config"

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{where}: {self.message}; see: keepwatch docs {self.topic}"


class ConfigError(Exception):
    """One or more problems in a configuration file."""

    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = problems
        super().__init__("\n".join(str(problem) for problem in problems))


@dataclass(frozen=True)
class Command:
    """A command hook: argv runs directly; shell runs through the platform shell (or the watch's `shell`)."""

    argv: tuple[str, ...] | None = None
    shell: str | None = None

    def to_argv(self, shell: Sequence[str] | None = None) -> list[str]:
        if self.shell is not None:
            return platform.shell_argv(self.shell, shell)
        return list(self.argv or ())

    def display(self) -> str:
        return self.shell if self.shell is not None else shlex.join(self.argv or ())


@dataclass(frozen=True)
class ExitCodes:
    true: tuple[int, ...] = (0,)
    false: tuple[int, ...] = (1,)
    unknown: tuple[int, ...] = ()


@dataclass(frozen=True)
class Key:
    """One scalar config key. `doc` is the source for `keepwatch docs config`."""

    name: str
    kind: str
    default: Any
    doc: str
    defaultable: bool = False


WATCH_KEYS = (
    Key("description", "str", "", "One line shown in `status` and logs."),
    Key("enabled", "bool", True, "`false` parks the watch: loaded and validated, never polled."),
    Key(
        "interval",
        "interval",
        60.0,
        "Wait between the end of one poll and the start of the next. Minimum 1s.",
        True,
    ),
    Key(
        "initial_condition",
        "bool",
        False,
        "The condition before the first answer after the service starts.",
        True,
    ),
    Key("check_timeout", "interval", 60.0, "Deadline for the check.", True),
    Key("action_timeout", "interval", 60.0, "Deadline for each action.", True),
    Key(
        "max_failures",
        "int",
        5,
        "Consecutive failed polls before the watch goes offline. 0 disables the limit.",
        True,
    ),
    Key("retry_after", "interval", None, "While offline, make one trial poll this often.", True),
    Key("python_dependencies", "str_list", (), "PEP 508 requirements for watch.py, installed by uv."),
    Key(
        "shell",
        "argv",
        None,
        "Program and leading arguments that run string hooks, e.g. [\"pwsh\", \"-NoProfile\", \"-Command\"]. "
        "Default: /bin/sh -c on POSIX, Windows PowerShell on Windows.",
        True,
    ),
    Key(
        "recipe",
        "str",
        None,
        "Use a built-in recipe (`pull` or `push`) instead of hooks of your own; it is configured in [settings]. "
        "See `keepwatch docs recipes`.",
    ),
)

WATCH_TABLES = {
    "hooks": "Hooks implemented as commands: hook name = string (run via /bin/sh -c) or list (run directly).",
    "check_exit_codes": "Exit-code mapping for a command check: true, false, unknown (lists of 0-255).",
    "environment": "Extra environment variables for this watch's hooks.",
    "settings": "Free-form plugin parameters: ctx.settings in Python, KEEPWATCH_SETTING_* for commands.",
    "observe": "Event sources the service keeps running for this watch, one [observe.<name>] table each; "
    "see `keepwatch docs observers`.",
}

OBSERVER_KINDS = ("command", "files", "remote_files")
DEFAULT_IGNORE = (".*", "*.tmp", "*.part", "*~")

OBSERVER_KEYS = (
    Key(
        "kind",
        "str",
        None,
        "Required. `command`: a long-running program; each line it prints is an event. "
        "`files`: a local directory; each settled file is an event. "
        "`remote_files`: a directory on another host, watched over one ssh connection.",
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
        "kind = command or remote_files: restart when no line (heartbeats included) arrives for this long. "
        "Default: none for command, 3 × `heartbeat` for remote_files.",
    ),
    Key(
        "path",
        "str",
        None,
        "kind = files, required: the directory to observe. Relative to the watch directory; `~` and environment "
        "variables are expanded.",
    ),
    Key("pattern", "str", "*", "kind = files or remote_files: report only file names matching this glob."),
    Key(
        "ignore",
        "str_list",
        DEFAULT_IGNORE,
        "kind = files or remote_files: never report file names matching any of these globs.",
    ),
    Key("recursive", "bool", False, "kind = files: also observe subdirectories."),
    Key(
        "settle",
        "duration",
        10.0,
        "kind = files or remote_files: report a file once its size and modification time have not changed for "
        "this long and it was last modified at least this long ago.",
    ),
    Key(
        "marker",
        "str",
        "none",
        "kind = files: `sha256` reports a file only once NAME.sha256 beside it (sha256sum format) matches it; the "
        "markers themselves are never reported. `none`: no markers.",
    ),
    Key("remote", "str", None, "kind = remote_files, required: `user@host`, or a Host alias from ~/.ssh/config."),
    Key(
        "dir",
        "str",
        None,
        "kind = remote_files, required: the remote directory (not recursive). `~` and relative paths are "
        "relative to the remote home directory.",
    ),
    Key("interval", "interval", 2.0, "kind = remote_files: rescan this often (at once on inotify, where the host has it)."),
    Key("checksum", "bool", True, "kind = remote_files: compute each file's sha256 on the remote host (event key `sha256`)."),
    Key("heartbeat", "interval", 30.0, "kind = remote_files: the remote watcher prints a heartbeat after this long without output."),
    Key(
        "remote_python",
        "str",
        "auto",
        "kind = remote_files: the remote Python 3.6+ that runs the watcher. `auto`: /usr/bin/python3 if it exists, "
        "else python3 from the remote PATH.",
    ),
    Key("port", "int", None, "kind = remote_files: the ssh port (default: ssh's own, normally 22)."),
    Key(
        "identity",
        "str",
        None,
        "kind = remote_files: a private key file for ssh -i; ssh then offers only this key (IdentitiesOnly=yes). "
        "Relative to the watch directory; `~` and environment variables are expanded.",
    ),
    Key(
        "ssh_options",
        "str_list",
        (),
        'kind = remote_files: extra ssh arguments, placed before keepwatch\'s own, e.g. ["-o", "ProxyJump=bastion"].',
    ),
    Key(
        "ssh_command",
        "argv",
        None,
        'kind = remote_files: the ssh program and leading arguments. Default: ["ssh"].',
    ),
    Key(
        "skip_ledger",
        "str",
        None,
        "kind = remote_files: a ledger of this watch (ctx.ledger(name)) whose keys \"path|size|mtime\" are files "
        "already handled; they are neither hashed nor reported again, which keeps reconnects cheap.",
    ),
)
_OBSERVER_KIND_KEYS = {
    "command": ("command", "stdin", "heartbeat_timeout"),
    "files": ("path", "pattern", "ignore", "recursive", "settle", "marker"),
    "remote_files": (
        "remote",
        "dir",
        "pattern",
        "ignore",
        "settle",
        "interval",
        "checksum",
        "heartbeat",
        "heartbeat_timeout",
        "remote_python",
        "port",
        "identity",
        "ssh_options",
        "ssh_command",
        "skip_ledger",
    ),
}
_OBSERVER_REQUIRED = {"command": ("command",), "files": ("path",), "remote_files": ("remote", "dir")}
_OBSERVER_NON_EMPTY = ("path", "remote", "dir", "remote_python")
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
    remote: str | None = None
    dir: str | None = None
    interval: float = 2.0
    checksum: bool = True
    heartbeat: float = 30.0
    remote_python: str = "auto"
    port: int | None = None
    identity: Path | None = None
    ssh_options: tuple[str, ...] = ()
    ssh_command: tuple[str, ...] | None = None
    skip_ledger: str | None = None
    marker: str = "none"


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
        Key(
            "on_conflict",
            "str",
            "rename",
            "When local_dir already holds a different file of that name (a newer version arrived before the old "
            "one was pushed): `rename` the new one to NAME-1.ext, `overwrite` the old one, or `skip-identical` (fail).",
        ),
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
_RECIPE_CHOICES = {"protocol": PROTOCOLS, "marker": MARKERS, "after": RECIPE_AFTER, "on_conflict": CONFLICTS}


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
        if name == "ssh_options" and converted and not converted[0].startswith("-"):
            collector.add(
                f"'ssh_options' are scp arguments and must start with an option, e.g. ['-o', {converted[0]!r}]",
                key=name,
                table="settings",
                topic="recipes",
            )
            continue
        values[name] = converted
    for name in _RECIPE_REQUIRED[recipe]:
        if values.get(name) in (None, ""):
            collector.add(f"recipe = \"{recipe}\" needs '{name}' in [settings]", table="settings", topic="recipes")
    for name in _RECIPE_PATHS:
        if isinstance(values.get(name), str) and values[name]:
            values[name] = str(_observed_path(values[name], watch_dir))
    if recipe == "pull" and values["delete_remote"] and not values["checksum"]:
        collector.add(
            "delete_remote = true needs checksum = true: the only other copy may go only after a verified pull",
            key="delete_remote",
            table="settings",
            topic="recipes",
        )
    if recipe == "push":
        try:
            dest_remote = not values["dest"] or parse_endpoint(values["dest"]).remote
        except ValueError as exc:
            collector.add(f"'dest': {exc}", key="dest", table="settings", topic="recipes")
            dest_remote = True
        if not dest_remote:
            collector.add(
                f"'dest' must be a remote directory (user@host:dir or scp://user@host/dir), got {values['dest']!r}",
                key="dest",
                table="settings",
                topic="recipes",
            )
        if values["after"] == "archive" and not values["archive_dir"]:
            collector.add("after = \"archive\" needs 'archive_dir' in [settings]", key="after", table="settings", topic="recipes")
        elif values["after"] == "archive" and values["local_dir"] and Path(values["archive_dir"]) == Path(values["local_dir"]):
            collector.add(
                "'archive_dir' must not be 'local_dir': archived files would be pushed again and again",
                key="archive_dir",
                table="settings",
                topic="recipes",
            )
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
        ssh_options += known_hosts_option(settings["known_hosts"])
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


@dataclass(frozen=True)
class WatchConfig:
    name: str
    watch_dir: Path
    description: str = ""
    enabled: bool = True
    interval: float = 60.0
    initial_condition: bool = False
    check_timeout: float = 60.0
    action_timeout: float = 60.0
    max_failures: int = 5
    retry_after: float | None = None
    python_dependencies: tuple[str, ...] = ()
    shell: tuple[str, ...] | None = None
    recipe: str | None = None
    hooks: Mapping[str, Command] = field(default_factory=dict)
    exit_codes: ExitCodes = ExitCodes()
    environment: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)
    observers: Mapping[str, ObserverConfig] = field(default_factory=dict)

    @property
    def config_file(self) -> Path:
        return self.watch_dir / CONFIG_NAME


_INVALID = object()
_HEADER = re.compile(r"^\s*\[\s*(?P<name>[^\]\s]+)\s*\]\s*(#.*)?$")


class _Bad(Exception):
    """Internal: a value has the wrong type or range."""


class _Collector:
    """Collects problems for one file and finds the line a key is on."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.lines = text.splitlines()
        self.problems: list[ConfigProblem] = []

    def add(self, message: str, *, key: str | None = None, table: str | None = None, topic: str = "config") -> None:
        self.problems.append(ConfigProblem(self.path, self.line_of(key, table), message, topic))

    def line_of(self, key: str | None, table: str | None = None) -> int | None:
        if key is None and table is None:
            return None
        key_pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""") if key else None
        in_table = table is None
        for number, line in enumerate(self.lines, start=1):
            header = _HEADER.match(line)
            if header:
                name = header.group("name")
                if key is None and name == table:
                    return number
                in_table = table is not None and name == table
                continue
            if key_pattern is not None and in_table and key_pattern.match(line):
                return number
        return None


def _read(path: Path) -> tuple[dict[str, Any], _Collector]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([ConfigProblem(path, None, f"cannot read file: {exc.strerror or exc}")]) from exc
    collector = _Collector(path, text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        found = re.search(r"line (\d+)", str(exc))
        line = int(found.group(1)) if found else None
        raise ConfigError([ConfigProblem(path, line, f"invalid TOML: {exc}")]) from exc
    return data, collector


def _convert(collector: _Collector, key: Key, value: Any, table: str | None = None) -> Any:
    try:
        if key.kind == "str":
            if not isinstance(value, str):
                raise _Bad("a string")
            return value
        if key.kind == "bool":
            if not isinstance(value, bool):
                raise _Bad("true or false")
            return value
        if key.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _Bad("a whole number >= 0")
            return value
        if key.kind in ("duration", "interval"):
            seconds = parse_duration(value)
            if key.kind == "interval" and seconds < 1:
                raise _Bad("at least 1s")
            return seconds
        if key.kind == "str_list":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise _Bad("a list of strings")
            return tuple(value)
        if key.kind == "argv":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                raise _Bad("a non-empty list of strings")
            return tuple(value)
    except DurationError as exc:
        collector.add(f"'{key.name}': {exc}", key=key.name, table=table)
        return _INVALID
    except _Bad as exc:
        collector.add(f"'{key.name}' must be {exc}, got {value!r}", key=key.name, table=table)
        return _INVALID
    raise AssertionError(f"unknown key kind {key.kind}")


def _unknown_key(collector: _Collector, name: str, valid: Iterable[str], table: str | None = None) -> None:
    close = difflib.get_close_matches(name, list(valid), n=1)
    hint = f" (did you mean '{close[0]}'?)" if close else ""
    where = f" in [{table}]" if table else ""
    collector.add(f"unknown key '{name}'{where}{hint}", key=name, table=table)


def _command(collector: _Collector, value: Any, *, key: str, table: str | None, topic: str) -> Command | None:
    if isinstance(value, str) and value.strip():
        return Command(shell=value)
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return Command(argv=tuple(value))
    collector.add(
        f"'{key}' must be a command: a non-empty string (run by the shell: /bin/sh -c, or PowerShell on Windows) "
        f"or a non-empty list of strings (run directly), got {value!r}",
        key=key,
        table=table,
        topic=topic,
    )
    return None


def _string_table(collector: _Collector, value: Any, table: str) -> dict[str, str]:
    if not isinstance(value, dict):
        collector.add(f"'{table}' must be a table ([{table}])", key=table)
        return {}
    result = {}
    for name, raw in value.items():
        if isinstance(raw, str):
            result[name] = raw
        else:
            collector.add(f"'{name}' must be a string, got {raw!r}", key=name, table=table)
    return result


def _hooks_table(collector: _Collector, value: Any) -> dict[str, Command]:
    if not isinstance(value, dict):
        collector.add("'hooks' must be a table ([hooks])", key="hooks", topic="executables")
        return {}
    hooks = {}
    for name, raw in value.items():
        if name not in HOOK_NAMES:
            _unknown_key(collector, name, HOOK_NAMES, table="hooks")
            continue
        command = _command(collector, raw, key=name, table="hooks", topic="executables")
        if command is not None:
            hooks[name] = command
    return hooks


def _exit_codes_table(collector: _Collector, value: Any) -> ExitCodes:
    table = "check_exit_codes"
    if not isinstance(value, dict):
        collector.add(f"'{table}' must be a table ([{table}])", key=table, topic="executables")
        return ExitCodes()
    lists: dict[str, tuple[int, ...]] = {"true": (0,), "false": (1,), "unknown": ()}
    for name, raw in value.items():
        if name not in lists:
            _unknown_key(collector, name, lists, table=table)
            continue
        if not isinstance(raw, list) or not all(
            isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 255 for code in raw
        ):
            collector.add(f"'{name}' must be a list of exit codes (0-255), got {raw!r}", key=name, table=table)
            continue
        lists[name] = tuple(raw)
    seen: dict[int, str] = {}
    for name, codes in lists.items():
        for code in codes:
            if code in seen:
                collector.add(f"exit code {code} is listed under both '{seen[code]}' and '{name}'", table=table)
            seen[code] = name
    return ExitCodes(**lists)


def _json_default(value: Any) -> Any:
    # datetime.datetime is a subclass of datetime.date, so this covers all three TOML date/time types.
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} cannot be passed to hooks")


def _settings_table(collector: _Collector, value: dict[str, Any]) -> dict[str, Any]:
    """[settings] as JSON-safe data: TOML dates and times become ISO 8601 strings."""
    try:
        return json.loads(json.dumps(value, default=_json_default, allow_nan=False))
    except (TypeError, ValueError):
        collector.add(
            "[settings] must not contain nan or inf (settings are passed to hooks as JSON)",
            table="settings",
        )
        return {}


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
            owners = " or ".join(f'"{owner}"' for owner, names in _OBSERVER_KIND_KEYS.items() if key_name in names)
            collector.add(
                f"'{key_name}' only applies to kind = {owners}; this observer is kind = \"{kind}\"",
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
        if key_name in _OBSERVER_NON_EMPTY and converted == "":
            collector.add(f"'{key_name}' must not be empty", key=key_name, table=table, topic="observers")
            continue
        if key_name == "port" and not 1 <= converted <= 65535:
            collector.add(
                f"'port' must be between 1 and 65535, got {converted}", key="port", table=table, topic="observers"
            )
            continue
        if key_name == "skip_ledger" and not LEDGER_NAME.fullmatch(converted):
            collector.add(
                f"'skip_ledger' must be a ledger name (letters, digits, '_', '.', '-'), got {converted!r}",
                key="skip_ledger",
                table=table,
                topic="observers",
            )
            continue
        if key_name == "marker" and converted not in ("none", "sha256"):
            collector.add(f"'marker' must be none or sha256, got {converted!r}", key="marker", table=table, topic="observers")
            continue
        values[key_name] = converted
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
    for required in _OBSERVER_REQUIRED[kind]:
        if required not in raw:
            collector.add(f"[{table}] (kind = \"{kind}\") needs '{required}'", table=table, topic="observers")
    if len(collector.problems) > before:
        return None
    for key_name in ("path", "identity"):
        if key_name in values:
            values[key_name] = _observed_path(values[key_name], watch_dir)
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


def load_watch_config(watch_dir: Path, defaults: Mapping[str, Any] | None = None) -> WatchConfig:
    """Read and validate <watch_dir>/config.toml. Raises ConfigError listing every problem."""
    path = watch_dir / CONFIG_NAME
    if not path.is_file():
        raise ConfigError([ConfigProblem(path, None, "missing config.toml (every watch needs one, even if empty)")])
    data, collector = _read(path)
    by_name = {key.name: key for key in WATCH_KEYS}
    values: dict[str, Any] = {key.name: key.default for key in WATCH_KEYS}
    values.update(defaults or {})
    hooks: dict[str, Command] = {}
    exit_codes = ExitCodes()
    environment: dict[str, str] = {}
    settings: dict[str, Any] = {}
    observers: dict[str, ObserverConfig] = {}
    for name, value in data.items():
        if name in by_name:
            converted = _convert(collector, by_name[name], value)
            if converted is not _INVALID:
                values[name] = converted
        elif name == "hooks":
            hooks = _hooks_table(collector, value)
        elif name == "check_exit_codes":
            exit_codes = _exit_codes_table(collector, value)
        elif name == "environment":
            environment = _string_table(collector, value, "environment")
        elif name == "settings":
            if isinstance(value, dict):
                settings = _settings_table(collector, value)
            else:
                collector.add("'settings' must be a table ([settings])", key="settings")
        elif name == "observe":
            observers = _observe_table(collector, value, watch_dir)
        else:
            _unknown_key(collector, name, [*by_name, *WATCH_TABLES])
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
    if collector.problems:
        raise ConfigError(collector.problems)
    return WatchConfig(
        name=watch_dir.name,
        watch_dir=watch_dir,
        hooks=MappingProxyType(hooks),
        exit_codes=exit_codes,
        environment=MappingProxyType(environment),
        settings=MappingProxyType(settings),
        observers=MappingProxyType(observers),
        **values,
    )


@dataclass(frozen=True)
class LogSettings:
    max_bytes: int = 10_000_000
    backups: int = 10
    capture_bytes: int = 65_536


@dataclass(frozen=True)
class GlobalConfig:
    path: Path
    watch_dirs: tuple[Path, ...]
    reload_interval: float = 5.0
    alert_command: Command | None = None
    defaults: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    log: LogSettings = LogSettings()


GLOBAL_KEYS = (
    Key(
        "watch_dirs",
        "path_list",
        None,
        "Directories whose subdirectories are watches. Earlier entries win name clashes. "
        "Default: $XDG_CONFIG_HOME/keepwatch/watches.",
    ),
    Key("reload_interval", "interval", 5.0, "Master tick: how often config changes are picked up."),
    Key("alert_command", "command", None, "Run when a watch goes offline or comes back online."),
)

GLOBAL_TABLES = {
    "defaults": "Default values for defaultable watch keys.",
    "environment": "Extra environment variables for every hook.",
    "log": "Log rotation and output capture limits.",
}

LOG_KEYS = (
    Key("max_bytes", "int", 10_000_000, "Rotate the log at this size."),
    Key("backups", "int", 10, "Rotated log files kept."),
    Key(
        "capture_bytes",
        "int",
        65_536,
        "Per stream (stdout, stderr) captured into a log record; longer output keeps head and tail.",
    ),
)


def _watch_dirs(collector: _Collector, value: Any, base: Path) -> tuple[Path, ...] | None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        collector.add(f"'watch_dirs' must be a non-empty list of directory paths, got {value!r}", key="watch_dirs")
        return None
    dirs = []
    for item in value:
        expanded = Path(os.path.expandvars(os.path.expanduser(item)))
        dirs.append(expanded if expanded.is_absolute() else base / expanded)
    return tuple(dirs)


def _defaults_table(collector: _Collector, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        collector.add("'defaults' must be a table ([defaults])", key="defaults")
        return {}
    by_name = {key.name: key for key in WATCH_KEYS}
    allowed = [key.name for key in WATCH_KEYS if key.defaultable]
    defaults = {}
    for name, raw in value.items():
        if name not in by_name:
            _unknown_key(collector, name, allowed, table="defaults")
            continue
        if not by_name[name].defaultable:
            collector.add(
                f"'{name}' cannot be set in [defaults]; defaultable keys: {', '.join(allowed)}",
                key=name,
                table="defaults",
            )
            continue
        converted = _convert(collector, by_name[name], raw, table="defaults")
        if converted is not _INVALID:
            defaults[name] = converted
    return defaults


def _log_table(collector: _Collector, value: Any) -> LogSettings:
    if not isinstance(value, dict):
        collector.add("'log' must be a table ([log])", key="log")
        return LogSettings()
    by_name = {key.name: key for key in LOG_KEYS}
    values = {}
    for name, raw in value.items():
        if name not in by_name:
            _unknown_key(collector, name, by_name, table="log")
            continue
        converted = _convert(collector, by_name[name], raw, table="log")
        if converted is _INVALID:
            continue
        if converted < 1:
            collector.add(f"'{name}' must be at least 1, got {raw!r}", key=name, table="log")
            continue
        values[name] = converted
    return LogSettings(**values)


def load_global_config(path: Path, paths: Paths) -> GlobalConfig:
    """Read and validate the global config. A missing file means all defaults."""
    if not path.exists():
        return GlobalConfig(path=path, watch_dirs=(paths.default_watches_dir,))
    data, collector = _read(path)
    by_name = {key.name: key for key in GLOBAL_KEYS}
    watch_dirs: tuple[Path, ...] | None = (paths.default_watches_dir,)
    reload_interval = 5.0
    alert_command = None
    defaults: dict[str, Any] = {}
    environment: dict[str, str] = {}
    log = LogSettings()
    for name, value in data.items():
        if name == "watch_dirs":
            watch_dirs = _watch_dirs(collector, value, path.parent)
        elif name == "reload_interval":
            converted = _convert(collector, by_name[name], value)
            if converted is not _INVALID:
                reload_interval = converted
        elif name == "alert_command":
            alert_command = _command(collector, value, key=name, table=None, topic="failures")
        elif name == "defaults":
            defaults = _defaults_table(collector, value)
        elif name == "environment":
            environment = _string_table(collector, value, "environment")
        elif name == "log":
            log = _log_table(collector, value)
        else:
            _unknown_key(collector, name, [*by_name, *GLOBAL_TABLES])
    if collector.problems or watch_dirs is None:
        raise ConfigError(collector.problems)
    return GlobalConfig(
        path=path,
        watch_dirs=watch_dirs,
        reload_interval=reload_interval,
        alert_command=alert_command,
        defaults=MappingProxyType(defaults),
        environment=MappingProxyType(environment),
        log=log,
    )


_WATCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def is_valid_watch_name(name: str) -> bool:
    """Letters, digits, '_', '.' and '-', starting with a letter or digit."""
    return bool(_WATCH_NAME.fullmatch(name))


@dataclass(frozen=True)
class Discovery:
    watches: Mapping[str, Path]
    problems: tuple[ConfigProblem, ...]


def discover_watches(global_config: GlobalConfig) -> Discovery:
    """Find watch directories in watch_dirs order. Earlier directories win duplicate names."""
    found: dict[str, Path] = {}
    problems: list[ConfigProblem] = []
    for base in global_config.watch_dirs:
        if not base.is_dir():
            problems.append(ConfigProblem(base, None, "watches directory does not exist (create it or fix watch_dirs)"))
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not _WATCH_NAME.fullmatch(entry.name):
                problems.append(
                    ConfigProblem(entry, None, "watch directory names may contain only letters, digits, '_', '.' and '-'")
                )
                continue
            if entry.name in found:
                problems.append(
                    ConfigProblem(
                        entry,
                        None,
                        f"duplicate watch name '{entry.name}': {found[entry.name]} wins "
                        "(it comes first in watch_dirs); this one is skipped",
                    )
                )
                continue
            found[entry.name] = entry
    return Discovery(MappingProxyType(found), tuple(problems))


class WatchNotFound(Exception):
    """No watch with the requested name."""


def find_watch(discovery: Discovery, name: str) -> Path:
    if name in discovery.watches:
        return discovery.watches[name]
    close = difflib.get_close_matches(name, list(discovery.watches), n=1)
    hint = f" (did you mean '{close[0]}'?)" if close else ""
    known = ", ".join(discovery.watches) or "none"
    raise WatchNotFound(f"no watch named '{name}'{hint}; known watches: {known}")
