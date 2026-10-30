"""One watch's schedule: when it is due, backoff, going offline and coming back (spec sections 7-8)."""

from __future__ import annotations

import functools
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from watchglass.config import WatchConfig
from watchglass.locks import hold_lock
from watchglass.logstore import Sink, make_record
from watchglass.observers import EventQueue, Observer, build_observer, stop_all
from watchglass.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from watchglass.paths import Paths
from watchglass.pollengine import PollEngine, PollReport
from watchglass.rejections import AUTHENTICATION_FAILED, HOST_KEY_FAILED
from watchglass.state import ANSWERS, initial_state, next_delay, should_go_offline, should_park_for_rejection
from watchglass.transfer import read_activity

Alert = Callable[[str, str, str], None]
CRASH_RETRY = 60.0
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
            detail = result.reason
            lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
            if lines and (not detail or lines[-1] not in detail):
                detail = f"{detail}; {lines[-1]}" if detail else lines[-1]
            return f"{result.hook} {result.status}: {detail}"
    return f"check {report.outcome.value}: {report.reason}"


def trial_delay(retry_after: float, retry_base: float | None, failures: int) -> float:
    """How long before the next trial poll: an accordion that reaches `retry_after` and stays there.

    The k-th consecutive failed trial waits `min(retry_after, retry_base * 2^(k-1))` — the shape of the failure
    backoff above — so a blip is retried quickly and a long outage is probed rarely. A poll in which the check
    answered resets the failure count, and with it the accordion. Without `retry_base` the wait is `retry_after`
    flat, which is what watchglass did before.
    """
    if retry_base is None:
        return retry_after
    # The exponent is clamped before the power (see next_delay): an int power raises OverflowError past ~1024
    # doublings, and so does a float one, long before a real outage is over.
    return min(retry_after, retry_base * 2.0 ** min(max(failures, 1) - 1, 64))


