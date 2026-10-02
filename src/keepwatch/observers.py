"""Observers: event sources the service keeps running for a watch (see `keepwatch docs observers`).

A command observer runs a long-lived program and turns each line it prints into an event. Each
observer runs on its own thread, restarts its source with backoff when it stops, and hands events
to a `deliver` callback.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any

from keepwatch import platform
from keepwatch.config import ObserverConfig, WatchConfig
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import iso_time
from keepwatch.platform import HookProcess
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, base_environment

Deliver = Callable[[dict[str, Any]], None]

BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
RESET_AFTER = 300.0
QUEUE_CAP = 10_000
MAX_LINE = 1024 * 1024
OUTPUT_RECORDS_PER_MINUTE = 20
STDERR_TAIL = 5
_SUPERVISE_STEP = 0.2


def parse_event(text: str) -> dict[str, Any] | None:
    """One line of a command observer's output as an event: a JSON object as-is, other text as {"line": text}.

    Returns None for blank lines and heartbeats ({"event": "heartbeat"}), which are not delivered.
    """
    if not text.strip():
        return None
    line = text.rstrip("\r\n")
    try:
        value = json.loads(line)
    except ValueError:
        return {"line": line}
    if not isinstance(value, dict):
        return {"line": line}
    if value.get("event") == "heartbeat":
        return None
    return value


class EventQueue:
    """Pending events for one watch, oldest first, at most `cap` (the oldest are dropped).

    Not thread-safe: the WatchRunner guards it with its lock.
    """

    def __init__(self, cap: int = QUEUE_CAP) -> None:
        self.cap = cap
        self.dropped = 0
        self._items: deque[tuple[int, dict[str, Any]]] = deque()
        self._next = 1

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: dict[str, Any]) -> bool:
        """Add an event; True if the oldest one was dropped to make room."""
        self._items.append((self._next, event))
        self._next += 1
        if len(self._items) > self.cap:
            self._items.popleft()
            self.dropped += 1
            return True
        return False

    def pending(self) -> tuple[int, list[dict[str, Any]]]:
        """(a mark for ack(), the pending events oldest first)."""
        return self._next - 1, [event for _, event in self._items]

    def ack(self, mark: int) -> None:
        """Remove the events up to `mark` (from pending()); later ones stay."""
        while self._items and self._items[0][0] <= mark:
            self._items.popleft()


def observer_environment(
    watch: WatchConfig, name: str, *, environment: Mapping[str, str], data_dir: Path
) -> dict[str, str]:
    """What an observer's program runs with: a hook's environment without the per-poll variables."""
    env = base_environment(environment, watch)
    env.update(
        {
            "KEEPWATCH_WATCH": watch.name,
            "KEEPWATCH_OBSERVER": name,
            "KEEPWATCH_WATCH_DIR": str(watch.watch_dir),
            "KEEPWATCH_DATA_DIR": str(data_dir),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


class Observer:
    """One observer's thread: runs the source, restarts it with backoff, and stops on request."""

    kind = ""

    def __init__(self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink) -> None:
        self.watch = watch
        self.config = config
        self.name = config.name
        self.restarts = 0
        self.last_event: float | None = None
        self._deliver = deliver
        self._sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _tag(self) -> dict[str, Any]:
        return {"watch": self.watch.name, "observer": self.name}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"keepwatch-{self.watch.name}-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the observer to stop; join() waits until it has."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def emit(self, event: dict[str, Any]) -> None:
        """Tag an event with this observer's name and arrival time, log it (DEBUG) and deliver it."""
        now = time.time()
        self.last_event = now
        self._sink(make_record("observer.event", level="DEBUG", **self._tag(), data=event))
        try:
            self._deliver({**event, "observer": self.name, "received": iso_time(now)})
        except Exception as exc:
            self._crash(exc)

    def _crash(self, exc: Exception) -> None:
        self._sink(
            make_record(
                "observer.crash",
                level="ERROR",
                **self._tag(),
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
        )

    def _stopped(self, started: float, *, exit_code: int | None, reason: str, stderr_tail: list[str]) -> None:
        self._sink(
            make_record(
                "observer.stopped",
                level="INFO" if reason == "stopped" else "WARNING",
                **self._tag(),
                exit_code=exit_code,
                reason=reason,
                duration=round(time.monotonic() - started, 3),
                stderr_tail=stderr_tail,
            )
        )

    def _loop(self) -> None:
        delay = BACKOFF_START
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                self._crash(exc)
            if self._stop.is_set():
                break
            if time.monotonic() - started >= RESET_AFTER:
                delay = BACKOFF_START
            self.restarts += 1
            self._sink(make_record("observer.restarting", **self._tag(), delay=delay))
            self._stop.wait(delay)
            delay = min(delay * 2, BACKOFF_CAP)

    def run_once(self) -> None:
        """Run the source until it ends or stop() is called, logging observer.started and observer.stopped."""
        raise NotImplementedError


class _OutputLimiter:
    """observer.output records: at most OUTPUT_RECORDS_PER_MINUTE a minute, then one record counting the rest."""

    def __init__(self, sink: Sink, tag: dict[str, Any]) -> None:
        self._sink = sink
        self._tag = tag
        self._lock = threading.Lock()
        self._window = time.monotonic()
        self._count = 0
        self._suppressed = 0

    def line(self, text: str, stream: str = "stderr") -> None:
        with self._lock:
            if time.monotonic() - self._window >= 60.0:
                self._flush_locked()
                self._window, self._count = time.monotonic(), 0
            if self._count >= OUTPUT_RECORDS_PER_MINUTE:
                self._suppressed += 1
                return
            self._count += 1
        self._sink(make_record("observer.output", **self._tag, stream=stream, text=text))

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._suppressed:
            self._sink(
                make_record(
                    "observer.output",
                    level="WARNING",
                    **self._tag,
                    stream="stderr",
                    text=f"{self._suppressed} more line(s) not logged (limit {OUTPUT_RECORDS_PER_MINUTE} a minute)",
                    suppressed=self._suppressed,
                )
            )
            self._suppressed = 0


def _write_and_close(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


class CommandObserver(Observer):
    """Runs `command` in its own process group / Job Object; each stdout line is an event."""

    kind = "command"

    def __init__(
        self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink, env: Mapping[str, str]
    ) -> None:
        super().__init__(watch, config, deliver=deliver, sink=sink)
        self._env = dict(env)
        self._process: HookProcess | None = None
        self._process_lock = threading.Lock()
        self._last_line = time.monotonic()
        self._output = _OutputLimiter(sink, self._tag())

    def argv(self) -> list[str]:
        command = self.config.command
        assert command is not None
        argv = command.to_argv(self.watch.shell)
        if command.argv is not None:
            argv = platform.command_argv(argv, self.watch.watch_dir)
        return argv

    def stop(self) -> None:
        super().stop()
        with self._process_lock:
            if self._process is not None:
                self._process.terminate()

    def run_once(self) -> None:
        argv = self.argv()
        started = time.monotonic()
        try:
            process = platform.start_process(
                argv,
                cwd=self.watch.watch_dir,
                env=self._env,
                stdin=subprocess.PIPE if self.config.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self._stopped(started, exit_code=None, reason=f"cannot start: {exc.strerror or exc}", stderr_tail=[])
            return
        with self._process_lock:
            self._process = process
        if self._stop.is_set():
            process.terminate()
        self._sink(make_record("observer.started", **self._tag(), kind=self.kind, argv=argv, pid=process.pid))
        self._last_line = time.monotonic()
        tail: deque[str] = deque(maxlen=STDERR_TAIL)
        threads = [
            threading.Thread(target=self._read_stdout, args=(process.popen.stdout,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(process.popen.stderr, tail), daemon=True),
        ]
        if self.config.stdin is not None:
            data = self.config.stdin.encode("utf-8")
            threads.append(threading.Thread(target=_write_and_close, args=(process.popen.stdin, data), daemon=True))
        for thread in threads:
            thread.start()
        reason = self._supervise(process)
        drain_until = time.monotonic() + DRAIN_GRACE
        for thread in threads:
            thread.join(max(drain_until - time.monotonic(), 0.0))
        if any(thread.is_alive() for thread in threads):
            # A leftover child still holds a pipe open: stop the whole tree.
            process.kill()
            for thread in threads:
                thread.join(DRAIN_GRACE)
        with self._process_lock:
            self._process = None
        process.close()
        if reason is None:
            reason = "stopped" if self._stop.is_set() else "exited"
        self._output.flush()
        self._stopped(started, exit_code=process.popen.returncode, reason=reason, stderr_tail=list(tail))

    def _supervise(self, process: HookProcess) -> str | None:
        """Wait for the process to end; end it on stop() or a heartbeat timeout. Returns why keepwatch ended it."""
        timeout = self.config.heartbeat_timeout
        reason: str | None = None
        kill_at: float | None = None
        while True:
            try:
                process.popen.wait(timeout=_SUPERVISE_STEP)
                return reason
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if kill_at is not None:
                if now >= kill_at:
                    process.kill()
                    kill_at = float("inf")
                continue
            if self._stop.is_set():
                reason = "stopped"
            elif timeout is not None and now - self._last_line > timeout:
                reason = f"no output for {format_duration(timeout)} (heartbeat_timeout)"
            if reason is not None:
                process.terminate()
                kill_at = now + KILL_GRACE

    def _read_stdout(self, stream: IO[bytes]) -> None:
        skipping = False
        try:
            while True:
                raw = stream.readline(MAX_LINE)
                if not raw:
                    return
                self._last_line = time.monotonic()
                complete = raw.endswith(b"\n")
                if skipping:
                    skipping = not complete
                    continue
                if not complete and len(raw) >= MAX_LINE:
                    skipping = True
                    self._output.line(f"dropped a line longer than {MAX_LINE} bytes", stream="stdout")
                    continue
                event = parse_event(raw.decode("utf-8", errors="replace"))
                if event is not None:
                    self.emit(event)
        finally:
            stream.close()

    def _read_stderr(self, stream: IO[bytes], tail: deque[str]) -> None:
        try:
            while True:
                raw = stream.readline(MAX_LINE)
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if text.strip():
                    tail.append(text)
                    self._output.line(text)
        finally:
            stream.close()


def build_observer(
    watch: WatchConfig,
    config: ObserverConfig,
    *,
    deliver: Deliver,
    sink: Sink,
    environment: Mapping[str, str],
    data_dir: Path,
) -> Observer:
    """The Observer for one [observe.<name>] table (not started)."""
    if config.kind == "command":
        with contextlib.suppress(OSError):
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = observer_environment(watch, config.name, environment=environment, data_dir=data_dir)
        return CommandObserver(watch, config, deliver=deliver, sink=sink, env=env)
    raise ValueError(f"unknown observer kind {config.kind!r}")
