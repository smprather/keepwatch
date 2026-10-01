"""keepwatch's online reference: narrative Markdown topics plus sections generated from the code."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from importlib import resources

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
