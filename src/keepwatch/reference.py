"""keepwatch's online reference: narrative Markdown topics plus sections generated from the code."""

from __future__ import annotations

import difflib
import inspect
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

TOPICS: tuple[tuple[str, str], ...] = (
    ("agent", "Start here: the whole contract, the development loop, a done checklist, common mistakes"),
    ("overview", "What keepwatch does, its vocabulary and how the pieces fit"),
    ("config", "Every key of the global and watch config files"),
    ("python", "watch.py: hooks, return values, exceptions, imports"),
    ("ctx", "Every Ctx and Ledger member, from the code"),
    ("executables", "Command hooks: exit codes, environment variables, payload files"),
    ("states", "Outcomes, the state table, startup, retries and ordering"),
    ("failures", "Failed polls, backoff, offline, enable, retry_after and alert_command"),
    ("storage", "Directories, persistent and run-only data, ledgers, renaming"),
    ("logging", "The log file, every event, ctx.log and keepwatch logs"),
    ("environment", "What hooks run inside: cwd, stdin, variables, PATH, ssh-agent, timeouts"),
    ("dependencies", "python_dependencies and uv"),
    ("reload", "How config changes are picked up while the service runs"),
    ("cli", "Every command and option"),
    ("examples", "Complete example watches (tested)"),
)

GENERATED: dict[str, Callable[[], str]] = {}


class UnknownTopic(KeyError):
    """No topic with that name; str() suggests the closest one."""

    def __str__(self) -> str:
        name = self.args[0]
        names = [topic for topic, _ in TOPICS]
        close = difflib.get_close_matches(name, names, n=1)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        return f"no docs topic '{name}'{hint}; topics: {', '.join(names)}"


def narrative(name: str) -> str:
    return resources.files("keepwatch").joinpath("reference", f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def render_topic(name: str) -> str:
    if name not in dict(TOPICS):
        raise UnknownTopic(name)
    if name in GENERATED:
        return GENERATED[name]()
    return narrative(name)


def topic_index() -> str:
    lines = [
        "# keepwatch reference",
        "",
        "Read a topic with `keepwatch docs <topic>`; `keepwatch docs --all` prints every topic in this order.",
        "New to keepwatch, or writing a watch? Start with `keepwatch docs agent`.",
        "",
        "| Topic | Contents |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | {summary} |" for name, summary in TOPICS]
    return "\n".join(lines)


def render_all() -> str:
    return "\n\n".join([topic_index(), *(render_topic(name) for name, _ in TOPICS)])


_KIND_NAMES = {
    "str": "string",
    "bool": "boolean",
    "int": "integer ≥ 0",
    "duration": "duration",
    "interval": "duration ≥ 1s",
    "str_list": "list of strings",
    "path_list": "list of paths",
    "command": "command (string or list)",
}


def _default_text(key: Any) -> str:
    from keepwatch.durations import format_duration

    value = key.default
    if value is None:
        return "none"
    if key.kind in ("duration", "interval"):
        return f'`"{format_duration(value)}"`'
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, tuple):
        return "`[]`"
    if value == "":
        return '`""`'
    return f"`{value}`"


def _key_table(keys: Any, *, defaultable_column: bool) -> list[str]:
    header = "| Key | Type | Default | Meaning |"
    rule = "|---|---|---|---|"
    if defaultable_column:
        header = "| Key | Type | Default | In `[defaults]` | Meaning |"
        rule = "|---|---|---|---|---|"
    rows = [header, rule]
    for key in keys:
        cells = [f"`{key.name}`", _KIND_NAMES[key.kind], _default_text(key)]
        if defaultable_column:
            cells.append("yes" if key.defaultable else "no")
        cells.append(key.doc)
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def config_topic() -> str:
    from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, WATCH_KEYS, WATCH_TABLES

    lines = [narrative("config"), "", "## Watch config.toml", ""]
    lines += _key_table(WATCH_KEYS, defaultable_column=True)
    lines += ["", "Tables:", ""]
    lines += [f"- `[{name}]`: {doc}" for name, doc in WATCH_TABLES.items()]
    lines += ["", "## Global config.toml", ""]
    lines += _key_table(
        [key for key in GLOBAL_KEYS],
        defaultable_column=False,
    )
    lines += ["", "Tables:", ""]
    lines += [f"- `[{name}]`: {doc}" for name, doc in GLOBAL_TABLES.items()]
    lines += ["", "### `[log]`", ""]
    lines += _key_table(LOG_KEYS, defaultable_column=False)
    return "\n".join(lines)


