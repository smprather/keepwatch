"""Terminal output: plain for agents and pipes, Rich on a terminal."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from keepwatch.runner import FAILED_STATUSES

LEVEL_STYLES = {"DEBUG": "dim", "INFO": "", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold red"}


def plain_output(stream: TextIO | None = None) -> bool:
    """True when output should be plain: not a terminal, or NO_COLOR is set."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return not (isatty is not None and isatty())


def make_console(*, stderr: bool = False) -> Console:
    """A Console for the current stdout/stderr (call it inside a command, not at import)."""
    stream = sys.stderr if stderr else sys.stdout
    if plain_output(stream):
        return Console(
            file=stream,
            color_system=None,
            force_terminal=False,
            highlight=False,
            markup=False,
            emoji=False,
            soft_wrap=True,
        )
    return Console(file=stream, highlight=False, markup=False, emoji=False)


def _condition(value: Any) -> str:
    return "TRUE" if value else "FALSE"


def _block(name: str, text: str) -> str:
    return f"\n  {name}:\n" + textwrap.indent(text.rstrip("\n"), "    ")


def _poll_start(record: dict[str, Any], verbose: bool) -> str:
    tags = [tag for tag, on in (("fake", record.get("faked")), ("dry run", record.get("dry_run"))) if on]
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return f"poll {record.get('poll_id')} start: condition {_condition(record.get('condition'))}{suffix}"


def _hook_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"{record.get('hook')} → {record.get('status')} ({record.get('duration', 0):.2f}s) {record.get('target')}"
    if record.get("reason"):
        line += f": {record['reason']}"
    if record.get("exit_code") not in (None, 0):
        line += f" [exit {record['exit_code']}]"
    if record.get("signal"):
        line += f" [signal {record['signal']}]"
    if verbose or record.get("status") in FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
        exception = record.get("exception") or {}
        if exception.get("traceback"):
            line += _block("traceback", exception["traceback"])
    return line


def _check_outcome(record: dict[str, Any], verbose: bool) -> str:
    before, after = record.get("condition_before"), record.get("condition_after")
    change = _condition(before) if before == after else f"{_condition(before)} → {_condition(after)}"
    actions = ", ".join(record.get("actions") or []) or "none"
    line = f"outcome {record.get('outcome')}; condition {change}; actions: {actions}"
    if record.get("reason"):
        line += f"; reason: {record['reason']}"
    if verbose and record.get("payload") is not None:
        line += f"\n  payload: {json.dumps(record['payload'], ensure_ascii=False)[:2000]}"
    return line


def _command(record: dict[str, Any], verbose: bool) -> str:
    argv = record.get("argv")
    shown = argv if isinstance(argv, str) else " ".join(str(item) for item in argv or [])
    result = "timed out" if record.get("timed_out") else f"exit {record.get('exit_code')}"
    line = f"  $ {shown} → {result} ({record.get('duration', 0):.2f}s)"
    if verbose or record.get("exit_code") not in (0,):
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line


def _plugin_log(record: dict[str, Any], verbose: bool) -> str:
    line = f"  [{record.get('level')}] {record.get('message')}"
    if record.get("fields"):
        line += f" {json.dumps(record['fields'], ensure_ascii=False, default=str)}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _poll_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"poll {'FAILED' if record.get('failed') else 'ok'}; failures {record.get('failures')}"
    if record.get("dry_run"):
        line += " (dry run: actions assumed to succeed)"
    return line


def _service_start(record: dict[str, Any], verbose: bool) -> str:
    return f"service started (version {record.get('version')}, config {record.get('config')})"


def _service_stop(record: dict[str, Any], verbose: bool) -> str:
    return "service stopped"


def _service_error(record: dict[str, Any], verbose: bool) -> str:
    line = f"service error: {record.get('error')}"
    if verbose and record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _cli_error(record: dict[str, Any], verbose: bool) -> str:
    return f"command failed: {record.get('error')}"


def _config_loaded(record: dict[str, Any], verbose: bool) -> str:
    return f"config reloaded: {record.get('path')}"


def _config_error(record: dict[str, Any], verbose: bool) -> str:
    error = str(record.get("error") or "")
    lines = error.splitlines() or [""]
    note = " (still running the previous config)" if record.get("running_previous") else ""
    line = f"config error{note}: {lines[0]}"
    if len(lines) > 1:
        line += _block("error", error)
    return line


def _watch_added(record: dict[str, Any], verbose: bool) -> str:
    return "watch added" + (" (parked: enabled = false)" if record.get("enabled") is False else "")


def _watch_changed(record: dict[str, Any], verbose: bool) -> str:
    return "config changed; applies from the next poll"


def _watch_removed(record: dict[str, Any], verbose: bool) -> str:
    return "watch removed"


def _watch_offline(record: dict[str, Any], verbose: bool) -> str:
    line = f"OFFLINE: {record.get('reason')}"
    if record.get("last_failure"):
        line += f"; last failure: {record['last_failure']}"
    return line


def _watch_online(record: dict[str, Any], verbose: bool) -> str:
    return f"back online: {record.get('reason')}"


def _watch_crash(record: dict[str, Any], verbose: bool) -> str:
    line = f"keepwatch internal error: {record.get('error')}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _alert_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"alert_command ({record.get('alert_event')}) → {record.get('status')} ({record.get('duration', 0):.2f}s)"
    if record.get("reason"):
        line += f": {record['reason']}"
    if verbose or record.get("status") in FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line


_SKIP = {"ts", "level", "event", "pid", "watch", "poll_id"}


def _generic(record: dict[str, Any]) -> str:
    rest = {key: value for key, value in record.items() if key not in _SKIP}
    return f"{record.get('event')} {json.dumps(rest, ensure_ascii=False, default=str)}"


_FORMATTERS: dict[str, Callable[[dict[str, Any], bool], str]] = {
    "poll.start": _poll_start,
    "hook.end": _hook_end,
    "check.outcome": _check_outcome,
    "command": _command,
    "plugin.log": _plugin_log,
    "poll.end": _poll_end,
    "service.start": _service_start,
    "service.stop": _service_stop,
    "service.error": _service_error,
    "cli.error": _cli_error,
    "config.loaded": _config_loaded,
    "config.error": _config_error,
    "watch.added": _watch_added,
    "watch.changed": _watch_changed,
    "watch.removed": _watch_removed,
    "watch.offline": _watch_offline,
    "watch.online": _watch_online,
    "watch.crash": _watch_crash,
    "alert.end": _alert_end,
}


def format_record(record: dict[str, Any], *, verbose: bool = False) -> str:
    """One human-readable line (plus indented blocks) for a log record."""
    time_part = str(record.get("ts", ""))[11:19]
    prefix = f"{time_part} {record.get('watch', '')}".strip()
    formatter = _FORMATTERS.get(str(record.get("event")))
    body = formatter(record, verbose) if formatter else _generic(record)
    return f"{prefix} {body}" if prefix else body


class ConsolePrinter:
    """A log sink that prints records to a Console."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose

    def __call__(self, record: dict[str, Any]) -> None:
        style = LEVEL_STYLES.get(str(record.get("level", "INFO")), "")
        self.console.print(Text(format_record(record, verbose=self.verbose), style=style))
