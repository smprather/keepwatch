"""One watch's schedule: when it is due, backoff, going offline and coming back (spec sections 7-8)."""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from keepwatch.config import WatchConfig
from keepwatch.locks import hold_lock
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import Paths
from keepwatch.pollengine import PollEngine, PollReport
from keepwatch.state import ANSWERS, initial_state, next_delay, should_go_offline

Alert = Callable[[str, str, str], None]
CRASH_RETRY = 60.0


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

    @property
    def config(self) -> WatchConfig:
        return self._config

    def update_config(self, config: WatchConfig) -> None:
        """Use a new config from the next poll on. Safe to call from another thread."""
        with self._lock:
            self._pending = config
        self._wake.set()

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
        return max(self.next_due - now, 0.0)

    def poll_once(self, now: float) -> PollReport | None:
        """Poll if due (a trial poll when offline with retry_after). Returns None when not due."""
        self._apply_pending()
        self._refresh_offline(now)
        wait = self.seconds_until_due(now)
        if wait is None or wait > 0:
            return None
        config = self._config
        with hold_lock(self._paths.watch_lock(self.name)):
            # Checked under the lock: `keepwatch rename` moves the directory while holding it.
            if not config.watch_dir.is_dir():
                return None
            trial = self.offline is not None
            if trial:
                self.last_trial = now
            report = self._engine.poll(config, self.state, trial=trial)
        self.state = report.after
        finished = self._clock()
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
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"keepwatch-{self.name}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
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
                self.next_due = self._clock() + CRASH_RETRY
                report = None
            if report is None:
                wait = self.seconds_until_due(self._clock())
                self._wake.wait(1.0 if wait is None else min(wait, 1.0))
                self._wake.clear()

    def stop(self) -> None:
        """Stop after the current poll. Polls that end after this never change offline state."""
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()
