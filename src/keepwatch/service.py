"""The long-running service: master tick, live config, watch runners, status file (spec sections 3, 11)."""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections.abc import Callable, Collection
from pathlib import Path

from keepwatch import __version__
from keepwatch.alerts import run_alert
from keepwatch.config import (
    CONFIG_NAME,
    ConfigError,
    GlobalConfig,
    discover_watches,
    load_global_config,
    load_watch_config,
)
from keepwatch.logstore import Sink, make_record
from keepwatch.offline import iso_time
from keepwatch.paths import Paths, write_json_atomic
from keepwatch.pollengine import PollEngine
from keepwatch.runner import DRAIN_GRACE, KILL_GRACE, Runner
from keepwatch.scheduler import WatchRunner

Signature = tuple[int, int] | None


def file_signature(path: Path) -> Signature:
    """(mtime_ns, size), or None if the file does not exist or cannot be read."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


class Service:
    def __init__(
        self,
        *,
        paths: Paths,
        config_path: Path,
        sink: Sink,
        only: Collection[str] = (),
        start_threads: bool = True,
        clock: Callable[[], float] = time.time,
        runner: Runner | None = None,
    ) -> None:
        self.paths = paths
        self.config_path = config_path
        self._sink = sink
        self._only = frozenset(only)
        self._start_threads = start_threads
        self._clock = clock
        self._runner = runner or Runner()
        self.global_config: GlobalConfig | None = None
        self.engine: PollEngine | None = None
        self.runners: dict[str, WatchRunner] = {}
        self._retired: list[WatchRunner] = []
        self._global_signature: Signature = None
        self._global_error: str | None = None
        self._watch_signatures: dict[str, tuple[Path, Signature]] = {}
        self._watch_errors: dict[str, str] = {}
        self._reported_problems: frozenset[str] = frozenset()
        self._started = clock()
        self._tick_error: str | None = None

    def start(self) -> None:
        """Load the global config (raises ConfigError if broken), log service.start, run the first tick."""
        self.paths.stop_request.unlink(missing_ok=True)
        self._global_signature = file_signature(self.config_path)
        self.global_config = load_global_config(self.config_path, self.paths)
        self.engine = PollEngine(
            runner=self._runner,
            paths=self.paths,
            global_config=self.global_config,
            sink=self._sink,
            pid=os.getpid(),
        )
        self._started = self._clock()
        self._sink(
            make_record(
                "service.start",
                version=__version__,
                config=str(self.config_path),
                watch_dirs=[str(path) for path in self.global_config.watch_dirs],
                only=sorted(self._only),
            )
        )
        self.tick()

    def run(self, stop: threading.Event) -> None:
        """start(); then tick every reload_interval until `stop` is set or a stop request arrives; then stop()."""
        self.start()
        try:
            next_tick = time.monotonic() + self.global_config.reload_interval
            while not stop.wait(1.0):
                if self._stop_requested():
                    break
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic() + self.global_config.reload_interval
        finally:
            self.stop()

    def _stop_requested(self) -> bool:
        """True (once) when `keepwatch stop` has written the stop request file."""
        try:
            self.paths.stop_request.unlink()
        except OSError:
            return False
        self._sink(make_record("service.stop_requested"))
        return True

    def tick(self) -> None:
        """One master tick. Never raises: a failure is logged once, and the next tick tries again."""
        try:
            self._tick()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self._tick_error:
                self._sink(make_record("service.error", level="ERROR", error=error, traceback=traceback.format_exc()))
            self._tick_error = error
        else:
            self._tick_error = None

    def _tick(self) -> None:
        now = self._clock()
        self._reload_global()
        discovery = discover_watches(self.global_config)
        problems = frozenset(str(problem) for problem in discovery.problems)
        for problem in sorted(problems - self._reported_problems):
            self._sink(make_record("config.error", level="ERROR", error=problem))
        self._reported_problems = problems
        wanted = {
            name: path
            for name, path in discovery.watches.items()
            if not self._only or name in self._only
        }
        for name, path in wanted.items():
            self._sync_watch(name, path)
        for name in sorted(set(self.runners) - set(wanted)):
            runner = self.runners.pop(name)
            runner.stop()
            self._retired.append(runner)
            self._watch_signatures.pop(name, None)
            self._sink(make_record("watch.removed", watch=name))
        for name in sorted(set(self._watch_errors) - set(wanted)):
            del self._watch_errors[name]
            self._watch_signatures.pop(name, None)
        self.write_status(now)

    def _reload_global(self) -> None:
        signature = file_signature(self.config_path)
        if signature == self._global_signature:
            return
        self._global_signature = signature
        try:
            config = load_global_config(self.config_path, self.paths)
        except ConfigError as exc:
            self._global_error = str(exc)
            self._sink(make_record("config.error", level="ERROR", path=str(self.config_path), error=str(exc)))
            return
        self.global_config = config
        self._global_error = None
        self.engine.set_global_config(config)
        self._watch_signatures.clear()
        self._sink(make_record("config.loaded", path=str(self.config_path)))

    def _sync_watch(self, name: str, path: Path) -> None:
        signature = (path, file_signature(path / CONFIG_NAME))
        if self._watch_signatures.get(name) == signature:
            return
        self._watch_signatures[name] = signature
        runner = self.runners.get(name)
        try:
            config = load_watch_config(path, self.global_config.defaults)
        except ConfigError as exc:
            error = str(exc)
            self._watch_errors[name] = error
            if runner is not None:
                runner.config_error = error
            self._sink(
                make_record(
                    "config.error",
                    level="ERROR",
                    watch=name,
                    path=str(path / CONFIG_NAME),
                    error=error,
                    running_previous=runner is not None,
                )
            )
            return
        self._watch_errors.pop(name, None)
        if runner is not None:
            runner.config_error = None
            if runner.config != config:
                runner.update_config(config)
                self._sink(make_record("watch.changed", watch=name))
            return
        runner = WatchRunner(
            config,
            engine=self.engine,
            paths=self.paths,
            sink=self._sink,
            alert=self._alert,
            clock=self._clock,
        )
        self.runners[name] = runner
        if self._start_threads:
            runner.start()
        self._sink(make_record("watch.added", watch=name, enabled=config.enabled))

    def _alert(self, event: str, watch: str, reason: str) -> None:
        config = self.global_config
        run_alert(
            config.alert_command,
            event=event,
            watch=watch,
            reason=reason,
            environment=config.environment,
            timeout=float(config.defaults.get("action_timeout", 60.0)),
            sink=self._sink,
            capture_bytes=config.log.capture_bytes,
        )

    def stop(self) -> None:
        """Stop every runner and its hooks: SIGTERM, wait, SIGKILL, wait; then write the final status."""
        runners = [*self.runners.values(), *self._retired]
        for runner in runners:
            runner.stop()
        self._runner.close()
        deadline = time.monotonic() + KILL_GRACE
        for runner in runners:
            runner.join(max(deadline - time.monotonic(), 0.0))
        self._runner.kill_all()
        deadline = time.monotonic() + DRAIN_GRACE + 3.0
        for runner in runners:
            runner.join(max(deadline - time.monotonic(), 0.0))
        self._sink(make_record("service.stop"))
        self.write_status(self._clock(), running=False)

    def write_status(self, now: float, *, running: bool = True) -> None:
        document = {
            "service": {
                "running": running,
                "pid": os.getpid(),
                "version": __version__,
                "started": iso_time(self._started),
                "updated": iso_time(now),
                "config": str(self.config_path),
                "config_error": self._global_error,
                "only": sorted(self._only),
            },
            "watches": {name: runner.snapshot(now) for name, runner in sorted(self.runners.items())},
            "invalid_watches": {
                name: error for name, error in sorted(self._watch_errors.items()) if name not in self.runners
            },
            "problems": sorted(self._reported_problems),
        }
        write_json_atomic(self.paths.status_file, document)
