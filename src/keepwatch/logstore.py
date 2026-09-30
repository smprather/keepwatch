"""Log records and the JSON-lines log file (spec section 12.1)."""

from __future__ import annotations

import json
import os
import threading
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
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        self._backup(self.backups).unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self._backup(index)
            if source.exists():
                source.rename(self._backup(index + 1))
        self.path.rename(self._backup(1))