class WatchRunner:
    """Owns one watch's state. poll_once() is synchronous; start() runs it on a thread."""
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
        self._in_flight: dict[str, Any] | None = None
        self._in_flight_lock = threading.Lock()
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

    def add_event(self, event: dict[str, Any], *, wake: bool = True) -> int:
        """Queue an observer event for the next poll; with wake, poll as soon as the schedule allows. Thread-safe.

        Returns how many of the oldest events had to be dropped to make room: a source that can re-report
        (the remote watcher's tree mode) reconnects instead of leaving a hole in what the check sees.
        """
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
        return dropped

    def _observers_wanted(self) -> Any:
        """What the running observers depend on, or None when none should run."""
        config = self._config
        if self._stop.is_set() or not config.enabled or self.offline is not None or not config.observers:
            return None
        global_config = self._engine.global_config
        environment_file = global_config.environment_file
        signature = None
        if environment_file is not None:
            try:
                info = environment_file.stat()
                signature = (info.st_mtime_ns, info.st_size, info.st_mode)
            except OSError:
                signature = None
        return (
            config.observers, config.watch_dir, config.shell, config.environment, config.settings,
            global_config.environment, environment_file, signature, config.max_auth_failures,
        )

    def _observer_rejection(self) -> tuple[str, str] | None:
        """The first observer whose ssh login (or command) was rejected, with the detail for offline.json."""
        with self._observers_lock:
            running = dict(self._observers)
        for name, observer in sorted(running.items()):
            reason = observer.permanent_reason
            if reason is not None:
                tail = observer.rejection_tail[-1] if observer.rejection_tail else reason
                return reason, f"observer {name}: {tail}"
        return None

    def sync_observers(self) -> None:
        """Start, stop or restart this watch's observers to match its config and state. Runner thread only."""
        self._apply_pending()
        if self.offline is None and self._config.max_auth_failures > 0:
            found = self._observer_rejection()
            if found is not None:
                # An observer's rejected credential is as permanent as a hook's: no restart, no trial polls.
                self._park(self._clock(), found[0], found[1])
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
                environment_file=self._engine.global_config.environment_file,
                data_dir=self._paths.watch_data_dir(self.name),
            )
            with self._observers_lock:
                self._observers[name] = observer
            observer.start()

    def _stop_observers(self) -> None:
        with self._observers_lock:
            running, self._observers = list(self._observers.values()), {}
        stop_all(running)

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
            self.state = replace(self.state, failures=0, rejections=0)
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
                    permanent=marker.permanent,
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
            if self.offline.by_user or self.offline.permanent or config.retry_after is None:
                return None
            base = self.last_trial if self.last_trial is not None else self.offline.since_epoch()
            return max(base + self._trial_delay(config) - now, 0.0)
        if self._wake_requested and self.state.failures == 0:
            earliest = now if self._last_end is None else self._last_end + WAKE_GAP
            return max(min(self.next_due, max(earliest, self._hold_until)) - now, 0.0)
        return max(self.next_due - now, 0.0)

    def _trial_delay(self, config: WatchConfig) -> float:
        """This watch's next trial-poll wait (see `trial_delay`)."""
        return trial_delay(float(config.retry_after or 0.0), config.retry_base, self.state.failures)

    def _park(self, finished: float, rejection: str | None, last_failure: str | None) -> None:
        """Write offline.json and say why: an ordinary failure limit, or a permanent rejection (no trial polls)."""
        if rejection is None:
            reason = f"{self.state.failures} consecutive failed polls"
        else:
            fix = {
                AUTHENTICATION_FAILED: "fix the credential",
                HOST_KEY_FAILED: "fix known_hosts",
            }.get(rejection, "fix it")
            reason = f"{rejection}; {fix}, then: watchglass enable {self.name}"
        marker = OfflineMarker(
            reason=reason,
            since=iso_time(finished),
            last_failure=last_failure,
            permanent=rejection is not None,
        )
        write_offline(self._paths, self.name, marker)
        self.offline = marker
        if marker.permanent:
            self.last_trial = None
        self._sink(
            make_record(
                "watch.offline",
                level="CRITICAL",
                watch=self.name,
                reason=marker.reason,
                by_user=False,
                permanent=marker.permanent,
                last_failure=marker.last_failure,
            )
        )
        self._alert("offline", self.name, f"{marker.reason}; last: {marker.last_failure}")

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
            # Checked under the lock: `watchglass rename` moves the directory while holding it.
            if not config.watch_dir.is_dir():
                return None
            trial = self.offline is not None
            if trial:
                self.last_trial = now
            report = self._engine.poll(config, self.state, trial=trial, events=events, on_hook=self._record_in_flight)
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
            # `watchglass disable` ran during this poll: the user's marker wins, and the next
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
            elif report.permanent_reason is not None and should_park_for_rejection(
                self.state.rejections, config.max_auth_failures
            ):
                # A trial poll discovered the credential is rejected: upgrade the marker so no further trial
                # poll runs (retrying cannot succeed, and each attempt is the damage).
                self._park(finished, report.permanent_reason, _failure_summary(report))
            return report
        rejection = report.permanent_reason if report.failed else None
        if rejection is not None and should_park_for_rejection(self.state.rejections, config.max_auth_failures):
            self._park(finished, rejection, _failure_summary(report))
        elif report.failed and should_go_offline(self.state.failures, config.max_failures):
            self._park(finished, None, _failure_summary(report))
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
            "rejections": self.state.rejections,
            "offline": self.offline.to_dict() if self.offline else None,
            "last_poll": asdict(self.last_poll) if self.last_poll else None,
            "next_poll": None if wait is None else iso_time(now + wait),
            "config_error": self.config_error,
            "pending_events": len(self.events),
            "observers": self._observer_status(),
            "in_flight": self._in_flight_snapshot(),
            "transfer": self._transfer_snapshot(),
        }

    def _record_in_flight(self, report: dict[str, Any] | None) -> None:
        """The engine says which hook is running now (None between hooks and after the poll)."""
        with self._in_flight_lock:
            self._in_flight = report

    def _in_flight_snapshot(self) -> dict[str, Any] | None:
        """The hook running right now, with the name the log uses for it."""
        with self._in_flight_lock:
            report = dict(self._in_flight) if self._in_flight else None
        if report is not None and self._config.recipe:
            report["target"] = f"recipe {self._config.recipe}:{report['hook']}"
        return report

    def _transfer_snapshot(self) -> dict[str, Any] | None:
        """What the running hook is transferring (its run dir says so; see transfer.read_activity)."""
        with self._in_flight_lock:
            if self._in_flight is None:
                return None
        return read_activity(self._paths.run_dir(self._engine.pid, self.name))

    def _observer_status(self) -> dict[str, Any]:
        with self._observers_lock:
            running = dict(self._observers)
        return {
            name: {
                "kind": observer.kind,
                "running": observer.running,
                "restarts": observer.restarts,
                "last_event": iso_time(observer.last_event) if observer.last_event is not None else None,
                "permanent_reason": observer.permanent_reason,
            }
            for name, observer in sorted(running.items())
        }

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=f"watchglass-{self.name}", daemon=True)
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
