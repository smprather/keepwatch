"""alert_command: run when a watch goes offline or comes back online (spec 8.4)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from keepwatch.config import Command
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.protocol import clip


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
    try:
        completed = subprocess.run(
            command.to_argv(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        status = "timeout"
        fields["reason"] = f"timed out after {format_duration(timeout)}"
    except OSError as exc:
        status = "failed"
        fields["reason"] = f"cannot start alert_command: {exc.strerror or exc}"
    else:
        status = "ok" if completed.returncode == 0 else "failed"
        fields["exit_code"] = completed.returncode
        fields["stdout"] = clip(completed.stdout, capture_bytes)[0]
        fields["stderr"] = clip(completed.stderr, capture_bytes)[0]
        if status == "failed":
            fields["reason"] = f"exit code {completed.returncode}"
    sink(
        make_record(
            "alert.end",
            level="INFO" if status == "ok" else "WARNING",
            status=status,
            duration=round(time.monotonic() - started, 3),
            **fields,
        )
    )
