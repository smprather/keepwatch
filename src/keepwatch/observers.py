"""Observers: event sources the service keeps running for a watch (see `keepwatch docs observers`).

A command observer runs a long-lived program and turns each line it prints into an event; a files
observer reports settled files in a local directory; a remote_files observer runs keepwatch's remote
watcher on another host over ssh. Each observer runs on its own thread, restarts its source with
backoff when it stops, and hands events to a `deliver` callback.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import json
import os
import re
import stat
import subprocess
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer as NativeObserver

from keepwatch import platform
from keepwatch.config import Command, ObserverConfig, WatchConfig
from keepwatch.ctx import Ledger, LedgerCorrupt, ledger_file
from keepwatch.durations import format_duration
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import iso_time
from keepwatch.platform import HookProcess
from keepwatch.protocol import clip
from keepwatch.remote import AUTO_PYTHON as AUTO_PYTHON
from keepwatch.remote import ssh_argv, watcher_source
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, base_environment
from keepwatch.transfer import sha256_file

Deliver = Callable[[dict[str, Any]], None]

BACKOFF_START = 5.0
BACKOFF_CAP = 300.0
RESET_AFTER = 300.0
QUEUE_CAP = 10_000
QUEUE_BYTES = 64 * 1024 * 1024
EVENT_LOG_BYTES = 4096  # observer.event records keep larger events only as clipped JSON text
MAX_LINE = 1024 * 1024
OUTPUT_RECORDS_PER_MINUTE = 20
STDERR_TAIL = 5
_SUPERVISE_STEP = 0.2
OBSERVER_JOIN = KILL_GRACE + DRAIN_GRACE + 1.0  # how long stopping observers may take

FILES_RESCAN = 30.0  # rescan this often even without notifications (network shares can miss them)
FILES_POLL = 2.0  # rescan this often when native notifications are unavailable
FILES_SETTLE_STEP = 1.0  # rescan this often while a file is settling
FILES_MIN_RESCAN = 0.5  # at most two scans a second, however many notifications arrive


def parse_event(text: str) -> dict[str, Any] | None:
    """One line of a command observer's output as an event: a JSON object as-is, other text as {"line": text}.
    So is a JSON object that cannot be encoded as UTF-8.

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
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return {"line": line}  # lone surrogates (\udcxx escapes) could not be logged or handed to hooks
    return value


def _event_size(event: dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))


class EventQueue:
    """Pending events for one watch, oldest first: at most `cap` events and `max_bytes` of JSON.

    The oldest are dropped to make room; the newest event is always kept. Not thread-safe: the
    WatchRunner guards it with its lock.
    """

    def __init__(self, cap: int = QUEUE_CAP, max_bytes: int = QUEUE_BYTES) -> None:
        self.cap = cap
        self.max_bytes = max_bytes
        self.bytes = 0
        self.dropped = 0
        self._items: deque[tuple[int, int, dict[str, Any]]] = deque()
        self._next = 1

    def __len__(self) -> int:
        return len(self._items)

    def put(self, event: dict[str, Any]) -> int:
        """Add an event; returns how many of the oldest were dropped to make room."""
        size = _event_size(event)
        self._items.append((self._next, size, event))
        self._next += 1
        self.bytes += size
        dropped = 0
        while len(self._items) > 1 and (len(self._items) > self.cap or self.bytes > self.max_bytes):
            _, old_size, _ = self._items.popleft()
            self.bytes -= old_size
            dropped += 1
        self.dropped += dropped
        return dropped

    def pending(self) -> tuple[int, list[dict[str, Any]]]:
        """(a mark for ack(), the pending events oldest first)."""
        return self._next - 1, [event for _, _, event in self._items]

    def ack(self, mark: int) -> None:
        """Remove the events up to `mark` (from pending()); later ones stay."""
        while self._items and self._items[0][0] <= mark:
            _, size, _ = self._items.popleft()
            self.bytes -= size


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
        if self._stop.is_set():
            return  # a source being stopped: its last lines are not delivered
        now = time.time()
        self.last_event = now
        text = json.dumps(event, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) <= EVENT_LOG_BYTES:
            fields: dict[str, Any] = {"data": event}
        else:
            fields = {"data_clipped": clip(text, EVENT_LOG_BYTES)[0]}
        self._sink(make_record("observer.event", level="DEBUG", **self._tag(), **fields))
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


def stop_all(running: Iterable[Observer], timeout: float = OBSERVER_JOIN) -> None:
    """Ask every observer to stop, then wait for all of them (they stop in parallel) up to `timeout`."""
    observers = list(running)
    for observer in observers:
        observer.stop()
    deadline = time.monotonic() + timeout
    for observer in observers:
        observer.join(max(deadline - time.monotonic(), 0.0))


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


