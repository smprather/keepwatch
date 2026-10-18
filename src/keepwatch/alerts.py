"""alert_command: run when a watch goes offline or comes back online (spec 8.4)."""

from __future__ import annotations

import os
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

KILL_GRACE = 5.0  # seconds to collect an alert's output once its whole tree has been killed


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
        # Started like a hook (its own process group on POSIX, a Job Object on Windows), so a timeout can end
        # the whole tree: a grandchild that outlives the child would otherwise keep the pipes open, and
        # `subprocess.run`'s Windows timeout path re-reads them - on the watch's own thread.
        process = platform.start_process(
            argv,
            cwd=None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        status = "failed"
        fields["reason"] = f"cannot start alert_command: {exc.strerror or exc}"
    else:
        stdout = stderr = b""
        try:
            try:
                stdout, stderr = process.popen.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()  # the whole tree, so the pipes close even when a grandchild holds them
                try:
                    stdout, stderr = process.popen.communicate(timeout=KILL_GRACE)
                except subprocess.TimeoutExpired:
                    pass  # nothing arrived; the record says it timed out and the tree is gone
                status = "timeout"
                fields["reason"] = f"timed out after {format_duration(timeout)}"
            else:
                status = "ok" if process.popen.returncode == 0 else "failed"
                fields["exit_code"] = process.popen.returncode
                if status == "failed":
                    fields["reason"] = f"exit code {process.popen.returncode}"
            # UTF-8 like every hook's output, not the locale's encoding
            fields["stdout"] = clip((stdout or b"").decode("utf-8", "replace"), capture_bytes)[0]
            fields["stderr"] = clip((stderr or b"").decode("utf-8", "replace"), capture_bytes)[0]
        finally:
            process.close()  # Windows: releases the job, which ends anything left in it
    sink(
        make_record(
            "alert.end",
            level="INFO" if status == "ok" else "WARNING",
            status=status,
            duration=round(time.monotonic() - started, 3),
            **fields,
        )
    )
