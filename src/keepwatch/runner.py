"""Run one hook call in a child process: a Python worker or a command.

Every call gets its own process group (POSIX) or Job Object (Windows). At the deadline the whole tree is
stopped: SIGTERM, then SIGKILL after kill_grace seconds on POSIX; at once on Windows. stdout and stderr are
captured (head and tail kept); everything is returned as a HookResult.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from keepwatch import __version__, platform
from keepwatch.config import Command, ExitCodes, WatchConfig
from keepwatch.durations import format_duration
from keepwatch.hooks import CHECK
from keepwatch.platform import HookProcess
from keepwatch.protocol import PROTOCOL_VERSION, decode_lines, normalize_payload
from keepwatch.state import Outcome

KILL_GRACE = 5.0
DRAIN_GRACE = 2.0
RESULT_LIMIT = 16 * 1024 * 1024
FAILED_STATUSES = frozenset({"error", "failed", "timeout"})
_NO_UV = "python_dependencies needs uv on PATH (https://docs.astral.sh/uv/); install it or remove python_dependencies"
_EXPECTED_VERSION = __version__


@dataclass(frozen=True)
class HookCall:
    watch: WatchConfig
    hook: str
    poll_id: str
    condition: bool
    payload: Any
    data_dir: Path
    run_dir: Path
    timeout: float
    capture_bytes: int = 65_536
    environment: Mapping[str, str] = field(default_factory=dict)
    mode: str = "call"
    on_message: Callable[[dict[str, Any]], None] | None = field(default=None, compare=False)


@dataclass
class HookResult:
    hook: str
    kind: str
    target: str
    status: str
    payload: Any = None
    reason: str | None = None
    exit_code: int | None = None
    signal: int | None = None
    exception: dict[str, str] | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[str] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"

    def outcome(self) -> Outcome:
        return Outcome(self.status)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        del record["messages"]
        del record["hooks"]
        return record


class _Reader(threading.Thread):
    """Drains one pipe. With a limit, keeps only the first and last limit/2 bytes."""

    def __init__(self, stream: IO[bytes], limit: int | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self) -> None:
        fd = self._stream.fileno()
        try:
            while chunk := os.read(fd, 65536):
                self.total += len(chunk)
                if self._limit is None:
                    self.head += chunk
                    continue
                half = max(self._limit // 2, 1)
                room = half - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > half:
                        del self.tail[:-half]
        finally:
            self._stream.close()

    def raw(self) -> bytes:
        return bytes(self.head) + bytes(self.tail)

    def text(self) -> tuple[str, bool]:
        omitted = self.total - len(self.head) - len(self.tail)
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if omitted <= 0:
            return (bytes(self.head) + bytes(self.tail)).decode("utf-8", errors="replace"), False
        return f"{head}\n…[{omitted} bytes omitted]…\n{tail}", True


class _MessageReader(threading.Thread):
    """Reads the worker's JSON-line messages as they arrive.

    hello and result are kept. log and command messages are delivered (to
    on_message, or kept in .messages without one) until `limit` bytes of them
    have been delivered; later ones are counted in dropped_bytes. A line longer
    than `limit` is skipped without being buffered whole.
    """

    def __init__(self, stream: IO[bytes], limit: int, on_message: Callable[[dict[str, Any]], None] | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._on_message = on_message
        self.hello: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.messages: list[dict[str, Any]] = []
        self.bad = 0
        self.delivered_bytes = 0
        self.dropped_bytes = 0

    def run(self) -> None:
        fd = self._stream.fileno()
        buffer = b""
        skipping = False
        try:
            while chunk := os.read(fd, 65536):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if skipping:
                        self.dropped_bytes += len(line)
                        skipping = False
                        continue
                    self._line(line)
                if len(buffer) > self._limit:
                    self.dropped_bytes += len(buffer)
                    buffer = b""
                    skipping = True
            if buffer and not skipping:
                self._line(buffer)
        finally:
            self._stream.close()

    def _line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        messages, bad = decode_lines(raw + b"\n")
        self.bad += len(bad)
        for message in messages:
            kind = message.get("type")
            if kind == "hello":
                self.hello = message
            elif kind == "result":
                self.result = message
            elif kind in ("log", "command"):
                if self.delivered_bytes + len(raw) > self._limit:
                    self.dropped_bytes += len(raw)
                    continue
                self.delivered_bytes += len(raw)
                self.deliver(message)
            else:
                self.bad += 1

    def deliver(self, message: dict[str, Any]) -> None:
        if self._on_message is None:
            self.messages.append(message)
            return
        try:
            self._on_message(message)
        except Exception:
            self.messages.append(message)


class _Writer(threading.Thread):
    def __init__(self, fd: int, data: bytes) -> None:
        super().__init__(daemon=True)
        self._fd = fd
        self._data = data

    def run(self) -> None:
        try:
            with os.fdopen(self._fd, "wb") as handle:
                handle.write(self._data)
        except BrokenPipeError:
            pass


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


def classify_exit(code: int, codes: ExitCodes) -> str:
    """Map a command check's exit code to an outcome value via [check_exit_codes]."""
    if code in codes.true:
        return "true"
    if code in codes.false:
        return "false"
    if code in codes.unknown:
        return "unknown"
    return "error"


