"""Configuration files: schema, parsing and validation with line numbers."""

from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from keepwatch.durations import DurationError, parse_duration
from keepwatch.hooks import HOOK_NAMES
from keepwatch.paths import Paths

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
    """A command hook: argv runs directly, shell runs via /bin/sh -c."""

    argv: tuple[str, ...] | None = None
    shell: str | None = None

    def to_argv(self) -> list[str]:
        if self.shell is not None:
            return ["/bin/sh", "-c", self.shell]
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
)

WATCH_TABLES = {
    "hooks": "Hooks implemented as commands: hook name = string (run via /bin/sh -c) or list (run directly).",
    "check_exit_codes": "Exit-code mapping for a command check: true, false, unknown (lists of 0-255).",
    "environment": "Extra environment variables for this watch's hooks.",
    "settings": "Free-form plugin parameters: ctx.settings in Python, KEEPWATCH_SETTING_* for commands.",
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
    hooks: Mapping[str, Command] = field(default_factory=dict)
    exit_codes: ExitCodes = ExitCodes()
    environment: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)

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
        f"'{key}' must be a command: a non-empty string (run via /bin/sh -c) "
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
        else:
            _unknown_key(collector, name, [*by_name, *WATCH_TABLES])
    if collector.problems:
        raise ConfigError(collector.problems)
    return WatchConfig(
        name=watch_dir.name,
        watch_dir=watch_dir,
        hooks=MappingProxyType(hooks),
        exit_codes=exit_codes,
        environment=MappingProxyType(environment),
        settings=MappingProxyType(settings),
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