def _signature(member: Any) -> str:
    signature = inspect.signature(member)
    parameters = list(signature.parameters.values())[1:]
    return str(signature.replace(parameters=parameters)).replace("'", "")


def _members(owner: type, prefix: str) -> list[str]:
    lines = []
    for name, member in inspect.getmembers(owner):
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            lines += [f"### `{prefix}{name}`", "", inspect.getdoc(member) or "", ""]
        elif inspect.isfunction(member):
            lines += [f"### `{prefix}{name}{_signature(member)}`", "", inspect.getdoc(member) or "", ""]
    return lines


def ctx_topic() -> str:
    from keepwatch.ctx import Ctx, Ledger

    lines = [narrative("ctx"), "", "## Methods and properties", ""]
    lines += _members(Ctx, "ctx.")
    lines += [
        "## Ledger",
        "",
        inspect.getdoc(Ledger) or "",
        "",
        "Membership and size: `key in ledger`, `len(ledger)`, and iteration (`for key in ledger`, sorted).",
        "",
    ]
    lines += _members(Ledger, "ledger.")
    return "\n".join(lines).rstrip()


def cli_topic() -> str:
    import click

    from keepwatch.cli import cli

    root = click.Context(cli, info_name="keepwatch")
    lines = [narrative("cli"), "", "## keepwatch (global options)", ""]
    lines += _options(cli)
    for name in cli.list_commands(root):
        command = cli.get_command(root, name)
        sub = click.Context(command, info_name=name, parent=root)
        usage = " ".join(command.collect_usage_pieces(sub))
        lines += [
            f"## keepwatch {name}",
            "",
            f"`keepwatch {name} {usage}`",
            "",
            inspect.cleandoc(command.help or ""),
            "",
        ]
        lines += _options(command)
    return "\n".join(lines).rstrip()


def _options(command: Any) -> list[str]:
    import click

    rows = []
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        names = ", ".join(f"`{opt}`" for opt in (*param.opts, *param.secondary_opts))
        if param.is_flag:
            kind = "flag"
        else:
            kind = param.type.name
            if param.multiple:
                kind += ", repeatable"
        unset = getattr(click.core, "UNSET", None)  # click >= 8.2 marks "no default" with a sentinel
        no_default = param.default in (None, False, ()) or (unset is not None and param.default is unset)
        default = "" if no_default else f"`{param.default}`"
        rows.append(f"| {names} | {kind} | {default} | {param.help or ''} |")
    if not rows:
        return []
    return ["| Option | Type | Default | Meaning |", "|---|---|---|---|", *rows, ""]


GENERATED.update({"config": config_topic, "ctx": ctx_topic, "cli": cli_topic})


_LANGUAGES = {".toml": "toml", ".py": "python", ".sh": "sh", ".exp": "tcl"}


def example_dirs() -> list[Path]:
    root = Path(str(resources.files("keepwatch").joinpath("examples")))
    return sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(("_", ".")))


def examples_topic() -> str:
    lines = [narrative("examples")]
    for directory in example_dirs():
        lines += ["", f"## {directory.name}"]
        for path in sorted(directory.iterdir(), key=lambda p: (p.name != "config.toml", p.name)):
            if not path.is_file():
                continue
            language = _LANGUAGES.get(path.suffix, "")
            text = path.read_text(encoding="utf-8").rstrip("\n")
            lines += ["", f"### {directory.name}/{path.name}", "", f"```{language}", text, "```"]
    return "\n".join(lines)


GENERATED["examples"] = examples_topic