def setting_variables(settings: Mapping[str, Any]) -> dict[str, str]:
    """KEEPWATCH_SETTING_<KEY> for each top-level scalar setting."""
    variables = {}
    for key, value in settings.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            text = str(value)
        else:
            continue
        variables[f"KEEPWATCH_SETTING_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}"] = text
    return variables


def make_shim(directory: Path) -> Path:
    """A PYTHONPATH directory exposing only the running keepwatch package (for uv environments)."""
    import keepwatch

    target = Path(keepwatch.__file__).resolve().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = directory / "keepwatch"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return directory
    temp = directory / f".keepwatch-{os.getpid()}-{threading.get_ident()}"
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, link)  # atomic: concurrent callers never see a missing or half-made link
    return directory


@dataclass
class _WorkerRun:
    returncode: int
    timed_out: bool
    out: _Reader
    err: _Reader
    messages: _MessageReader
    process: HookProcess


class Runner:
    def __init__(self, *, python: str = sys.executable, kill_grace: float = KILL_GRACE, uv: str | None = None) -> None:
        self.python = python
        self.kill_grace = kill_grace
        self.uv = uv or shutil.which("uv")
        self._active: set[HookProcess] = set()
        self._active_lock = threading.Lock()
        self._closing = False

    def terminate_all(self) -> None:
        """Stop every running hook gracefully (POSIX SIGTERM; immediately on Windows)."""
        with self._active_lock:
            processes = list(self._active)
        for process in processes:
            process.terminate()

    def close(self) -> None:
        """Shutdown, step 1: refuse new hook calls and SIGTERM the running ones."""
        with self._active_lock:
            self._closing = True
        self.terminate_all()

    def kill_all(self) -> None:
        """Shutdown, step 2: forcibly stop every hook still running."""
        with self._active_lock:
            processes = list(self._active)
        for process in processes:
            process.kill()

    def _track(self, process: HookProcess) -> None:
        with self._active_lock:
            self._active.add(process)
            closing = self._closing
        if closing:
            # Started while close() ran: stop it right away.
            process.terminate()

    def _untrack(self, process: HookProcess) -> None:
        with self._active_lock:
            self._active.discard(process)
        process.close()

    def run(self, call: HookCall) -> HookResult:
        """Run one hook call. Never raises: any unexpected failure becomes a failed result."""
        started = time.monotonic()
        is_command = call.mode == "call" and call.hook in call.watch.hooks
        if self._closing:
            return self._error(call, "command" if is_command else "python", call.hook, started,
                               "keepwatch is shutting down")
        try:
            if is_command:
                return self._run_command(call, call.watch.hooks[call.hook])
            return self._run_python(call)
        except Exception as exc:
            result = self._error(
                call,
                "command" if is_command else "python",
                call.hook,
                started,
                f"keepwatch internal error: {type(exc).__name__}: {exc}",
            )
            result.exception = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            return result

    # -- shared -------------------------------------------------------------

    def _failure_status(self, call: HookCall) -> str:
        return "error" if call.hook == CHECK or call.mode == "describe" else "failed"

    def _error(self, call: HookCall, kind: str, target: str, started: float, reason: str) -> HookResult:
        return HookResult(
            hook=call.hook,
            kind=kind,
            target=target,
            status=self._failure_status(call),
            reason=reason,
            duration=time.monotonic() - started,
        )

    def _environment(self, call: HookCall) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("KEEPWATCH_") or key == "KEEPWATCH_CONFIG"
        }
        env.update(call.environment)
        env.update(call.watch.environment)
        env.update(setting_variables(call.watch.settings))
        env.update(
            {
                "KEEPWATCH_WATCH": call.watch.name,
                "KEEPWATCH_HOOK": call.hook,
                "KEEPWATCH_POLL_ID": call.poll_id,
                "KEEPWATCH_CONDITION": "true" if call.condition else "false",
                "KEEPWATCH_WATCH_DIR": str(call.watch.watch_dir),
                "KEEPWATCH_DATA_DIR": str(call.data_dir),
                "KEEPWATCH_RUN_DIR": str(call.run_dir),
            }
        )
        return env

    def _supervise(self, process: HookProcess, readers: Sequence[threading.Thread], timeout: float) -> tuple[int, bool]:
        timed_out = False
        try:
            process.popen.wait(timeout=max(timeout, 0.0))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.popen.wait(timeout=self.kill_grace)
            except subprocess.TimeoutExpired:
                pass
            process.kill()
            process.popen.wait()
        drain_until = time.monotonic() + DRAIN_GRACE
        for reader in readers:
            reader.join(max(drain_until - time.monotonic(), 0.0))
        if any(reader.is_alive() for reader in readers):
            # A leftover child still holds a pipe open: stop the whole tree.
            process.kill()
            for reader in readers:
                reader.join(DRAIN_GRACE)
        return process.popen.returncode, timed_out

    def _captured(self, out: _Reader, err: _Reader, returncode: int, started: float) -> dict[str, Any]:
        stdout, stdout_cut = out.text()
        stderr, stderr_cut = err.text()
        return {
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_cut,
            "stderr_truncated": stderr_cut,
            "exit_code": returncode if returncode >= 0 else None,
            "signal": -returncode if returncode < 0 else None,
            "duration": time.monotonic() - started,
        }

    @staticmethod
    def _stopped_reason(process: HookProcess, returncode: int) -> str | None:
        """Why a process ended abnormally because of a signal or keepwatch, or None."""
        if returncode < 0:
            return f"killed by signal {_signal_name(-returncode)}"
        if platform.IS_WINDOWS and process.terminated and returncode == platform.TERMINATED_EXIT:
            return "terminated by keepwatch"
        return None

    # -- Python hooks ---------------------------------------------------------

    def _uv_prefix(self, dependencies: Sequence[str], *, offline: bool) -> list[str]:
        argv = [self.uv or "uv", "run", "--no-project", "--quiet", "--python", self.python]
        if offline:
            argv.append("--offline")
        for dependency in dependencies:
            argv += ["--with", dependency]
        return argv

    def prepare_environment(self, watch: WatchConfig, timeout: float = 600.0) -> str | None:
        """Build (or reuse) the uv environment for python_dependencies. Returns a problem, or None."""
        if not watch.python_dependencies:
            return None
        if not self.uv:
            return _NO_UV
        argv = [*self._uv_prefix(watch.python_dependencies, offline=False), "python", "-c", "pass"]
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"building the environment for python_dependencies timed out after {format_duration(timeout)}"
        except OSError as exc:
            return f"cannot run uv ({self.uv}): {exc}"
        if completed.returncode != 0:
            tail = " | ".join(completed.stderr.strip().splitlines()[-5:])
            return f"uv could not build the environment for python_dependencies: {tail}"
        return None

    def _start_worker(
        self,
        call: HookCall,
        request: dict[str, Any],
        env: dict[str, str],
        *,
        offline: bool,
        timeout: float,
    ) -> _WorkerRun | str:
        if self._closing:
            return "keepwatch is shutting down"
        req_r, req_w = os.pipe()
        res_r, res_w = os.pipe()
        tail = ["-m", "keepwatch.worker", *platform.pipe_arguments(req_r, res_w)]
        if call.watch.python_dependencies:
            argv = [*self._uv_prefix(call.watch.python_dependencies, offline=offline), "python", *tail]
        else:
            argv = [self.python, *tail]
        try:
            process = platform.start_process(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=call.watch.watch_dir,
                env=env,
                inherit=(req_r, res_w),
            )
        except OSError as exc:
            for fd in (req_r, req_w, res_r, res_w):
                os.close(fd)
            return f"cannot start the Python worker ({argv[0]}): {exc}"
        os.close(req_r)
        os.close(res_w)
        _Writer(req_w, json.dumps(request).encode("utf-8")).start()
        out = _Reader(process.popen.stdout, call.capture_bytes)
        err = _Reader(process.popen.stderr, call.capture_bytes)
        messages = _MessageReader(os.fdopen(res_r, "rb"), RESULT_LIMIT, call.on_message)
        for reader in (out, err, messages):
            reader.start()
        self._track(process)
        try:
            returncode, timed_out = self._supervise(process, (out, err, messages), timeout)
        finally:
            self._untrack(process)
        return _WorkerRun(returncode, timed_out, out, err, messages, process)

    def _run_python(self, call: HookCall) -> HookResult:
        target = f"watch.py:{call.hook}" if call.mode == "call" else "watch.py (describe)"
        started = time.monotonic()
        dependencies = call.watch.python_dependencies
        if dependencies and not self.uv:
            return self._error(call, "python", target, started, _NO_UV)
        deadline = time.time() + call.timeout
        request = {
            "watch": call.watch.name,
            "hook": call.hook,
            "watch_dir": str(call.watch.watch_dir),
            "data_dir": str(call.data_dir),
            "run_dir": str(call.run_dir),
            "poll_id": call.poll_id,
            "condition": call.condition,
            "payload": call.payload,
            "settings": dict(call.watch.settings),
            "deadline": deadline,
            "capture_bytes": call.capture_bytes,
            "mode": call.mode,
        }
        env = self._environment(call)
        env["PYTHONUNBUFFERED"] = "1"
        if dependencies:
            shim = make_shim(call.run_dir.parent / "lib")
            env["PYTHONPATH"] = os.pathsep.join(part for part in (str(shim), env.get("PYTHONPATH", "")) if part)
        run: _WorkerRun | None = None
        for offline in (True, False) if dependencies else (False,):
            attempt = self._start_worker(call, request, env, offline=offline, timeout=max(deadline - time.time(), 0.0))
            if isinstance(attempt, str):
                return self._error(call, "python", target, started, attempt)
            run = attempt
            # Offline first: when the environment is not cached yet, uv fails before the worker says hello.
            if not (offline and run.messages.hello is None and not run.timed_out):
                break
        assert run is not None
        return self._worker_result(call, target, started, run)

    def _worker_result(self, call: HookCall, target: str, started: float, run: _WorkerRun) -> HookResult:
        results = run.messages
        base = self._captured(run.out, run.err, run.returncode, started)
        if results.bad:
            results.deliver(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": f"ignored {results.bad} malformed message(s) from the worker",
                    "fields": {},
                }
            )
        if results.dropped_bytes:
            results.deliver(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": (
                        f"dropped {results.dropped_bytes} bytes of worker messages "
                        f"beyond the {RESULT_LIMIT}-byte limit"
                    ),
                    "fields": {},
                }
            )
        base["messages"] = results.messages
        common = {"hook": call.hook, "kind": "python", "target": target, **base}
        failure = self._failure_status(call)
        returncode = run.returncode
        if run.timed_out:
            return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
        stopped = self._stopped_reason(run.process, run.returncode)
        if stopped is not None and results.result is None:
            return HookResult(status=failure, reason=stopped, **common)
        hello = results.hello
        result = results.result
        if hello is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) before starting; see stderr",
                **common,
            )
        if hello.get("protocol") != PROTOCOL_VERSION or hello.get("version") != _EXPECTED_VERSION:
            return HookResult(
                status=failure,
                reason=(
                    f"keepwatch version mismatch: service {_EXPECTED_VERSION} (protocol {PROTOCOL_VERSION}), "
                    f"worker {hello.get('version')} (protocol {hello.get('protocol')})"
                ),
                **common,
            )
        if result is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) without reporting a result; see stderr",
                **common,
            )
        status = result.get("status")
        details = {"reason": result.get("reason"), "exception": result.get("exception")}
        if call.mode == "describe":
            return HookResult(status="ok" if status == "ok" else "error", hooks=result.get("hooks"), **details, **common)
        if call.hook == CHECK:
            if status == "answer":
                mapped = "true" if result.get("answer") else "false"
            elif status == "unknown":
                mapped = "unknown"
            else:
                mapped = "error"
            return HookResult(status=mapped, payload=result.get("payload"), **details, **common)
        return HookResult(status="ok" if status == "ok" else "failed", **details, **common)

    # -- command hooks --------------------------------------------------------

    def _run_command(self, call: HookCall, command: Command) -> HookResult:
        started = time.monotonic()
        target = command.display()
        env = self._environment(call)
        temp_files: list[Path] = []
        try:
            payload_out: Path | None = None
            try:
                call.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                call.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                stem = f"{call.poll_id}-{call.hook}"
                settings_file = call.run_dir / f"{stem}-settings.json"
                settings_file.write_text(json.dumps(dict(call.watch.settings)), encoding="utf-8")
                temp_files.append(settings_file)
                env["KEEPWATCH_SETTINGS_FILE"] = str(settings_file)
                if call.hook == CHECK:
                    payload_out = call.run_dir / f"{stem}-payload-out.json"
                    payload_out.unlink(missing_ok=True)
                    temp_files.append(payload_out)
                    env["KEEPWATCH_PAYLOAD_OUT"] = str(payload_out)
                elif call.payload is not None:
                    payload_file = call.run_dir / f"{stem}-payload.json"
                    payload_file.write_text(json.dumps(call.payload), encoding="utf-8")
                    temp_files.append(payload_file)
                    env["KEEPWATCH_PAYLOAD_FILE"] = str(payload_file)
            except OSError as exc:
                return self._error(
                    call,
                    "command",
                    target,
                    started,
                    f"cannot prepare the hook's files in {call.run_dir}: {exc.strerror or exc}",
                )
            argv = command.to_argv(call.watch.shell)
            if command.argv is not None:
                argv = platform.command_argv(argv, call.watch.watch_dir)
            try:
                process = platform.start_process(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=call.watch.watch_dir,
                    env=env,
                )
            except OSError as exc:
                return self._error(call, "command", target, started, f"cannot start command: {exc.strerror or exc}")
            out = _Reader(process.popen.stdout, call.capture_bytes)
            err = _Reader(process.popen.stderr, call.capture_bytes)
            out.start()
            err.start()
            self._track(process)
            try:
                returncode, timed_out = self._supervise(process, (out, err), call.timeout)
            finally:
                self._untrack(process)
            common = {
                "hook": call.hook,
                "kind": "command",
                "target": target,
                **self._captured(out, err, returncode, started),
            }
            if timed_out:
                return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
            stopped = self._stopped_reason(process, returncode)
            if stopped is not None:
                return HookResult(status=self._failure_status(call), reason=stopped, **common)
            if call.hook != CHECK:
                if returncode == 0:
                    return HookResult(status="ok", **common)
                return HookResult(status="failed", reason=f"exit code {returncode}", **common)
            status = classify_exit(returncode, call.watch.exit_codes)
            if status == "error":
                codes = call.watch.exit_codes
                return HookResult(
                    status="error",
                    reason=(
                        f"exit code {returncode} is not listed in [check_exit_codes] "
                        f"(true={list(codes.true)}, false={list(codes.false)}, unknown={list(codes.unknown)})"
                    ),
                    **common,
                )
            payload, problem = self._read_payload(payload_out)
            if problem is not None:
                return HookResult(status="error", reason=problem, **common)
            return HookResult(status=status, payload=payload, **common)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    @staticmethod
    def _read_payload(path: Path | None) -> tuple[Any, str | None]:
        if path is None or not path.exists() or path.stat().st_size == 0:
            return None, None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON: {exc}"
        return normalize_payload(raw)
