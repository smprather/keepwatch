"""keepwatch status: combine status.json, offline markers and the config on disk."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from keepwatch.config import Discovery
from keepwatch.durations import format_duration
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.offline import read_offline
from keepwatch.paths import Paths


def service_running(paths: Paths) -> bool:
    if not paths.runtime.is_dir():
        return False
    try:
        with hold_lock(paths.service_lock, blocking=False):
            return False
    except LockBusy:
        return True


def read_status(paths: Paths) -> dict[str, Any] | None:
    try:
        data = json.loads(paths.status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def orphaned_state(paths: Paths, discovery: Discovery) -> list[str]:
    root = paths.state_home / "watches"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir() and entry.name not in discovery.watches)


def collect_status(paths: Paths, discovery: Discovery, names: list[str]) -> dict[str, Any]:
    running = service_running(paths)
    saved = (read_status(paths) or {}) if running else {}
    saved_watches = saved.get("watches", {})
    watches = {}
    for name in names or list(discovery.watches):
        entry = dict(saved_watches.get(name) or {"known_to_service": False})
        marker = read_offline(paths, name)
        entry["offline"] = marker.to_dict() if marker else None
        entry["exists"] = name in discovery.watches
        watches[name] = entry
    service: dict[str, Any] = {"running": running}
    if running:
        saved_service = saved.get("service", {})
        service.update({key: saved_service.get(key) for key in ("pid", "version", "started", "updated", "config_error")})
    return {
        "service": service,
        "watches": watches,
        "invalid_watches": saved.get("invalid_watches", {}),
        "problems": [str(problem) for problem in discovery.problems],
        "orphaned_state": orphaned_state(paths, discovery),
    }


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _ago(value: Any, now: float) -> str:
    stamp = _epoch(value)
    return "?" if stamp is None else format_duration(max(int(now - stamp), 0))


def _until(value: Any, now: float) -> str:
    stamp = _epoch(value)
    return "?" if stamp is None else format_duration(max(int(stamp - now), 0))


def _watch_line(name: str, entry: dict[str, Any], now: float) -> str:
    if entry.get("offline"):
        label = "offline"
    elif entry.get("enabled") is False:
        label = "parked"
    elif entry.get("known_to_service") is False:
        label = "idle"
    else:
        label = "online"
    parts = [f"{name:<20} {label:<8}"]
    if "condition" in entry:
        parts.append(f"condition {'TRUE' if entry['condition'] else 'FALSE'}")
    if "failures" in entry:
        parts.append(f"failures {entry['failures']}")
    last = entry.get("last_poll")
    if last:
        parts.append(f"last {last['outcome']}{' FAILED' if last['failed'] else ''} {_ago(last['at'], now)} ago")
    if entry.get("next_poll"):
        parts.append(f"next in {_until(entry['next_poll'], now)}")
    return "  ".join(parts)


def format_status(document: dict[str, Any], now: float) -> list[str]:
    service = document["service"]
    if service["running"]:
        lines = [
            f"service: running (pid {service.get('pid')}, version {service.get('version')}, "
            f"status updated {_ago(service.get('updated'), now)} ago)"
        ]
        if service.get("config_error"):
            lines.append(f"    global config error (running the previous config): {service['config_error']}")
    else:
        lines = ["service: not running (start it with: keepwatch run)"]
    for name, entry in document["watches"].items():
        lines.append(_watch_line(name, entry, now))
        offline = entry.get("offline")
        if offline:
            lines.append(f"    offline since {offline['since']}: {offline['reason']}")
            if offline.get("last_failure"):
                lines.append(f"    last failure: {offline['last_failure']}")
        if entry.get("config_error"):
            lines.append(f"    config error (running the previous config): {entry['config_error'].splitlines()[0]}")
        if not entry.get("exists", True):
            lines.append("    the watch directory no longer exists")
    for name, error in document.get("invalid_watches", {}).items():
        lines.append(f"{name:<20} invalid  not started: {error.splitlines()[0]}")
    for problem in document.get("problems", []):
        lines.append(f"problem: {problem}")
    for name in document.get("orphaned_state", []):
        lines.append(f"orphaned state: {name} (state of a watch that no longer exists; to rename a watch use keepwatch rename)")
    return lines
