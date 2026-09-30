"""Configuration files: schema, parsing and validation with line numbers."""

from __future__ import annotations

import difflib
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
    Key("check_timeout", "duration", 60.0, "Deadline for the check.", True),
    Key("action_timeout", "duration", 60.0, "Deadline for each action.", True),
    Key(
        "max_failures",
        "int",
        5,
        "Consecutive failed polls before the watch goes offline. 0 disables the limit.",
        True,
    ),
    Key("retry_after", "duration", None, "While offline, make one trial poll this often.", True),
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
                settings = value
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