class _Poke(FileSystemEventHandler):
    """Any filesystem change just asks for a rescan (reads do not: hashing a file must not trigger another scan)."""

    def __init__(self, changed: threading.Event) -> None:
        super().__init__()
        self._changed = changed

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in ("opened", "closed_no_write"):
            self._changed.set()


Version = tuple[int, int, tuple[int, int] | None]  # a file's (size, mtime_ns) and its marker's, if any


class FilesObserver(Observer):
    """Reports settled files in a local directory: native notifications (watchdog) trigger rescans."""

    kind = "files"

    def __init__(self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink) -> None:
        super().__init__(watch, config, deliver=deliver, sink=sink)
        self._reported: set[tuple[str, int, int]] = set()
        self._held: set[tuple[str, Version]] = set()  # settled without a matching marker

    def _ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.config.ignore)

    def _wanted(self, name: str) -> bool:
        if self.config.marker == "sha256" and name.endswith(".sha256"):
            return False
        return fnmatch.fnmatch(name, self.config.pattern) and not self._ignored(name)

    def _marker_version(self, path: str) -> tuple[int, int] | None:
        """The marker's (size, mtime_ns); None without marker = sha256 or while it is missing."""
        if self.config.marker != "sha256":
            return None
        try:
            info = os.stat(path + ".sha256")
        except OSError:
            return None
        return (info.st_size, info.st_mtime_ns)

    def _verified(self, path: str, marker: tuple[int, int] | None) -> dict[str, Any] | None:
        """{} without markers; with marker = sha256 the event fields, or None when the marker is missing or wrong.

        Raises OSError when the file or its marker cannot be read (it is checked again after another settle period).
        """
        if self.config.marker != "sha256":
            return {}
        if marker is None:
            return None
        marker_path = Path(path + ".sha256")
        fields = marker_path.read_text(encoding="utf-8", errors="replace").split()
        expected = fields[0].lower() if fields else ""
        actual = sha256_file(path)
        if re.fullmatch(r"[0-9a-f]{64}", expected) and actual == expected:
            return {"sha256": actual, "marker": str(marker_path)}
        self._sink(
            make_record(
                "observer.output",
                level="WARNING",
                **self._tag(),
                stream="marker",
                text=f"{Path(path).name}: {marker_path.name} does not match (sha256 {actual}); waiting for a new upload",
            )
        )
        return None

    def _scan(self) -> dict[str, os.stat_result]:
        """Regular files to consider, by absolute path. Raises OSError if the directory is gone."""
        root = self.config.path
        assert root is not None
        if not self.config.recursive:
            listing = [(root, os.listdir(root))]
        else:
            if not root.is_dir():
                raise FileNotFoundError(errno.ENOENT, "directory does not exist", str(root))
            listing = []
            for directory, subdirectories, files in os.walk(root):
                subdirectories[:] = [name for name in subdirectories if not self._ignored(name)]
                listing.append((Path(directory), files))
        found = {}
        for directory, entries in listing:
            for name in entries:
                if not self._wanted(name):
                    continue
                path = directory / name
                try:
                    info = path.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found[str(path)] = info
        return found

    def run_once(self) -> None:
        started = time.monotonic()
        root = self.config.path
        assert root is not None
        if not root.is_dir():
            self._stopped(started, exit_code=None, reason=f"directory {root} does not exist", stderr_tail=[])
            return
        changed = threading.Event()
        native: Any = NativeObserver()  # watchdog exposes Observer as a per-platform variable, not a class
        native_error = None
        try:
            native.schedule(_Poke(changed), str(root), recursive=self.config.recursive)
            native.start()
        except Exception as exc:  # OSError, or a platform-specific watchdog error
            native_error = f"{type(exc).__name__}: {exc}"
            native = None
        self._sink(
            make_record(
                "observer.started",
                level="INFO" if native_error is None else "WARNING",
                **self._tag(),
                kind=self.kind,
                path=str(root),
                native=native_error is None,
                native_error=native_error,
            )
        )
        try:
            reason = self._watch(changed, FILES_RESCAN if native_error is None else FILES_POLL)
        finally:
            if native is not None:
                native.stop()
                native.join(5.0)
        self._stopped(started, exit_code=None, reason=reason, stderr_tail=[])

    def _watch(self, changed: threading.Event, idle: float) -> str:
        """Scan, report settled files, wait for a notification or a timeout; repeat until stopped."""
        settle = self.config.settle
        candidates: dict[str, tuple[Version, float]] = {}
        last_scan = 0.0
        while not self._stop.is_set():
            self._stop.wait(max(last_scan + FILES_MIN_RESCAN - time.monotonic(), 0.0))
            if self._stop.is_set():
                break
            changed.clear()
            last_scan = time.monotonic()
            try:
                found = self._scan()
            except OSError as exc:
                return f"cannot scan {self.config.path}: {exc.strerror or exc}"
            now, wall = time.monotonic(), time.time()
            current, versions = set(), set()
            for path, info in sorted(found.items()):
                key = (info.st_size, info.st_mtime_ns)
                current.add((path, *key))
                if (path, *key) in self._reported:
                    continue
                marker = self._marker_version(path)
                version = (*key, marker)  # a marker arriving or changing starts a new settle period
                versions.add((path, version))
                if (path, version) in self._held:
                    continue  # no matching marker: checked again when the file or its marker changes
                seen = candidates.get(path)
                if seen is None or seen[0] != version:
                    seen = candidates[path] = (version, now)
                newest = max(info.st_mtime_ns, marker[1] if marker else 0) / 1e9
                if now - seen[1] < settle or wall - newest < settle:
                    continue
                del candidates[path]
                try:
                    extra = self._verified(path, marker)
                except OSError:
                    candidates[path] = (version, now)  # unreadable for now (locked on Windows): again after settle
                    continue
                if extra is None:
                    self._held.add((path, version))
                    continue
                self._reported.add((path, *key))
                self.emit(
                    {
                        "event": "file",
                        "path": path,
                        "name": Path(path).name,
                        "size": info.st_size,
                        "mtime": info.st_mtime,
                        **extra,
                    }
                )
            for path in set(candidates) - set(found):
                del candidates[path]
            self._reported &= current
            self._held &= versions
            deadline = time.monotonic() + (FILES_SETTLE_STEP if candidates else idle)
            while not self._stop.is_set() and not changed.is_set() and time.monotonic() < deadline:
                changed.wait(min(0.2, max(deadline - time.monotonic(), 0.0)))
        return "stopped"


