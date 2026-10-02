"""The API a watch's Python hooks receive. Stdlib only: this runs inside plugin environments.

Every public member is documented here; `keepwatch docs ctx` is generated from these docstrings.
"""

from __future__ import annotations

import contextlib
import glob as _glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from keepwatch import platform
from keepwatch.durations import parse_duration
from keepwatch.protocol import clip

if TYPE_CHECKING:
    from keepwatch.transfer import Transfer

Emit = Callable[[dict[str, Any]], None]
_LEDGER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
LEDGER_NAME = _LEDGER_NAME  # public: config.py validates ledger names with it


class Unknown(Exception):
    """Raise from check() to answer "unknown" with a reason that goes into the log."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class CommandFailed(Exception):
    """Raised by Ctx.run() when a command exits nonzero and check=True."""

    def __init__(self, argv: Sequence[str] | str, returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        shown = argv if isinstance(argv, str) else " ".join(argv)
        tail = stderr.strip().splitlines()[-5:]
        detail = f"; stderr: {' | '.join(tail)}" if tail else ""
        super().__init__(f"command exited {returncode}: {shown}{detail}")


class LedgerReadOnly(Exception):
    """Raised when a check tries to change a ledger. Checks must only observe."""


class LedgerCorrupt(Exception):
    """A ledger file exists but cannot be read. The message names the file to fix or delete."""


def ledger_file(data_dir: Path, name: str) -> Path:
    """Where ledger `name` of a watch is stored (data_dir is the watch's persistent data directory)."""
    return data_dir / "ledgers" / f"{name}.json"


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class Ledger:
    """A persistent set of string keys, each stamped with the time it was added.

    Stored as JSON and rewritten atomically on every add() and discard(), so a
    crash mid-loop keeps everything recorded so far. With `expire` (seconds),
    entries older than that are treated as absent and pruned on the next write.
    Beware: if the thing a key stands for can still be seen after its entry
    expires, the watch will act on it again.
    """

    def __init__(self, path: Path, *, expire: float | None = None, writable: bool = True) -> None:
        self.path = path
        self.expire = expire
        self.writable = writable
        self._entries = self._load()

    def _load(self) -> dict[str, float]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            entries = json.loads(text)["entries"]
            return {str(key): float(stamp) for key, stamp in entries.items()}
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LedgerCorrupt(
                f"ledger file {self.path} is unreadable ({type(exc).__name__}: {exc}); "
                "fix it, or delete it to start with an empty ledger"
            ) from exc

    def _fresh(self, stamp: float, now: float) -> bool:
        return self.expire is None or stamp >= now - self.expire

    def _live(self) -> dict[str, float]:
        if self.expire is None:
            return self._entries
        now = time.time()
        return {key: stamp for key, stamp in self._entries.items() if self._fresh(stamp, now)}

    def __contains__(self, key: object) -> bool:
        stamp = self._entries.get(key)  # type: ignore[call-overload]
        return stamp is not None and self._fresh(stamp, time.time())

    def __len__(self) -> int:
        return len(self._live())

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._live()))

    def added_at(self, key: str) -> datetime | None:
        """When key was added (UTC), or None if it is absent or expired."""
        stamp = self._entries.get(key)
        if stamp is None or not self._fresh(stamp, time.time()):
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc)

    def add(self, key: str) -> None:
        """Add key (or refresh its timestamp) and save immediately."""
        self._check_writable()
        if not isinstance(key, str):
            raise TypeError(f"ledger keys must be strings, got {type(key).__name__}")
        self._entries[key] = time.time()
        self._save()

    def discard(self, key: str) -> None:
        """Remove key if present and save immediately."""
        self._check_writable()
        if self._entries.pop(key, None) is not None:
            self._save()

    def _check_writable(self) -> None:
        if not self.writable:
            raise LedgerReadOnly(f"{self.path.name}: a check must not change a ledger; do it in an action")

    def _save(self) -> None:
        self._entries = dict(self._live())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "entries": self._entries}, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            platform.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            raise


