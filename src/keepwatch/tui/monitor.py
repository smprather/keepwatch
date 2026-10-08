"""The live monitor's data layer: status.json, config discovery and the log, with no UI of its own.

`keepwatch tui` renders this. Nothing here imports Textual and nothing here writes anything, so another
front-end (a web view, an IPC-backed source) can reuse the data layer as it is.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from keepwatch import transfer
from keepwatch.config import ConfigError, Discovery, discover_watches, load_global_config
from keepwatch.durations import format_duration
from keepwatch.logquery import LogFilter, follow_log, select_records
from keepwatch.output import format_record
from keepwatch.paths import Paths
from keepwatch.statusview import collect_status, read_status, service_running

STALE_AFTER = 5.0  # two missed service ticks: the status is no longer live
DISCOVERY_EVERY = 5.0  # how often the config on disk is re-read
FOLLOW_INTERVAL = 0.5  # the log is checked twice a second, as `keepwatch logs --follow` does

_STATE_STYLES = {
    "online": "green",
    "polling": "yellow",
    "offline": "red",
    "parked": "dim",
    "idle": "dim",
    "invalid": "red",
    "orphaned": "red",
}


@dataclass(frozen=True)
class InFlight:
    """A poll whose `poll.start` has no matching `poll.end`: something is running right now."""

    poll_id: str
    started: float


@dataclass(frozen=True)
class WatchRow:
    """One watch as the monitor sees it: live data when the service runs, last-known data (labelled) when not."""

    name: str
    state: str
    condition: bool | None
    failures: int | None
    last_poll: dict[str, Any] | None
    next_poll: str | None
    observers: tuple[int, int] | None
    pending_events: int
    note: str
    interval: float | None
    description: str | None
    watch_dir: str | None
    offline: dict[str, Any] | None
    config_error: str | None
    in_flight: dict[str, Any] | None
    transfer: dict[str, Any] | None


@dataclass(frozen=True)
class Status:
    """Everything the screen shows about the service and its watches."""

    service: dict[str, Any]
    watches: list[WatchRow]
    problems: list[str]
    stale: bool
    stale_since: float | None
    follower_error: str | None


def _epoch(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _first_line(text: Any) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[0] if lines else "?"


class Monitor:
    """Read-only: it never writes to the state directory, the log file or the config."""

    def __init__(
        self,
        paths: Paths,
        config_path: Path,
        *,
        backfill: int = 200,
        per_watch: int = 300,
        history: int = 300,
    ) -> None:
        self.paths = paths
        self.config_path = config_path
        self._backfill = backfill
        self._per_watch = per_watch
        self._lock = threading.Lock()
        self._by_watch: dict[str, deque[dict[str, Any]]] = {}
        self._all: deque[dict[str, Any]] = deque(maxlen=history)
        self._open: dict[str, InFlight] = {}
        self._version = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict[str, Any] | None = None
        self._running = False
        self._pid: int | None = None
        self._discovery: Discovery | None = None
        self._discovery_at = 0.0
        self._discovery_error: str | None = None
        self._follower_error: str | None = None
        self._samples: dict[str, tuple[float, int]] = {}
        self._rates: dict[str, float] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Read the tail of the log once, then follow it in one daemon thread."""
        for record in select_records(self.paths.log_file, LogFilter(), self._backfill):
            self._ingest(record)
        self._thread = threading.Thread(target=self._follow, name="keepwatch-tui-log", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _follow(self) -> None:
        try:
            follow_log(self.paths.log_file, self._ingest, stop=self._stop.is_set, interval=FOLLOW_INTERVAL)
        except Exception as exc:  # the pane stops updating; the banner says why instead of the thread dying mute
            with self._lock:
                self._follower_error = f"{type(exc).__name__}: {exc}"

    def _ingest(self, record: dict[str, Any]) -> None:
        watch = record.get("watch")
        event = record.get("event")
        poll_id = str(record.get("poll_id") or "")
        with self._lock:
            self._version += 1
            self._all.append(record)
            if not isinstance(watch, str):
                return
            self._by_watch.setdefault(watch, deque(maxlen=self._per_watch)).append(record)
            if event == "poll.start":
                self._open[watch] = InFlight(poll_id=poll_id, started=_epoch(record.get("ts")) or time.time())
            elif event == "poll.end":
                current = self._open.get(watch)
                if current is not None and current.poll_id == poll_id:
                    del self._open[watch]
            elif event == "watch.removed":
                self._open.pop(watch, None)

    # -- status --------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read status.json and the service's liveness; re-discover the config when due."""
        self._status = read_status(self.paths)
        self._running = service_running(self.paths)
        pid_raw = ((self._status or {}).get("service") or {}).get("pid")
        pid = pid_raw if isinstance(pid_raw, int) else None
        with self._lock:
            if not self._running or (self._pid is not None and pid != self._pid):
                self._open.clear()  # a stopped or restarted service has no polls in flight
            self._pid = pid if self._running else None
        if self._discovery is None or time.monotonic() - self._discovery_at >= DISCOVERY_EVERY:
            self.rescan()

    def rescan(self) -> None:
        """Re-read the global config now. A broken one keeps the last good discovery and is shown in the banner."""
        self._discovery_at = time.monotonic()
        try:
            config = load_global_config(self.config_path, self.paths)
        except ConfigError as exc:
            self._discovery_error = "\n".join(str(problem) for problem in exc.problems)
            return
        self._discovery = discover_watches(config)
        self._discovery_error = None

    def status(self) -> Status:
        if self._status is None:
            self.refresh()
        discovery = self._discovery or Discovery(watches={}, problems=())
        document = collect_status(self.paths, discovery, [])
        saved_service = (self._status or {}).get("service") or {}
        service = dict(document["service"])
        for key in ("pid", "version", "started", "updated", "config", "config_error", "only"):
            if service.get(key) is None:
                service[key] = saved_service.get(key)
        age = None if service.get("updated") is None else time.time() - (_epoch(service["updated"]) or 0.0)
        running = bool(service["running"])
        stale = (not running) or age is None or age > STALE_AFTER
        saved_watches = (self._status or {}).get("watches") or {}
        entries, invalid = document["watches"], document["invalid_watches"]
        dirs = discovery.watches
        watches = [self._row(name, entries.get(name), saved_watches.get(name), invalid.get(name), dirs.get(name),
                             stale=stale, running=running)
                   for name in sorted(set(dirs) | set(entries) | set(invalid) | set(saved_watches))]
        watches += [self._orphan_row(name) for name in document["orphaned_state"]]
        now = time.time()
        for row in watches:
            self._sample_transfer(row.name, row.transfer, now)
        problems = [str(problem) for problem in document["problems"]]
        if self._discovery_error:
            problems.insert(0, f"global config error (showing the last good config): {_first_line(self._discovery_error)}")
        return Status(service=service, watches=watches, problems=problems, stale=stale,
                      stale_since=_epoch(service.get("updated")) if stale else None, follower_error=self._follower_error)

    def _row(
        self,
        name: str,
        entry: dict[str, Any] | None,
        saved: dict[str, Any] | None,
        invalid_error: Any,
        watch_dir: Path | None,
        *,
        stale: bool,
        running: bool,
    ) -> WatchRow:
        directory = str(watch_dir) if watch_dir is not None else None
        if invalid_error is not None:
            text = _first_line(invalid_error)
            return WatchRow(
                name=name, state="invalid", condition=None, failures=None, last_poll=None, next_poll=None,
                observers=None, pending_events=0, note=f"invalid: {text}", interval=None, description=None,
                watch_dir=directory, offline=None, config_error=text, in_flight=None, transfer=None,
            )
        merged = {**(saved or {}), **(entry or {})}
        if entry is None and saved:
            # the service's last snapshot knew a watch that is no longer on disk: never "online"
            merged = {**saved, "known_to_service": False, "exists": False}
        poll = self._believed(name, merged, stale=stale, running=running)
        condition, failures = merged.get("condition"), merged.get("failures")
        interval, description = merged.get("interval"), merged.get("description")
        merged_dir, config_error = merged.get("watch_dir"), merged.get("config_error")
        last_poll, next_poll = merged.get("last_poll"), merged.get("next_poll")
        offline, in_flight, activity = merged.get("offline"), merged.get("in_flight"), merged.get("transfer")
        return WatchRow(
            name=name,
            state=_state(merged, poll),
            condition=condition if isinstance(condition, bool) else None,
            failures=failures if isinstance(failures, int) else None,
            last_poll=last_poll if isinstance(last_poll, dict) else None,
            next_poll=next_poll if isinstance(next_poll, str) else None,
            observers=_observers(merged),
            pending_events=_count(merged.get("pending_events")),
            note=_note(merged),
            # a duration keepwatch itself wrote into status.json, already guarded by the isinstance above
            interval=float(interval) if isinstance(interval, (int, float)) else None,  # pi-lens-ignore: unchecked-numeric-parse-python
            description=description if isinstance(description, str) else None,
            watch_dir=merged_dir if isinstance(merged_dir, str) else directory,
            offline=offline if isinstance(offline, dict) else None,
            config_error=config_error if isinstance(config_error, str) else None,
            in_flight=in_flight if isinstance(in_flight, dict) else None,
            transfer=activity if isinstance(activity, dict) else None,
        )

    def _orphan_row(self, name: str) -> WatchRow:
        return WatchRow(
            name=name, state="orphaned", condition=None, failures=None, last_poll=None, next_poll=None, observers=None,
            pending_events=0, note="state of a watch that no longer exists (keepwatch rename)", interval=None,
            description=None, watch_dir=None, offline=None, config_error=None, in_flight=None, transfer=None,
        )

    def _believed(self, name: str, entry: dict[str, Any], *, stale: bool, running: bool) -> InFlight | None:
        """An open poll is believed only while the service runs with fresh status, and never after a poll it
        already reported (a poll the service finished cannot still be running: this is what stops a killed
        manual `keepwatch poll` from lying forever)."""
        if stale or not running:
            return None
        with self._lock:
            poll = self._open.get(name)
        if poll is None:
            return None
        last = entry.get("last_poll")
        ended = _epoch(last.get("at")) if isinstance(last, dict) else None
        if ended is not None and ended >= poll.started:
            return None
        return poll

    def in_flight(self) -> dict[str, InFlight]:
        """The polls the screen may claim are running right now."""
        flights = {}
        for row in self.status().watches:
            if row.state != "polling":
                continue
            with self._lock:
                poll = self._open.get(row.name)
            if poll is not None:
                flights[row.name] = poll
        return flights

    def hooks_done(self, watch: str, poll_id: str) -> list[str]:
        """The hooks of an in-flight poll that have already finished, in log order."""
        with self._lock:
            records = list(self._by_watch.get(watch, ()))
        return [str(record.get("hook")) for record in records
                if record.get("event") == "hook.end" and str(record.get("poll_id") or "") == poll_id]

    # -- records -------------------------------------------------------------

    # -- the transfer a hook is running right now (see transfer.read_activity) --------

    def _sample_transfer(self, name: str, activity: dict[str, Any] | None, now: float) -> None:
        """Remember two samples of a partial file, so the header can show a rate and an ETA."""
        part_size = activity.get("part_size") if isinstance(activity, dict) else None
        if not isinstance(part_size, int):
            with self._lock:
                self._samples.pop(name, None)
                self._rates.pop(name, None)
            return
        with self._lock:
            previous = self._samples.get(name)
            self._samples[name] = (now, part_size)
            if previous is not None and now - previous[0] >= 0.2 and part_size >= previous[1]:
                self._rates[name] = (part_size - previous[1]) / (now - previous[0])

    def transfer_rate(self, name: str) -> float | None:
        """Bytes a second for this watch's transfer, from the last two samples (None before there are two)."""
        with self._lock:
            return self._rates.get(name)

    def records(self, watch: str | None = None, *, verbose: bool = False) -> list[str]:
        """The recent records, oldest first: one watch's, or every watch's."""
        with self._lock:
            chosen = list(self._by_watch.get(watch, ())) if watch is not None else list(self._all)
        return [format_record(record, verbose=verbose) for record in chosen]

    def version(self) -> int:
        """Bumped on every record that arrives; the App redraws only when it changes."""
        with self._lock:
            return self._version


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _whole(seconds: float) -> int:
    """Whole seconds for display, never negative (a backwards clock must not print "-3s")."""
    return max(int(seconds), 0)  # pi-lens-ignore: unchecked-numeric-parse-python -- a float this module computed


def _state(entry: dict[str, Any], poll: InFlight | None) -> str:
    if entry.get("offline"):
        return "offline"
    enabled = entry.get("enabled")
    if isinstance(enabled, bool) and not enabled:
        return "parked"
    if entry.get("in_flight") or poll is not None:
        return "polling"
    known = entry.get("known_to_service")
    if isinstance(known, bool) and not known:
        return "idle"
    return "online"


def _note(entry: dict[str, Any]) -> str:
    if entry.get("config_error"):
        return f"config error: {_first_line(entry['config_error'])}"
    exists = entry.get("exists")
    if isinstance(exists, bool) and not exists:
        return "missing directory"
    pending = _count(entry.get("pending_events"))
    if pending > 0:
        return f"+{pending} events"
    offline = entry.get("offline")
    if isinstance(offline, dict) and offline.get("by_user"):
        return "offline by user"
    return ""


def _observers(entry: dict[str, Any]) -> tuple[int, int] | None:
    status = entry.get("observers")
    if not isinstance(status, dict) or not status:
        return None
    running = sum(1 for item in status.values() if isinstance(item, dict) and item.get("running"))
    return running, len(status)


# --- the text rules (here, so the App only renders and tests need no terminal) ---


def state_style(state: str) -> str:
    return _STATE_STYLES.get(state, "")


def condition_text(row: WatchRow) -> str:
    if row.condition is None:
        return "—"
    return "TRUE" if row.condition else "FALSE"


def observers_text(row: WatchRow) -> str:
    return "—" if row.observers is None else f"{row.observers[0]}/{row.observers[1]}"


def last_text(row: WatchRow, now: float) -> str:
    last = row.last_poll
    if not isinstance(last, dict):
        return "—"
    outcome = str(last.get("outcome") or "?")
    if last.get("failed"):
        outcome = f"FAILED {outcome}"
    at = _epoch(last.get("at"))
    age = "?" if at is None else format_duration(_whole(now - at))
    return f"{outcome} {age} ago"


def next_text(row: WatchRow, now: float, *, stale: bool) -> str:
    """A countdown only means something with a live service (and a watch that polls by itself)."""
    if stale or row.state in ("parked", "invalid", "orphaned") or row.next_poll is None:
        return "—"
    at = _epoch(row.next_poll)
    if at is None:
        return "—"
    seconds = at - now
    if seconds <= 0:
        return "due"
    text = format_duration(_whole(seconds))
    return f"retry {text}" if row.state == "offline" else text


def note_text(row: WatchRow) -> str:
    return row.note


def transfer_line(activity: dict[str, Any], rate: float | None = None) -> str:
    """The transfer part of the header: what is moving, how far, and (with two samples) how fast."""
    text = transfer.activity_text(activity)
    if isinstance(rate, float) and rate > 0:
        text += f", {transfer.format_bytes(int(rate))}/s"  # pi-lens-ignore: unchecked-numeric-parse-python -- our own float
    return text


def pane_header_text(
    row: WatchRow | None,
    *,
    in_flight: dict[str, Any] | None = None,
    poll: InFlight | None = None,
    transfer_now: dict[str, Any] | None = None,
    rate: float | None = None,
    hooks: list[str] | None = None,
    now: float = 0.0,
    paused_new: int = 0,
) -> str:
    if row is None:
        text = "all watches"
    else:
        parts = [row.name]
        if row.interval:
            parts.append(f"every {format_duration(row.interval)}")
        if (observers := observers_text(row)) != "—":
            parts.append(f"{observers} observers")
        if row.watch_dir:
            parts.append(row.watch_dir)
        text = " · ".join(parts)
    if in_flight is not None:
        started = _epoch(in_flight.get("started_at"))
        running = "?" if started is None else format_duration(_whole(now - started))
        text += f" · {in_flight.get('target') or in_flight.get('hook')} running {running}"
    elif poll is not None:
        done = hooks or []
        suffix = f" — {len(done)} hook{'s' if len(done) != 1 else ''} done"
        if done:
            suffix += f" ({', '.join(done[:3])})"
        text += f" · {poll.poll_id} running {format_duration(_whole(now - poll.started))}{suffix}"
    if isinstance(transfer_now, dict):
        text += f" · {transfer_line(transfer_now, rate)}"
    if paused_new:
        text += f" · paused · +{paused_new} new"
    return text


def banner_text(status: Status, now: float) -> str:
    service = status.service
    age = _age_text(service.get("updated"), now)
    if service.get("running"):
        line = f"keepwatch · service running (pid {service.get('pid')}, {service.get('version')})"
        if age is None:
            line += " · status age unknown"
        else:
            line += f" · status {age} ago" + (" (stale)" if status.stale else "")
    else:
        line = "keepwatch · service NOT running"
        if age is not None:
            line += f" · last status {age} ago (stale)"
    if status.follower_error:
        line += f" · log follower stopped: {status.follower_error}"
    return line


def _age_text(value: Any, now: float) -> str | None:
    at = _epoch(value)
    return None if at is None else format_duration(_whole(now - at))
