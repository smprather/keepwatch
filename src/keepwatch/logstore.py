"""Log records and the JSON-lines log file (spec section 12.1)."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from keepwatch.locks import hold_lock

Sink = Callable[[dict[str, Any]], None]


def make_record(event: str, *, level: str = "INFO", **fields: Any) -> dict[str, Any]:
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        "pid": os.getpid(),
        **fields,
    }


def fan_out(*sinks: Sink) -> Sink:
    def emit(record: dict[str, Any]) -> None:
        for sink in sinks:
            sink(record)

    return emit


class LogWriter:
    """Appends records as JSON lines; rotates by size. Safe across threads and processes."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._lock_path = path.parent / f".{path.name}.lock"

    def write(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with hold_lock(self._lock_path):
                try:
                    size = self.path.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size and size + len(line) > self.max_bytes:
                    self._rotate()
                with open(self.path, "ab") as handle:
                    handle.write(line)

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        """Shift the backups. If the log cannot be moved (Windows: someone holds it open), change nothing."""
        if self.backups <= 0:
            try:
                self.path.unlink(missing_ok=True)
            except PermissionError:
                pass
            return
        staging = self.path.with_name(f"{self.path.name}.rotating")
        try:
            self.path.rename(staging)
        except PermissionError:
            return  # try again on a later write
        try:
            self._backup(self.backups).unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self._backup(index)
                if source.exists():
                    source.rename(self._backup(index + 1))
            staging.rename(self._backup(1))
        except PermissionError:
            staging.rename(self.path)  # keep writing to the same log; rotate later


LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_STOP = object()


def level_filter(sink: Sink, minimum: str) -> Sink:
    """Pass on records whose level is at least `minimum` (unknown levels count as INFO)."""
    threshold = LEVELS[minimum]

    def emit(record: dict[str, Any]) -> None:
        if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) >= threshold:
            sink(record)

    return emit


class QueueSink:
    """A sink any thread can call; records are delivered in order on one writer thread."""

    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, name="keepwatch-log", daemon=True)
        self._thread.start()

    def __call__(self, record: dict[str, Any]) -> None:
        self._queue.put(record)

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _STOP:
                return
            try:
                self._sink(record)
            except Exception:
                if sys.stderr is not None:  # no console under pythonw
                    traceback.print_exc()

    def close(self, timeout: float = 10.0) -> None:
        """Deliver everything queued so far, then stop the writer thread."""
        self._queue.put(_STOP)
        self._thread.join(timeout)