def remote_command(config: ObserverConfig) -> list[str]:
    """The ssh command line that runs the remote watcher. User ssh_options come first: ssh keeps the first value."""
    assert config.remote is not None
    return ssh_argv(
        config.remote,
        port=config.port,
        identity=config.identity,
        ssh_options=config.ssh_options,
        ssh_command=config.ssh_command,
        remote_python=config.remote_python,
    )


def watcher_options(config: ObserverConfig) -> dict[str, Any]:
    return {
        "dir": config.dir,
        "pattern": config.pattern,
        "ignore": list(config.ignore),
        "settle": config.settle,
        "interval": config.interval,
        "checksum": config.checksum,
        "heartbeat": config.heartbeat,
        "rescan": FILES_RESCAN,
    }


class RemoteFilesObserver(CommandObserver):
    """A command observer running the remote watcher over ssh; only file events are delivered."""

    kind = "remote_files"

    def __init__(
        self,
        watch: WatchConfig,
        config: ObserverConfig,
        *,
        deliver: Deliver,
        sink: Sink,
        env: Mapping[str, str],
        data_dir: Path | None = None,
    ) -> None:
        timeout = config.heartbeat_timeout if config.heartbeat_timeout is not None else 3 * config.heartbeat
        self._options = watcher_options(config)
        self._skip_ledger = (
            None if config.skip_ledger is None or data_dir is None else ledger_file(data_dir, config.skip_ledger)
        )
        command_config = replace(
            config,
            command=Command(argv=tuple(remote_command(config))),
            stdin=watcher_source(self._options),
            heartbeat_timeout=timeout,
        )
        super().__init__(watch, command_config, deliver=deliver, sink=sink, env=env)

    def run_once(self) -> None:
        if self._skip_ledger is not None:
            # Read at every (re)connect: files handled since the last connection are skipped too.
            self.config = replace(self.config, stdin=watcher_source({**self._options, "skip": self._skipped()}))
        super().run_once()

    def _skipped(self) -> list[str]:
        assert self._skip_ledger is not None
        try:
            return list(Ledger(self._skip_ledger, writable=False))
        except LedgerCorrupt as exc:
            self._output.line(str(exc))
            return []

    def emit(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "hello":
            self._sink(
                make_record(
                    "observer.connected",
                    **self._tag(),
                    remote=self.config.remote,
                    dir=event.get("dir"),
                    python=event.get("python"),
                    version=event.get("version"),
                    inotify=event.get("inotify"),
                )
            )
            return
        if kind != "file":
            # Anything else on stdout (a login script's banner, say) is logged, never delivered.
            text = event["line"] if set(event) == {"line"} else json.dumps(event, ensure_ascii=False)
            self._output.line(text, stream="stdout")
            return
        super().emit({**event, "remote": self.config.remote})


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
    if config.kind in ("command", "remote_files"):
        with contextlib.suppress(OSError):
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = observer_environment(watch, config.name, environment=environment, data_dir=data_dir)
        if config.kind == "command":
            return CommandObserver(watch, config, deliver=deliver, sink=sink, env=env)
        return RemoteFilesObserver(watch, config, deliver=deliver, sink=sink, env=env, data_dir=data_dir)
    if config.kind == "files":
        return FilesObserver(watch, config, deliver=deliver, sink=sink)
    raise ValueError(f"unknown observer kind {config.kind!r}")
