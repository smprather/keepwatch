"""Reading the JSON-lines log: rotated files, filters, following (keepwatch logs)."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from keepwatch import platform
from keepwatch.durations import DurationError, parse_duration
from keepwatch.logstore import LEVELS
from keepwatch.runner import FAILED_STATUSES


def log_files(path: Path) -> list[Path]:
    """Rotated files oldest first (highest number first), then the current file."""
    rotated = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name[len(path.name) + 1:]
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    files = [candidate for _, candidate in sorted(rotated, reverse=True)]
    if path.exists():
        files.append(path)
    return files


def _parse_line(line: bytes | str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    for file in log_files(path):
        try:
            handle = platform.open_shared(file)
        except FileNotFoundError:
            continue
        with handle:
            for line in handle:
                record = _parse_line(line)
                if record is not None:
                    yield record


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """A duration ago ("1h", "2d") or an ISO date/time ("2026-09-30", "2026-09-30T14:00"); naive means local."""
    now = now or datetime.now().astimezone()
    try:
        return now - timedelta(seconds=parse_duration(text))
    except DurationError:
        pass
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f'{text!r} is neither a duration ("1h", "2d") nor a time ("2026-09-30", "2026-09-30T14:00")'
        ) from None
    return value if value.tzinfo is not None else value.astimezone()


def _stamp(record: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(record.get("ts")))
    except ValueError:
        return None


def is_failure(record: dict[str, Any]) -> bool:
    if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) >= LEVELS["ERROR"]:
        return True
    if record.get("event") == "poll.end" and record.get("failed"):
        return True
    return record.get("event") in ("hook.end", "alert.end") and record.get("status") in FAILED_STATUSES


@dataclass(frozen=True)
class LogFilter:
    watch: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    min_level: str | None = None
    events: tuple[str, ...] = ()
    failed: bool = False
    poll_id: str | None = None

    def matches(self, record: dict[str, Any]) -> bool:
        if self.watch is not None and record.get("watch") != self.watch:
            return False
        if self.poll_id is not None and not str(record.get("poll_id", "")).startswith(self.poll_id):
            return False
        event = str(record.get("event", ""))
        if self.events and not any(event == name or event.startswith(f"{name}.") for name in self.events):
            return False
        if self.min_level is not None:
            if LEVELS.get(str(record.get("level")), LEVELS["INFO"]) < LEVELS[self.min_level]:
                return False
        if self.failed and not is_failure(record):
            return False
        if self.since is not None or self.until is not None:
            stamp = _stamp(record)
            if stamp is None:
                return False
            if self.since is not None and stamp < self.since:
                return False
            if self.until is not None and stamp > self.until:
                return False
        return True


def select_records(path: Path, log_filter: LogFilter, limit: int) -> list[dict[str, Any]]:
    """The last `limit` matching records (all of them when limit is 0), oldest first."""
    matches: deque[dict[str, Any]] = deque(maxlen=limit or None)
    for record in read_records(path):
        if log_filter.matches(record):
            matches.append(record)
    return list(matches)


def follow_log(
    path: Path,
    emit: Callable[[dict[str, Any]], None],
    *,
    stop: Callable[[], bool],
    interval: float = 0.5,
) -> None:
    """Emit records appended to the log after this starts, across rotations, until stop() is true."""

    def open_current() -> tuple[Any, int | None]:
        try:
            handle = platform.open_shared(path)
        except FileNotFoundError:
            return None, None
        return handle, os.fstat(handle.fileno()).st_ino

    handle, inode = open_current()
    if handle is not None:
        handle.seek(0, os.SEEK_END)
    buffer = b""
    try:
        while not stop():
            if handle is None:
                handle, inode = open_current()
                buffer = b""
            if handle is not None:
                chunk = handle.read()
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        record = _parse_line(line)
                        if record is not None:
                            emit(record)
                    continue
                try:
                    current = os.stat(path).st_ino
                except FileNotFoundError:
                    current = None
                if current != inode:
                    # Rotated: finish the old file first, including a last line without a newline.
                    rest = buffer + handle.read()
                    handle.close()
                    for line in rest.split(b"\n"):
                        record = _parse_line(line) if line.strip() else None
                        if record is not None:
                            emit(record)
                    handle, inode = open_current()
                    buffer = b""
                    continue
            time.sleep(interval)
    finally:
        if handle is not None:
            handle.close()
