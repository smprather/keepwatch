"""Terminal output: plain for agents and pipes, Rich on a terminal."""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from keepwatch.durations import format_duration
from keepwatch.runner import FAILED_STATUSES

LEVEL_STYLES = {"DEBUG": "dim", "INFO": "", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold red"}

# How much of the log a reader wants: `v` cycles these in the TUI. QUIET keeps failures and state changes,
# NORMAL adds headlines and what actions did, VERBOSE adds the per-poll record of polling happening.
QUIET, NORMAL, VERBOSE = 0, 1, 2

# C0/C1 controls except tab and newline (which the formatter's blocks use), plus the bidi overrides that can
# reorder a line. ESC is the one that matters: Rich keeps it, so an untrusted name or a hook's stderr could
# move the cursor, retitle the window or drive the clipboard (OSC 52) on the operator's terminal.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def sanitize(text: str) -> str:
    """`text` as something a terminal can only display: control characters become visible escape text.

    Everything keepwatch shows may come from somewhere else - a remote file name, a hook's stdout, a server's
    error - so this is applied at the two places that print: `format_record` (the log pane and `keepwatch logs`)
    and the TUI's banner and header. `keepwatch logs --json` stays raw: JSON already escapes these.
    """
    return _UNSAFE.sub(lambda match: f"\\x{ord(match.group()):02x}" if ord(match.group()) < 256
                       else f"\\u{ord(match.group()):04x}", text)


def plain_output(stream: TextIO | None = None) -> bool:
    """True when output should be plain: not a terminal, or NO_COLOR is set."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return not (isatty is not None and isatty())


def use_utf8_output() -> None:
    """Make stdout and stderr write UTF-8 when their encoding cannot (a piped Windows stream is cp1252).

    Python encodes a pipe or a file with the locale's code page, which cannot write the arrows and boxes in
    keepwatch's output and raises UnicodeEncodeError instead; agents read UTF-8. A terminal is already UTF-8
    (or uses the console API), pythonw has no streams at all, and test harnesses have nothing to reconfigure.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not encoding or encoding.lower().replace("-", "") == "utf8" or reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


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


def _poll_start(record: dict[str, Any], level: int) -> str:
    tags = [tag for tag, on in (("fake", record.get("faked")), ("dry run", record.get("dry_run"))) if on]
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return f"poll {record.get('poll_id')} start: condition {_condition(record.get('condition'))}{suffix}"


def _hook_end(record: dict[str, Any], level: int) -> str:
    line = f"{record.get('hook')} → {record.get('status')} ({record.get('duration', 0):.2f}s) {record.get('target')}"
    if record.get("reason"):
        line += f": {record['reason']}"
    if record.get("exit_code") not in (None, 0):
        line += f" [exit {record['exit_code']}]"
    if record.get("signal"):
        line += f" [signal {record['signal']}]"
    if level >= 2 or record.get("status") in FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
        exception = record.get("exception") or {}
        if exception.get("traceback"):
            line += _block("traceback", exception["traceback"])
    return line


def _check_outcome(record: dict[str, Any], level: int) -> str:
    before, after = record.get("condition_before"), record.get("condition_after")
    change = _condition(before) if before == after else f"{_condition(before)} → {_condition(after)}"
    actions = ", ".join(record.get("actions") or []) or "none"
    line = f"outcome {record.get('outcome')}; condition {change}; actions: {actions}"
    if record.get("reason"):
        line += f"; reason: {record['reason']}"
    if level >= 2 and record.get("payload") is not None:
        line += f"\n  payload: {json.dumps(record['payload'], ensure_ascii=False)[:2000]}"
    return line


def _command(record: dict[str, Any], level: int) -> str:
    argv = record.get("argv")
    shown = argv if isinstance(argv, str) else " ".join(str(item) for item in argv or [])
    result = "timed out" if record.get("timed_out") else f"exit {record.get('exit_code')}"
    line = f"  $ {shown} → {result} ({record.get('duration', 0):.2f}s)"
    if level >= 2 or record.get("exit_code") not in (0,):
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line


def _plugin_log(record: dict[str, Any], level: int) -> str:
    line = f"  [{record.get('level')}] {record.get('message')}"
    if record.get("fields"):
        line += f" {json.dumps(record['fields'], ensure_ascii=False, default=str)}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _plugin_event(record: dict[str, Any], level: int) -> str:
    return str(record.get("message") or "")


def _poll_end(record: dict[str, Any], level: int) -> str:
    line = f"poll {'FAILED' if record.get('failed') else 'ok'}; failures {record.get('failures')}"
    if record.get("dry_run"):
        line += " (dry run: actions assumed to succeed)"
    return line


def _service_start(record: dict[str, Any], level: int) -> str:
    return f"service started (version {record.get('version')}, config {record.get('config')})"


def _service_stop(record: dict[str, Any], level: int) -> str:
    return "service stopped"


def _service_error(record: dict[str, Any], level: int) -> str:
    line = f"service error: {record.get('error')}"
    if level >= 2 and record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _cli_error(record: dict[str, Any], level: int) -> str:
    return f"command failed: {record.get('error')}"


def _config_loaded(record: dict[str, Any], level: int) -> str:
    return f"config reloaded: {record.get('path')}"


def _config_error(record: dict[str, Any], level: int) -> str:
    error = str(record.get("error") or "")
    lines = error.splitlines() or [""]
    note = " (still running the previous config)" if record.get("running_previous") else ""
    line = f"config error{note}: {lines[0]}"
    if len(lines) > 1:
        line += _block("error", error)
    return line


def _watch_added(record: dict[str, Any], level: int) -> str:
    return "watch added" + (" (parked: enabled = false)" if record.get("enabled") is False else "")


def _watch_changed(record: dict[str, Any], level: int) -> str:
    return "config changed; applies from the next poll"


def _watch_removed(record: dict[str, Any], level: int) -> str:
    return "watch removed"


def _watch_offline(record: dict[str, Any], level: int) -> str:
    line = f"OFFLINE: {record.get('reason')}"
    if record.get("last_failure"):
        line += f"; last failure: {record['last_failure']}"
    return line


def _watch_online(record: dict[str, Any], level: int) -> str:
    return f"back online: {record.get('reason')}"


def _watch_crash(record: dict[str, Any], level: int) -> str:
    line = f"keepwatch internal error: {record.get('error')}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _alert_end(record: dict[str, Any], level: int) -> str:
    line = f"alert_command ({record.get('alert_event')}) → {record.get('status')} ({record.get('duration', 0):.2f}s)"
    if record.get("reason"):
        line += f": {record['reason']}"
    if level >= 2 or record.get("status") in FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line


def _observer_started(record: dict[str, Any], level: int) -> str:
    source = record.get("path") or " ".join(str(item) for item in record.get("argv") or [])
    line = f"observer {record.get('observer')} started ({record.get('kind')}): {source}"
    if record.get("native") is False:
        line += f" [no notifications, rescanning: {record.get('native_error')}]"
    return line


def _observer_stopped(record: dict[str, Any], level: int) -> str:
    line = f"observer {record.get('observer')} stopped: {record.get('reason')}"
    if record.get("exit_code") is not None:
        line += f" [exit {record['exit_code']}]"
    tail = record.get("stderr_tail") or []
    if tail and (level >= 2 or record.get("reason") != "stopped"):
        line += _block("stderr", "\n".join(tail))
    return line


def _observer_restarting(record: dict[str, Any], level: int) -> str:
    return f"observer {record.get('observer')} restarting in {format_duration(float(record.get('delay') or 0))}"


def _observer_output(record: dict[str, Any], level: int) -> str:
    return f"observer {record.get('observer')} {record.get('stream')}: {record.get('text')}"


def _observer_event(record: dict[str, Any], level: int) -> str:
    data = record.get("data_clipped") or json.dumps(record.get("data"), ensure_ascii=False, default=str)
    return f"observer {record.get('observer')} event: {data[:2000]}"


def _observer_dropped(record: dict[str, Any], level: int) -> str:
    return f"event queue full ({record.get('cap')} events): {record.get('dropped')} oldest event(s) dropped so far"


def _observer_connected(record: dict[str, Any], level: int) -> str:
    notify = "inotify" if record.get("inotify") else "rescanning"
    return (
        f"observer {record.get('observer')} connected to {record.get('remote')}: watching {record.get('dir')} "
        f"(remote Python {record.get('python')}, {notify})"
    )


_SKIP = {"ts", "level", "event", "pid", "watch", "poll_id"}


def _generic(record: dict[str, Any]) -> str:
    rest = {key: value for key, value in record.items() if key not in _SKIP}
    return f"{record.get('event')} {json.dumps(rest, ensure_ascii=False, default=str)}"


_FORMATTERS: dict[str, Callable[[dict[str, Any], int], str]] = {
    "poll.start": _poll_start,
    "hook.end": _hook_end,
    "check.outcome": _check_outcome,
    "command": _command,
    "plugin.log": _plugin_log,
    "plugin.event": _plugin_event,
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
    "observer.started": _observer_started,
    "observer.connected": _observer_connected,
    "observer.stopped": _observer_stopped,
    "observer.restarting": _observer_restarting,
    "observer.output": _observer_output,
    "observer.event": _observer_event,
    "observer.dropped": _observer_dropped,
    "observer.crash": _watch_crash,
    "alert.end": _alert_end,
}


def record_level(record: dict[str, Any]) -> int:
    """The lowest verbosity at which this record is worth showing (see QUIET/NORMAL/VERBOSE).

    Failures and state changes are QUIET; headlines, action results and configuration changes are NORMAL;
    the per-poll record of polling happening (and captured output) is VERBOSE. Unknown events stay visible at
    VERBOSE rather than disappearing: a new event type is not noise until it is deliberately classified.
    """
    event = str(record.get("event"))
    severity = str(record.get("level", "INFO"))
    if event == "plugin.log":
        if severity in ("WARNING", "ERROR", "CRITICAL"):
            return QUIET
        return NORMAL if severity == "INFO" else VERBOSE
    if event == "plugin.event":
        if severity in ("WARNING", "ERROR", "CRITICAL"):
            return QUIET
        return VERBOSE if severity == "DEBUG" else NORMAL
    if event == "hook.end":
        if record.get("status") in FAILED_STATUSES:
            return QUIET
        return VERBOSE if record.get("hook") == "check" else NORMAL
    if event == "poll.end":
        return QUIET if record.get("failed") else VERBOSE
    if event == "check.outcome":
        return NORMAL if record.get("condition_before") != record.get("condition_after") else VERBOSE
    if event == "command":
        return QUIET if record.get("timed_out") or record.get("exit_code") not in (0, None) else VERBOSE
    if event in ("watch.offline", "watch.online", "service.error", "cli.error", "config.error", "watch.crash",
                 "observer.crash", "observer.dropped", "alert.end"):
        return QUIET
    if event == "observer.stopped":
        return NORMAL if record.get("reason") == "stopped" else QUIET
    if event in ("service.start", "service.stop", "config.loaded", "watch.added", "watch.changed",
                 "watch.removed", "observer.started", "observer.connected", "observer.restarting"):
        return NORMAL
    return VERBOSE


def format_record(record: dict[str, Any], *, level: int = NORMAL) -> str:
    """One human-readable line (plus indented blocks) for a log record, safe for a terminal.

    `level` selects how much detail to include; filtering records by `record_level` is the caller's job.
    """
    time_part = str(record.get("ts", ""))[11:19]
    prefix = f"{time_part} {record.get('watch', '')}".strip()
    formatter = _FORMATTERS.get(str(record.get("event")))
    body = formatter(record, level) if formatter else _generic(record)
    return sanitize(f"{prefix} {body}" if prefix else body)


class ConsolePrinter:
    """A log sink that prints records to a Console, with detail selected by `level` (it never filters)."""

    def __init__(self, console: Console, *, level: int = NORMAL) -> None:
        self.console = console
        self.level = level

    def __call__(self, record: dict[str, Any]) -> None:
        style = LEVEL_STYLES.get(str(record.get("level", "INFO")), "")
        self.console.print(Text(format_record(record, level=self.level), style=style))