class Ctx:
    """Everything a hook receives. See `keepwatch docs ctx`.

    Attributes: watch (name), hook (which hook is running), poll_id (on every
    log record of this poll), condition (before the answer in check, after it
    in actions), payload (what check returned with its answer; actions only), events (observer events delivered with this poll, oldest first; see keepwatch docs observers),
    settings (the [settings] table), watch_dir (also the working directory),
    log (a logging.Logger whose records land in keepwatch's log).
    """

    def __init__(
        self,
        *,
        watch: str,
        hook: str,
        poll_id: str,
        condition: bool,
        payload: Any,
        settings: Mapping[str, Any],
        watch_dir: Path,
        data_dir: Path,
        run_dir: Path,
        deadline: float,
        capture_bytes: int = 65_536,
        emit: Emit | None = None,
        shell: Sequence[str] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.watch = watch
        self.hook = hook
        self.poll_id = poll_id
        self.condition = condition
        self.payload = payload
        self.events: list[dict[str, Any]] = [dict(event) for event in events or ()]
        self.settings = settings
        self.watch_dir = watch_dir
        self.log = logging.getLogger(f"keepwatch.watch.{watch}")
        self._data_dir = data_dir
        self._run_dir = run_dir
        self._deadline = deadline
        self._capture_bytes = capture_bytes
        self._emit = emit or (lambda message: None)
        self._shell = list(shell) if shell else None

    @property
    def data_dir(self) -> Path:
        """Persistent storage for this watch; created on first access."""
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._data_dir

    @property
    def run_dir(self) -> Path:
        """Run-only scratch space, deleted when the keepwatch process exits; created on first access."""
        self._run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._run_dir

    @property
    def transfer(self) -> Transfer:
        """scp transfers (copy, pull, push) and tcp_open, logged like ctx.run; see `keepwatch docs transfers`."""
        from keepwatch.transfer import Transfer

        return Transfer(
            base=self.watch_dir,
            report=self._report_transfer,
            remaining=self._remaining,
            writable=self.hook != "check",
        )

    def _report_transfer(
        self, argv: list[str], exit_code: int | None, timed_out: bool, duration: float, stdout: str, stderr: str
    ) -> None:
        self._report(argv, False, exit_code, timed_out, duration, stdout, stderr)

    def _remaining(self) -> float:
        return max(self._deadline - time.time(), 0.0)

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]] | str,
        *,
        timeout: float | None = None,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and log it (command, exit code, duration, output).

        argv: a list runs directly (a relative program is resolved against the watch directory, and on
        Windows scripts get their interpreter, as for list hooks); a string runs through the watch's
        `shell`, or the platform shell. stdin is /dev/null unless `input` is given. `timeout` defaults to (and is capped
        at) the hook's remaining time; on expiry subprocess.TimeoutExpired is
        raised. With check=True a nonzero exit raises CommandFailed.
        """
        shell = isinstance(argv, str)
        args: str | list[str] = argv if isinstance(argv, str) else [os.fspath(item) for item in argv]
        remaining = self._remaining()
        limit = remaining if timeout is None else min(timeout, remaining)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                platform.shell_argv(args, self._shell)
                if shell
                else platform.command_argv(args, Path(cwd) if cwd is not None else self.watch_dir),
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                env={**os.environ, **(env or {})},
                cwd=cwd if cwd is not None else self.watch_dir,
                creationflags=platform.NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            self._report(args, shell, None, True, time.monotonic() - started, _as_text(exc.stdout), _as_text(exc.stderr))
            raise
        self._report(args, shell, completed.returncode, False, time.monotonic() - started, completed.stdout, completed.stderr)
        if check and completed.returncode != 0:
            raise CommandFailed(args, completed.returncode, completed.stderr)
        return completed

    def _report(
        self,
        args: str | list[str],
        shell: bool,
        exit_code: int | None,
        timed_out: bool,
        duration: float,
        stdout: str,
        stderr: str,
    ) -> None:
        stdout_text, stdout_cut = clip(stdout, self._capture_bytes)
        stderr_text, stderr_cut = clip(stderr, self._capture_bytes)
        self._emit(
            {
                "type": "command",
                "argv": args,
                "shell": shell,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration": round(duration, 3),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_truncated": stdout_cut,
                "stderr_truncated": stderr_cut,
            }
        )

    def ledger(self, name: str, expire: str | float | None = None) -> Ledger:
        """Open the persistent ledger `name` (letters, digits, '_', '.', '-').

        `expire` is a duration ("90d"); entries older than that are forgotten.
        Read-only inside check().
        """
        if not _LEDGER_NAME.fullmatch(name):
            raise ValueError(f"invalid ledger name {name!r}: use letters, digits, '_', '.', '-'")
        seconds = None if expire is None else parse_duration(expire)
        return Ledger(ledger_file(self._data_dir, name), expire=seconds, writable=self.hook != "check")

    def file_key(self, path: str | os.PathLike[str]) -> str:
        """"<absolute path>|<size>|<mtime_ns>": a new file reusing a name gets a new key."""
        resolved = Path(path).expanduser().resolve()
        info = resolved.stat()
        return f"{resolved}|{info.st_size}|{info.st_mtime_ns}"

    def glob(self, pattern: str) -> list[Path]:
        """Sorted glob matches; expands ~. Relative patterns are relative to watch_dir."""
        return sorted(Path(match) for match in _glob.glob(os.path.expanduser(pattern)))

    def unchanged_for(self, path: str | os.PathLike[str], duration: str | float) -> bool:
        """True when nothing has written to the file for `duration`; False if it does not exist."""
        try:
            mtime = Path(path).expanduser().stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - mtime >= parse_duration(duration)
