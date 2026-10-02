"""One watch's schedule: when it is due, backoff, going offline and coming back (spec sections 7-8)."""

from __future__ import annotations

import functools
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from keepwatch.config import WatchConfig
from keepwatch.locks import hold_lock
from keepwatch.logstore import Sink, make_record
from keepwatch.observers import EventQueue, Observer, build_observer
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import Paths
from keepwatch.pollengine import PollEngine, PollReport
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE
from keepwatch.state import ANSWERS, initial_state, next_delay, should_go_offline

Alert = Callable[[str, str, str], None]
CRASH_RETRY = 60.0
OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0
DROP_REPORT_EVERY = 1000
WAKE_GAP = 1.0  # a woken poll starts at least this long after the previous poll ended: busy sources are batched


@dataclass(frozen=True)
class LastPoll:
    poll_id: str
    at: str
    outcome: str
    reason: str | None
    failed: bool
    trial: bool
    results: list[dict[str, Any]]


def _failure_summary(report: PollReport) -> str:
    for result in report.results:
        if not result.succeeded:
            return f"{result.hook} {result.status}: {result.reason}"
    return f"check {report.outcome.value}: {report.reason}"


class WatchRunner:
    """Owns one watch's state. poll_once() is synchronous; start() runs it on a thread."""

    def __init__(
        self,
        config: WatchConfig,
        *,
        engine: PollEngine,
        paths: Paths,
        sink: Sink,
        alert: Alert,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.name = config.name
        self._config = config
        self._engine = engine
        self._paths = paths
        self._sink = sink
        self._alert = alert
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: WatchConfig | None = None
        self.state = initial_state(config.initial_condition)
        self.next_due = clock()
        self.offline: OfflineMarker | None = read_offline(paths, self.name)
        self.last_trial: float | None = None
        self.last_poll: LastPoll | None = None
        self.config_error: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.events = EventQueue()
        self._wake_requested = False
        self._last_end: float | None = None
        self._hold_until = 0.0
        self._observers: dict[str, Observer] = {}
        self._observers_lock = threading.Lock()
        self._observer_key: Any = None

    @property
    def config(self) -> WatchConfig:
        return self._config

    def update_config(self, config: WatchConfig) -> None:
        """Use a new config from the next poll on. Safe to call from another thread."""
        with self._lock:
            self._pending = config
        self._wake.set()

    def add_event(self, event: dict[str, Any], *, wake: bool = True) -> None:
        """Queue an observer event for the next poll; with wake, poll as soon as the schedule allows. Thread-safe."""
        with self._lock:
            dropped = self.events.put(event)
            total = self.events.dropped
            if wake:
                self._wake_requested = True
        if dropped and (total == dropped or total // DROP_REPORT_EVERY > (total - dropped) // DROP_REPORT_EVERY):
            self._sink(
                make_record(
                    "observer.dropped",
                    level="WARNING",
                    watch=self.name,
                    observer=event.get("observer"),
                    dropped=total,
                    cap=self.events.cap,
                    max_bytes=self.events.max_bytes,
                )
            )
        if wake:
            self._wake.set()

    def _observers_wanted(self) -> Any:
        """What the running observers depend on, or None when none should run."""
        config = self._config
        if self._stop.is_set() or not config.enabled or self.offline is not None or not config.observers:
            return None
        environment = self._engine.global_config.environment
        return (config.observers, config.watch_dir, config.shell, config.environment, config.settings, environment)

    def sync_observers(self) -> None:
        """Start, stop or restart this watch's observers to match its config and state. Runner thread only."""
        self._apply_pending()
        key = self._observers_wanted()
        if key == self._observer_key:
            return
        self._stop_observers()
        # Recorded before starting: a failure below is logged once instead of retried every second.
        self._observer_key = key
        if key is None:
            return
        config = self._config
        for name, observer_config in config.observers.items():
            observer = build_observer(
                config,
                observer_config,
                deliver=functools.partial(self.add_event, wake=observer_config.wake),
                sink=self._sink,
                environment=self._engine.global_config.environment,
                data_dir=self._paths.watch_data_dir(self.name),
            )
            with self._observers_lock:
                self._observers[name] = observer
            observer.start()

    def _stop_observers(self) -> None:
        with self._observers_lock:
            running, self._observers = list(self._observers.values()), {}
        for observer in running:
            observer.stop()
        for observer in running:
            observer.join(OBSERVER_JOIN)

    def _apply_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            self._config = pending

    def _went_online(self, reason: str) -> None:
        self._sink(make_record("watch.online", watch=self.name, reason=reason))
        self._alert("online", self.name, reason)

    def _refresh_offline(self, now: float) -> None:
        marker = read_offline(self._paths, self.name)
        if self.offline is not None and marker is None:
            self.offline = None
            self.last_trial = None
            self.state = replace(self.state, failures=0)
            self.next_due = now
            self._went_online("enabled by user")
        elif self.offline is None and marker is not None:
            self.offline = marker
            self._sink(
                make_record(
                    "watch.offline",
                    level="WARNING" if marker.by_user else "CRITICAL",
                    watch=self.name,
                    reason=marker.reason,
                    by_user=marker.by_user,
                )
            )
        else:
            self.offline = marker

    def seconds_until_due(self, now: float) -> float | None:
        """Seconds until the next poll, 0 if due now, None if the watch will not poll by itself."""
        config = self._config
        if not config.enabled:
            return None
        if self.offline is not None:
            if self.offline.by_user or config.retry_after is None:
                return None
            base = self.last_trial if self.last_trial is not None else self.offline.since_epoch()
            return max(base + config.retry_after - now, 0.0)
        if self._wake_requested and self.state.failures == 0:
            earliest = now if self._last_end is None else self._last_end + WAKE_GAP
            return max(min(self.next_due, max(earliest, self._hold_until)) - now, 0.0)
        return max(self.next_due - now, 0.0)

    def poll_once(self, now: float) -> PollReport | None:
        """Poll if due (a trial poll when offline with retry_after). Returns None when not due."""
        self._apply_pending()
        self._refresh_offline(now)
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
        config = self._config
        with self._lock:
            self._wake_requested = False
            mark, events = self.events.pending()
        with hold_lock(self._paths.watch_lock(self.name)):
            # Checked under the lock: `keepwatch rename` moves the directory while holding it.
            if not config.watch_dir.is_dir():
                return None
            trial = self.offline is not None
            if trial:
                self.last_trial = now
            report = self._engine.poll(config, self.state, trial=trial, events=events)
        self.state = report.after
        if not report.failed and report.outcome in ANSWERS:
            with self._lock:
                self.events.ack(mark)
        finished = self._clock()
        self._last_end = finished
        self.last_poll = LastPoll(
            poll_id=report.poll_id,
            at=iso_time(finished),
            outcome=report.outcome.value,
            reason=report.reason,
            failed=report.failed,
            trial=trial,
            results=[{"hook": r.hook, "status": r.status, "reason": r.reason} for r in report.results],
        )
        if self._stop.is_set():
            return report
        current = read_offline(self._paths, self.name)
        if current is not None and current.by_user:
            # `keepwatch disable` ran during this poll: the user's marker wins, and the next
            # _refresh_offline picks it up.
            self.next_due = finished + next_delay(config.interval, self.state.failures)
            return report
        if trial:
            if not report.failed and report.outcome in ANSWERS:
                clear_offline(self._paths, self.name)
                self.offline = None
                self.last_trial = None
                self.next_due = finished + config.interval
                self._went_online("trial poll succeeded")
            return report
        if report.failed and should_go_offline(self.state.failures, config.max_failures):
            marker = OfflineMarker(
                reason=f"{self.state.failures} consecutive failed polls",
                since=iso_time(finished),
                last_failure=_failure_summary(report),
            )
            write_offline(self._paths, self.name, marker)
            self.offline = marker
            self._sink(
                make_record(
                    "watch.offline",
                    level="CRITICAL",
                    watch=self.name,
                    reason=marker.reason,
                    by_user=False,
                    last_failure=marker.last_failure,
                )
            )
            self._alert("offline", self.name, f"{marker.reason}; last: {marker.last_failure}")
        self.next_due = finished + next_delay(config.interval, self.state.failures)
        return report

    def snapshot(self, now: float) -> dict[str, Any]:
        """This watch's entry in status.json."""
        config = self._config
        wait = self.seconds_until_due(now)
        return {
            "description": config.description,
            "watch_dir": str(config.watch_dir),
            "enabled": config.enabled,
            "interval": config.interval,
            "condition": self.state.condition,
            "pending_edge": self.state.pending_edge.value if self.state.pending_edge else None,
            "failures": self.state.failures,
            "offline": self.offline.to_dict() if self.offline else None,
            "last_poll": asdict(self.last_poll) if self.last_poll else None,
            "next_poll": None if wait is None else iso_time(now + wait),
            "config_error": self.config_error,
            "pending_events": len(self.events),
            "observers": self._observer_status(),
        }

    def _observer_status(self) -> dict[str, Any]:
        with self._observers_lock:
            running = dict(self._observers)
        return {
            name: {
                "kind": observer.kind,
                "running": observer.running,
                "restarts": observer.restarts,
                "last_event": iso_time(observer.last_event) if observer.last_event is not None else None,
            }
            for name, observer in sorted(running.items())
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"keepwatch-{self.name}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.sync_observers()
                    report = self.poll_once(self._clock())
                except Exception as exc:
                    self._sink(
                        make_record(
                            "watch.crash",
                            level="ERROR",
                            watch=self.name,
                            error=f"{type(exc).__name__}: {exc}",
                            traceback=traceback.format_exc(),
                        )
                    )
                    self.next_due = self._hold_until = self._clock() + CRASH_RETRY
                    report = None
                if report is None:
                    wait = self.seconds_until_due(self._clock())
                    self._wake.wait(1.0 if wait is None else min(wait, 1.0))
                    self._wake.clear()
        finally:
            self._stop_observers()

    def stop(self) -> None:
        """Stop after the current poll, and stop the observers. Polls that end after this never change offline state."""
        self._stop.set()
        self._wake.set()
        with self._observers_lock:
            running = list(self._observers.values())
        for observer in running:
            observer.stop()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()
