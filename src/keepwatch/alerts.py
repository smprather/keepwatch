"""alert_command: run when a watch goes offline or comes back online (spec 8.4)."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keepwatch import platform
from keepwatch.config import Command
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.protocol import clip


def _kill_alert(process: subprocess.Popen[str]) -> None:
    """Kill the alert and let go of its pipes without waiting for whoever else holds them."""
    try:
        if platform.IS_WINDOWS:
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # start_new_session made it the group leader
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def run_alert(
    command: Command | None,
    *,
    event: str,
    watch: str,
    reason: str,
    environment: Mapping[str, str],
    timeout: float,
    sink: Sink,
    capture_bytes: int = 65_536,
    base: Path | None = None,
) -> None:
    """Run alert_command and log one alert.end record. Never raises."""
    if command is None:
        return
    env = {
        **os.environ,
        **environment,
        "KEEPWATCH_ALERT_EVENT": event,
        "KEEPWATCH_WATCH": watch,
        "KEEPWATCH_ALERT_REASON": reason,
    }
    fields: dict[str, Any] = {
        "watch": watch,
        "alert_event": event,
        "command": command.display(),
        "exit_code": None,
        "reason": None,
        "stdout": "",
        "stderr": "",
    }
    started = time.monotonic()
    argv = command.to_argv()
    if command.argv is not None and base is not None:
        argv = platform.command_argv(argv, base)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",  # the same decoding hooks get; the locale would mojibake a UTF-8 alert
            errors="replace",
            env=env,
            start_new_session=True,
            creationflags=platform.NO_WINDOW,
        )
    except OSError as exc:
        status = "failed"
        fields["reason"] = f"cannot start alert_command: {exc.strerror or exc}"
    else:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill it and stop reading: a grandchild may hold the pipes open, and this runs on the watch's own
            # thread - `subprocess.run`'s own timeout path re-reads them on Windows, which hangs there.
            _kill_alert(process)
            status = "timeout"
            fields["reason"] = f"timed out after {format_duration(timeout)}"
        else:
            status = "ok" if process.returncode == 0 else "failed"
            fields["exit_code"] = process.returncode
            fields["stdout"] = clip(stdout, capture_bytes)[0]
            fields["stderr"] = clip(stderr, capture_bytes)[0]
            if status == "failed":
                fields["reason"] = f"exit code {process.returncode}"
    sink(
        make_record(
            "alert.end",
            level="INFO" if status == "ok" else "WARNING",
            status=status,
            duration=round(time.monotonic() - started, 3),
            **fields,
        )
    )
