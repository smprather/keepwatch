"""File transfers with the system's OpenSSH scp (see `keepwatch docs transfers`). Stdlib only.

This module also runs inside plugin environments (through ctx.transfer), so it imports only the standard
library and keepwatch's stdlib-only modules.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import itertools
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from keepwatch import askpass, platform
from keepwatch.ctx import CommandFailed
from keepwatch.durations import format_duration
from keepwatch.offline import iso_time
from keepwatch.paths import write_json_atomic

PROTOCOLS = ("scp", "sftp")
PASSWORD_MODES = ("askpass", "conpty")
CONFLICTS = ("skip-identical", "rename", "overwrite")
MARKERS = ("sha256", "none")
SCP_DEFAULTS = (
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)
CHUNK = 1024 * 1024
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI = re.compile(r"scp://(?:([^@/]+)@)?([^:/@]+)(?::(\d+))?/(.*)", re.S)
_VERSION = re.compile(r"OpenSSH_(?:for_Windows_)?(\d+)\.(\d+)")
_SAFE = re.compile(r"[A-Za-z0-9_./:@%+,=-]")


def escape_remote_path(path: str) -> str:
    """Backslash-escape a remote path for scp, whose classic protocol hands it to the remote shell.

    Backslashes are the one form that works in both protocols (spike 2026-10-02: single quotes fail in both).
    A leading `~/` (or a lone `~`) is kept so that it still means the remote home.
    """
    if path == "~":
        return path
    prefix = "~/" if path.startswith("~/") else ""
    rest = path[len(prefix) :]
    return prefix + "".join(char if _SAFE.fullmatch(char) else "\\" + char for char in rest)


def remote_path_arg(path: str, *, classic: bool, source: bool, windows: bool = platform.IS_WINDOWS) -> str:
    """A remote path as scp needs it on the command line.

    SFTP takes the plain path. The classic protocol hands it to the remote shell: on POSIX backslash escapes
    work both ways; Windows' scp turns backslashes into slashes, so a pull uses `?` for each special character
    (scp's own name check accepts glob matches; size and sha256 checks catch a wrong match) and a push quotes.
    """
    if not classic:
        return path
    if not windows:
        return escape_remote_path(path)
    prefix = "~/" if path.startswith("~/") else ""
    rest = path[len(prefix) :]
    if all(_SAFE.fullmatch(char) for char in rest):
        return path
    if source:
        return prefix + "".join(char if _SAFE.fullmatch(char) else "?" for char in rest)
    if "'" not in rest:
        return f"{prefix}'{rest}'"
    if not any(char in rest for char in '"$`\\'):
        return f'{prefix}"{rest}"'
    raise TransferFailed(
        f"cannot name {path!r} for scp's classic protocol on Windows (it holds both kinds of quotes or $ ` \\); "
        'rename the file or use protocol = "sftp"'
    )


def _local_arg(path: str) -> str:
    head = re.split(r"[\\/]", path, maxsplit=1)[0]
    if ":" in head and not _DRIVE.match(path):
        return "./" + path  # scp would read "name:with:colons" as host "name"
    return path


def _operand(end: Endpoint, *, classic: bool, source: bool) -> str:
    if not end.remote:
        return _local_arg(end.path)
    who = f"{end.user}@{end.host}" if end.user else str(end.host)
    return f"{who}:{remote_path_arg(end.path, classic=classic, source=source)}"


Report = Callable[[list[str], int | None, bool, float, str, str], None]


class TransferFailed(Exception):
    """A transfer could not be attempted or verified. (scp itself failing raises keepwatch.CommandFailed.)"""


class TransferMismatch(TransferFailed):
    """A pulled file's size or sha256 differs from what was expected (usually: it changed after it was reported)."""


@dataclass(frozen=True)
class Endpoint:
    """One side of a transfer: a local path, or a path on `host` (`user@host:path` or `scp://user@host:port/path`)."""

    path: str
    host: str | None = None
    user: str | None = None
    port: int | None = None
    uri: bool = False

    @property
    def remote(self) -> bool:
        return self.host is not None

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    def scp_arg(self) -> str:
        """The endpoint as people write it (in messages and results). scp_argv builds scp's own operands."""
        if not self.remote:
            return self.path
        who = f"{self.user}@{self.host}" if self.user else str(self.host)
        if self.uri:
            port = f":{self.port}" if self.port else ""
            return f"scp://{who}{port}/{self.path}"
        return f"{who}:{self.path}"

    def child(self, name: str) -> Endpoint:
        """This endpoint taken as a directory, with `name` inside it."""
        if not self.remote:
            return replace(self, path=str(Path(self.path) / name))
        if not self.path:
            return replace(self, path=name)
        return replace(self, path=self.path.rstrip("/") + "/" + name)


def parse_endpoint(text: str) -> Endpoint:
    """`scp://[user@]host[:port]/path`, `[user@]host:path` (a colon before any slash), or a local path."""
    if not text:
        raise ValueError("an endpoint must not be empty")
    if text.startswith("scp://"):
        match = _URI.fullmatch(text)
        if not match:
            raise ValueError(f"{text!r} is not scp://[user@]host[:port]/path")
        user, host, port, path = match.groups()
        return Endpoint(path=path, host=host, user=user, port=int(port) if port else None, uri=True)
    if _DRIVE.match(text):
        return Endpoint(path=text)  # C:\data or C:/data: a Windows drive, not host "C"
    before, colon, after = text.partition(":")
    user, _, host = before.rpartition("@")
    if colon and host and "/" not in before and "\\" not in host:  # DOMAIN\user@host:path is remote
        return Endpoint(path=after, host=host, user=user or None)
    return Endpoint(path=text)


def parse_openssh_version(text: str) -> tuple[int, int] | None:
    match = _VERSION.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


_VERSIONS: dict[str, tuple[int, int]] = {}


def openssh_version(ssh: str = "ssh") -> tuple[int, int] | None:
    """The local OpenSSH version from `ssh -V` (cached per program once known), or None if it cannot be told."""
    if ssh in _VERSIONS:
        return _VERSIONS[ssh]
    try:
        completed = subprocess.run(
            [ssh, "-V"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=platform.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # not cached: a busy or briefly missing ssh is asked again next time
    version = parse_openssh_version(completed.stderr + completed.stdout)
    if version is not None:
        _VERSIONS[ssh] = version
    return version


def _ssh_beside(scp: str) -> str:
    """The ssh program that belongs to this scp (same directory when scp is a path)."""
    path = Path(scp)
    if not platform.has_path_separator(scp):
        return "ssh"
    return str(path.with_name("ssh" + path.suffix))


def _sftp_beside(scp: str) -> str:
    """The sftp program that belongs to this scp (scp itself cannot resume a partial file)."""
    path = Path(scp)
    if not platform.has_path_separator(scp):
        return "sftp"
    return str(path.with_name("sftp" + path.suffix))


def format_bytes(count: int) -> str:
    """A size as text with binary units: 512 B, 1.5 KiB, 2.6 GiB."""
    for unit, step in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if count >= step:
            return f"{count / step:.1f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{count} B"


def _quoted_for_sftp(path: str) -> str:
    """A path for sftp's batch parser, which splits on whitespace but honours double quotes and backslashes."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sftp_argv(host: Endpoint, options: ScpOptions, batch: Path) -> list[str]:
    """The sftp command line for one batch run (reget continues a pull, reput a push; scp does neither).

    sftp takes the scp options it shares (-o, -i, -P, -F is part of ssh_options) and reads its commands from
    `batch`. The program is the `sftp` beside `scp_command[0]`, so a watch that names its scp names its sftp.
    """
    argv = [_sftp_beside(options.scp_command[0]), *options.ssh_options]
    argv += ["-o", "NumberOfPasswordPrompts=1"] if options.password_env else ["-o", "BatchMode=yes"]
    if options.known_hosts is not None:
        argv += known_hosts_option(options.known_hosts)
    if options.identity is not None:
        argv += ["-i", str(options.identity), "-o", "IdentitiesOnly=yes"]
    port = options.port if options.port is not None else host.port
    if port is not None:
        argv += ["-P", str(port)]
    argv += ["-q", "-b", str(batch)]
    name = f"{host.user}@{host.host}" if host.user else str(host.host)
    return [*argv, name]


def sftp_reget_batch(source: Endpoint, part: Path) -> str:
    """The sftp batch that continues `part` from the remote file (reget resumes from the local size)."""
    return f"reget {_quoted_for_sftp(source.path)} {_quoted_for_sftp(str(part))}\n"


def sftp_reput_batch(local: Path, target: Endpoint) -> str:
    """The sftp batch that continues `target` on the destination from `local` (reput resumes from its size)."""
    return f"reput {_quoted_for_sftp(str(local))} {_quoted_for_sftp(target.path)}\n"


def _refused_overwrite(exc: CommandFailed, name: str) -> bool:
    """Whether scp failed because the destination holds NAME and refuses to overwrite it.

    The signature is `<path>/NAME: Permission denied`. That is not the authentication failure
    (`Permission denied (publickey,password)`, no path) it is so easily mistaken for; see `keepwatch docs
    transfers` for the whole trap.
    """
    return f"{name}: Permission denied" in (exc.stderr or "")


@dataclass(frozen=True)
class ScpOptions:
    """How to run scp. `ssh_options` are extra scp arguments placed before keepwatch's own (ssh keeps the first value)."""

    protocol: str = "scp"
    password_env: str | None = None
    identity: str | os.PathLike[str] | None = None
    known_hosts: str | os.PathLike[str] | None = None
    port: int | None = None
    ssh_options: Sequence[str] = ()
    timeout: float | None = None
    deadline: float | None = None
    scp_command: Sequence[str] = ("scp",)
    password_mode: str = "askpass"

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {', '.join(PROTOCOLS)}, got {self.protocol!r}")
        if self.password_env is not None and not _VARIABLE.fullmatch(self.password_env):
            raise ValueError(f"password_env must be an environment variable name, got {self.password_env!r}")
        if self.password_mode not in PASSWORD_MODES:
            raise ValueError(f"password_mode must be one of {', '.join(PASSWORD_MODES)}, got {self.password_mode!r}")
        if self.password_mode == "conpty":
            if self.password_env is None:
                raise ValueError('password_mode = "conpty" needs password_env: there is no password to type otherwise')
            if not platform.IS_WINDOWS:
                raise ValueError('password_mode = "conpty" is Windows-only for now (a POSIX pty is untested)')
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        if isinstance(self.ssh_options, str):
            raise ValueError("ssh_options must be a list of scp arguments, e.g. ['-o', 'ProxyJump=bastion'], not a string")
        if self.ssh_options and not str(self.ssh_options[0]).startswith("-"):
            raise ValueError(
                f"ssh_options are scp arguments and must start with an option, e.g. ['-o', {self.ssh_options[0]!r}]"
            )
        object.__setattr__(self, "ssh_options", tuple(self.ssh_options))
        object.__setattr__(self, "scp_command", tuple(self.scp_command))


def _shown(version: tuple[int, int] | None) -> str:
    return "of unknown version" if version is None else f"OpenSSH {version[0]}.{version[1]}"


def known_hosts_option(path: str | os.PathLike[str]) -> list[str]:
    """The ssh option that checks host keys against `path` (quoted: ssh splits option values at spaces)."""
    return ["-o", f'UserKnownHostsFile="{Path(path).as_posix()}"']


def with_known_hosts(ssh_options: Sequence[str], known_hosts: str | os.PathLike[str] | None) -> list[str]:
    """ssh_options plus the known_hosts check: what the remote_files observer and the pull recipe's deletes use."""
    return [*ssh_options, *(known_hosts_option(known_hosts) if known_hosts else [])]


def scp_argv(
    source: Endpoint,
    destination: Endpoint,
    options: ScpOptions,
    version: tuple[int, int] | None,
) -> list[str]:
    """The scp command line for one copy (see the module docs for every option)."""
    if not (source.remote or destination.remote):
        raise ValueError("a transfer needs at least one remote endpoint (user@host:path or scp://user@host/path)")
    both = source.remote and destination.remote
    if options.protocol == "sftp" and (version is None or version < (9, 0)):
        raise TransferFailed(f'protocol = "sftp" needs OpenSSH 9.0 or newer; this scp is {_shown(version)}')
    if both and options.protocol != "sftp":
        raise TransferFailed(
            'copying between two remote hosts needs protocol = "sftp": with the classic scp protocol, '
            "scp -3 reports success even when the destination host fails"
        )
    if both and options.password_env:
        raise TransferFailed(
            "copying between two remote hosts needs key authentication on both; password_env is not supported "
            "with two remote endpoints (copy through a local folder instead)"
        )
    argv = [*options.scp_command, *options.ssh_options, *SCP_DEFAULTS]
    argv += ["-o", "NumberOfPasswordPrompts=1"] if options.password_env else ["-o", "BatchMode=yes"]
    if options.known_hosts is not None:
        argv += known_hosts_option(options.known_hosts)
    if options.identity is not None:
        argv += ["-i", str(options.identity), "-o", "IdentitiesOnly=yes"]
    port = options.port
    if port is None:
        ports = {end.port for end in (source, destination) if end.remote and end.port}
        if len(ports) > 1:
            raise TransferFailed(f"the two hosts use different ports ({', '.join(map(str, sorted(ports)))}); scp takes one -P")
        port = ports.pop() if ports else None
    if port is not None:
        argv += ["-P", str(port)]
    if options.protocol == "scp" and version is not None and version >= (9, 0):
        argv.append("-O")
    if both:
        argv.append("-3")
    classic = options.protocol == "scp"
    return [*argv, "--", _operand(source, classic=classic, source=True), _operand(destination, classic=classic, source=False)]


def _askpass_python() -> str:
    """A console Python for the askpass launcher (pythonw.exe has no usable stdout of its own)."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
        return str(executable.with_name("python.exe"))
    return str(executable)


def _askpass_script() -> str:
    """The helper's own file, run as a path rather than `-m keepwatch.askpass`.

    The launcher's interpreter is not always one that can import keepwatch (under a uv symlinked venv on
    Windows, sys.executable and its sibling console python are the base interpreter), but the helper imports
    only the standard library, so its file runs under any interpreter.
    """
    return str(Path(askpass.__file__).resolve())


def write_askpass(directory: Path, variable: str) -> Path:
    """Write the SSH_ASKPASS launcher: it runs the askpass helper's file (no secret in the file)."""
    if not _VARIABLE.fullmatch(variable):
        raise ValueError(f"not an environment variable name: {variable!r}")
    python, script = _askpass_python(), _askpass_script()
    if platform.IS_WINDOWS:
        path = directory / "askpass.cmd"
        line = f'"{python.replace("%", "%%")}" "{script.replace("%", "%%")}" {variable} %*'
        text = f"@echo off\r\n{line}\r\n"
        try:
            data = text.encode("oem")  # cmd.exe reads batch files in the OEM code page (C:\Users\José\...)
        except (LookupError, UnicodeEncodeError):
            data = text.encode("utf-8")
        path.write_bytes(data)
        return path
    path = directory / "askpass.sh"
    path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(python)} {shlex.quote(script)} {variable} "$@"\n', encoding="utf-8"
    )
    path.chmod(0o700)
    return path


@contextlib.contextmanager
def _transfer_env(options: ScpOptions) -> Generator[dict[str, str]]:
    """The environment for scp and sftp: the askpass launcher when a password goes through askpass.

    With `password_mode = "conpty"` the password is typed at the console instead, so ssh must not find an
    askpass launcher (or it would take that path): both variables are removed from the child's environment.
    """
    env = dict(os.environ)
    if options.password_env and options.password_env not in os.environ:
        raise TransferFailed(
            f"password_env names {options.password_env}, which is not set in keepwatch's environment "
            "(on Windows: setx; for the service, restart it after setting the variable)"
        )
    with contextlib.ExitStack() as stack:
        if options.password_env and options.password_mode == "askpass":
            work = stack.enter_context(tempfile.TemporaryDirectory(prefix="keepwatch-scp-", dir=_askpass_dir()))
            env["SSH_ASKPASS"] = str(write_askpass(Path(work), options.password_env))
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")  # OpenSSH before 8.4 uses SSH_ASKPASS only when DISPLAY is set
        if options.password_mode == "conpty":
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
        yield env


def _deadline_limit(options: ScpOptions, what: str, target: str) -> float | None:
    """The timeout for one run: the caller's timeout, never past the deadline; None means no limit."""
    limit = options.timeout
    if options.deadline is not None:
        left = options.deadline - time.time()
        if left <= 0:
            raise TransferFailed(f"no time left for {what} {target} (the deadline has passed)")
        limit = left if limit is None else min(limit, left)
    return limit


def _verify_part(part: Path, src: Endpoint, size: int | None, sha256: str | None) -> None:
    """Check a fetched part against what the observer reported: a mismatch means it changed or is corrupt."""
    actual_size = part.stat().st_size
    if size is not None and actual_size != size:
        raise TransferMismatch(f"pulled {actual_size} bytes of {src.scp_arg()}, expected {size}")
    if sha256 is not None:
        actual = sha256_file(part)
        if actual != sha256.lower():
            raise TransferMismatch(f"sha256 of the pulled {src.scp_arg()} is {actual}, expected {sha256.lower()}")


def _discard_part(part: Path, name: str, warn: Callable[[str], None] | None) -> None:
    """Delete a partial file from an earlier attempt (a killed transfer leaves one behind), saying the cost."""
    try:
        wasted = part.stat().st_size
    except OSError:
        wasted = 0
    part.unlink(missing_ok=True)
    if wasted and warn is not None:
        warn(f"discarded {format_bytes(wasted)} of a partial {name} from an earlier attempt")


def _sftp_reget(source: Endpoint, part: Path, options: ScpOptions, report: Report | None) -> None:
    """Continue a partial pull with sftp's reget: what scp itself cannot do (`resume` in the pull recipe)."""
    limit = _deadline_limit(options, "sftp", f"{source.scp_arg()} -> {part}")
    with tempfile.TemporaryDirectory(prefix="keepwatch-sftp-", dir=_askpass_dir()) as work:
        batch = Path(work) / "reget.batch"
        batch.write_text(sftp_reget_batch(source, part), encoding="utf-8")
        argv = sftp_argv(source, options, batch)
        started = time.monotonic()
        with _transfer_env(options) as env:
            returncode, stdout, stderr, timed_out = _run(argv, env, limit, options=options)
    if report is not None:
        report(argv, returncode, timed_out, time.monotonic() - started, stdout, stderr)
    if timed_out:
        raise TransferFailed(f"sftp timed out after {format_duration(limit or 0)}: {source.scp_arg()} -> {part}")
    if returncode != 0:
        raise CommandFailed(argv, returncode if returncode is not None else -1, stderr)


def _sftp_reput(local: Path, target: Endpoint, options: ScpOptions, report: Report | None) -> None:
    """Continue a partial upload with sftp's reput, for a destination that refuses to overwrite (see `resume`)."""
    limit = _deadline_limit(options, "sftp", f"{local} -> {target.scp_arg()}")
    with tempfile.TemporaryDirectory(prefix="keepwatch-sftp-", dir=_askpass_dir()) as work:
        batch = Path(work) / "reput.batch"
        batch.write_text(sftp_reput_batch(local, target), encoding="utf-8")
        argv = sftp_argv(target, options, batch)
        started = time.monotonic()
        with _transfer_env(options) as env:
            returncode, stdout, stderr, timed_out = _run(argv, env, limit, options=options)
    if report is not None:
        report(argv, returncode, timed_out, time.monotonic() - started, stdout, stderr)
    if timed_out:
        raise TransferFailed(f"sftp timed out after {format_duration(limit or 0)}: {local} -> {target.scp_arg()}")
    if returncode != 0:
        raise CommandFailed(argv, returncode if returncode is not None else -1, stderr)


def _run(
    argv: list[str], env: dict[str, str], timeout: float | None, *, options: ScpOptions
) -> tuple[int | None, str, str, bool]:
    if options.password_mode == "conpty":
        from keepwatch.conpty import run as conpty_run  # imported only for this mode (an optional extra)

        return conpty_run(argv, env.get(options.password_env or "", ""), limit=timeout, env=env)
    try:
        if platform.IS_WINDOWS:
            process = platform.start_process(
                argv, cwd=None, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            popen, terminate, stop, close = process.popen, process.kill, process.kill, process.close
        else:
            # The caller's process group: a hook killed at its deadline takes scp (and its ssh) with it.
            popen = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            terminate, stop, close = popen.terminate, popen.kill, (lambda: None)
    except OSError as exc:
        raise TransferFailed(f"cannot run {argv[0]}: {exc.strerror or exc} (is OpenSSH's scp installed?)") from exc
    timed_out = False
    try:
        try:
            out, err = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate()  # scp stops its ssh child on SIGTERM; a SIGKILL would leave ssh holding our pipes
            try:
                out, err = popen.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stop()
                try:
                    out, err = popen.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    out, err = b"", b""
    finally:
        close()
    return popen.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def _endpoint(value: str | os.PathLike[str] | Endpoint) -> Endpoint:
    if isinstance(value, Endpoint):
        return value
    if isinstance(value, os.PathLike):
        return Endpoint(path=os.fspath(value))
    return parse_endpoint(value)


def _askpass_dir() -> str | None:
    """Where the askpass launcher goes: the hook's run dir (often not noexec, unlike /tmp), else the temp dir."""
    run_dir = os.environ.get("KEEPWATCH_RUN_DIR")
    return run_dir if run_dir and os.path.isdir(run_dir) else None


ACTIVITY = "activity.json"


def _activity_file() -> Path | None:
    """Where a transfer publishes what it is doing: the hook's run dir, when there is one."""
    run_dir = os.environ.get("KEEPWATCH_RUN_DIR")
    return Path(run_dir) / ACTIVITY if run_dir and os.path.isdir(run_dir) else None


@contextlib.contextmanager
def _activity(**fields: Any) -> Generator[None]:
    """Publish what this transfer is doing (best effort: a status line never fails a transfer)."""
    path = _activity_file()
    if path is not None:
        try:
            write_json_atomic(path, {"started_at": iso_time(time.time()), **fields})
        except OSError:
            path = None
    try:
        yield
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def read_activity(run_dir: Path) -> dict[str, Any] | None:
    """What a hook in this run dir is transferring, with the part's current size; None when nothing is."""
    try:
        activity = json.loads((run_dir / ACTIVITY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(activity, dict):
        return None
    part = activity.get("part")
    if isinstance(part, str):
        try:
            activity["part_size"] = Path(part).stat().st_size
        except OSError:
            pass
    return activity


def transfer_progress(activity: dict[str, Any]) -> float | None:
    """How much of a pull has arrived (part_size / size), or None when that cannot be told."""
    size, part_size = activity.get("size"), activity.get("part_size")
    if not isinstance(size, int) or not isinstance(part_size, int) or size <= 0:
        return None
    return min(max(part_size / size, 0.0), 1.0)


def activity_text(activity: dict[str, Any]) -> str:
    """One line for `status` and the TUI: `pulling psg-export.gz 74% (2.6 GiB/3.7 GiB)`."""
    name = str(activity.get("name") or "?")
    size = activity.get("size")
    if not isinstance(activity.get("part"), str):
        return f"pushing {name}" + (f" ({format_bytes(size)})" if isinstance(size, int) else "")
    progress = transfer_progress(activity)
    if progress is None:
        return f"pulling {name}"
    if isinstance(size, int) and isinstance(activity.get("part_size"), int):
        return (f"pulling {name} {progress * 100:.0f}% "
                f"({format_bytes(activity['part_size'])}/{format_bytes(size)})")
    return f"pulling {name} {progress * 100:.0f}%"


def copy(
    source: str | os.PathLike[str] | Endpoint,
    destination: str | os.PathLike[str] | Endpoint,
    *,
    options: ScpOptions = ScpOptions(),
    report: Report | None = None,
) -> None:
    """Copy one file with scp. Raises CommandFailed when scp fails, TransferFailed when it cannot be attempted."""
    src, dst = _endpoint(source), _endpoint(destination)
    argv = scp_argv(src, dst, options, openssh_version(_ssh_beside(options.scp_command[0])))
    limit = options.timeout
    if options.deadline is not None:
        left = options.deadline - time.time()
        if left <= 0:
            raise TransferFailed(f"no time left for scp {src.scp_arg()} -> {dst.scp_arg()} (the deadline has passed)")
        limit = left if limit is None else min(limit, left)
    started = time.monotonic()
    with _transfer_env(options) as env:
        returncode, stdout, stderr, timed_out = _run(argv, env, limit, options=options)
    if report is not None:
        report(argv, returncode, timed_out, time.monotonic() - started, stdout, stderr)
    if timed_out:
        shown = format_duration(limit or 0)
        raise TransferFailed(f"scp timed out after {shown}: {src.scp_arg()} -> {dst.scp_arg()}")
    if returncode != 0:
        raise CommandFailed(argv, returncode if returncode is not None else -1, stderr)


def sha256_file(path: str | os.PathLike[str]) -> str:
    """The sha256 of a file as hex, read in chunks so a large file never sits in memory.

    An unreadable file raises the OSError from open/read: callers decide what that means (the files observer
    treats it as "not ready yet", a pull as "try again").
    """
    digest = hashlib.sha256()
    try:
        handle = open(path, "rb")
    except OSError:
        raise  # the OSError is the contract, not an accident
    with handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _free_name(directory: Path, name: str, same: Callable[[Path], bool]) -> Path:
    """name-1.ext, name-2.ext, …: an existing one for which same() holds, else the first free one (a.tar.gz -> a-1.tar.gz)."""
    head, dot, tail = name.partition(".") if not name.startswith(".") else (name, "", "")
    for number in itertools.count(1):
        candidate = directory / (f"{head}-{number}.{tail}" if dot else f"{head}-{number}")
        if not candidate.exists() or same(candidate):
            return candidate
    raise AssertionError("unreachable")


def _identical(path: Path, size: int | None, sha256: str | None) -> bool:
    """Whether path already holds the file with this size and sha256 (sizes first: no hashing when they differ)."""
    if sha256 is None:
        return False
    try:
        if size is not None and path.stat().st_size != size:
            return False
        return sha256_file(path) == sha256.lower()
    except OSError:
        return False


def pull(
    remote: str | Endpoint,
    local_dir: str | os.PathLike[str],
    *,
    size: int | None = None,
    sha256: str | None = None,
    on_conflict: str = "skip-identical",
    options: ScpOptions = ScpOptions(),
    resume: bool = False,
    report: Report | None = None,
    warn: Callable[[str], None] | None = None,
) -> Path:
    """Copy a remote file into local_dir through .NAME.part, verify size and sha256 when given, rename it (an identical existing file is a no-op).

    A partial file from an earlier (killed) attempt is discarded, and `warn` is told how many bytes that cost.
    With `resume` — which needs the sftp protocol, a known size and a sha256 — it is continued with sftp's
    reget instead, and kept if this attempt fails; a resumed file that does not verify is pulled again from
    the start, once.
    """
    src = _endpoint(remote)
    if not src.remote:
        raise ValueError(f"pull needs a remote source (user@host:path), got {src.scp_arg()!r}")
    if on_conflict not in CONFLICTS:
        raise ValueError(f"on_conflict must be one of {', '.join(CONFLICTS)}, got {on_conflict!r}")
    if sha256 is not None and not re.fullmatch(r"[0-9A-Fa-f]{64}", sha256):
        raise ValueError(f"sha256 must be 64 hex characters, got {sha256!r}")
    name = src.name
    if not name or name in (".", ".."):
        raise ValueError(f"{src.scp_arg()!r} does not name a file")
    if platform.IS_WINDOWS and any(char in name for char in '<>:"|?*'):
        raise TransferFailed(f"{name!r} cannot be stored as a Windows file name (it holds one of < > : \" | ? *)")
    directory = Path(local_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / name
    same = functools.partial(_identical, size=size, sha256=sha256)
    if final.exists():
        if same(final):
            return final  # an earlier pull of this exact file finished
        if on_conflict == "skip-identical":
            raise TransferFailed(
                f"{final} already exists with other or unknown content; pass sha256 to skip identical files, or "
                "on_conflict='rename' or 'overwrite'"
            )
        if on_conflict == "rename":
            final = _free_name(directory, name, same)
            if final.exists():
                return final  # a numbered copy of this exact file is already there
    part = directory / f".{name}.part"
    kept = part.stat().st_size if part.exists() else 0
    resumable = resume and kept > 0 and size is not None and kept < size
    if resumable:
        if warn is not None:
            warn(f"continuing {name} from {format_bytes(kept)} left by an earlier attempt")
    else:
        _discard_part(part, name, warn)
    attempts = [kept, 0] if resumable else [0]
    try:
        with _activity(name=name, part=str(part), size=size):  # status.json and the TUI read this
            for index, offset in enumerate(attempts):
                try:
                    if offset:
                        _sftp_reget(src, part, options, report)
                    else:
                        copy(src, Endpoint(path=str(part)), options=options, report=report)
                    _verify_part(part, src, size, sha256)
                    platform.replace(part, final)
                    break
                except TransferMismatch:
                    if index + 1 == len(attempts):
                        raise
                    if warn is not None:
                        warn(f"the resumed transfer of {name} did not verify; pulling it again from the start")
                    part.unlink(missing_ok=True)
    finally:
        if not resumable or not part.exists():
            part.unlink(missing_ok=True)  # a resumable partial is kept: the next attempt continues it
    return final


def push(
    path: str | os.PathLike[str],
    remote_dir: str | Endpoint,
    *,
    marker: str = "sha256",
    options: ScpOptions = ScpOptions(),
    resume: bool = False,
    report: Report | None = None,
    warn: Callable[[str], None] | None = None,
) -> str:
    """Upload a file into remote_dir, then (marker="sha256") NAME.sha256 in sha256sum format once the data succeeded. Returns the remote path.

    A destination that already holds NAME and refuses to overwrite it is a trap: a partial file from a failed
    attempt poisons that name until someone removes it, and the bare `Permission denied` reads like an auth
    failure. `warn` is told what it probably means; with `resume` (needs the sftp protocol) keepwatch continues
    the partial with sftp's reput instead of failing. The marker is still written afterwards, so the receiving
    side verifies the finished file.
    """
    local = Path(path)
    if not local.is_file():
        raise TransferFailed(f"{local} is not a file")
    if marker not in MARKERS:
        raise ValueError(f"marker must be one of {', '.join(MARKERS)}, got {marker!r}")
    directory = _endpoint(remote_dir)
    if not directory.remote:
        raise ValueError(f"push needs a remote directory (user@host:dir), got {directory.scp_arg()!r}")
    target = directory.child(local.name)
    try:
        size = local.stat().st_size
    except OSError:
        size = None
    with _activity(name=local.name, size=size):  # status.json and the TUI read this
        try:
            copy(local, target, options=options, report=report)
        except CommandFailed as exc:
            if not _refused_overwrite(exc, local.name):
                raise
            if warn is not None:
                warn(
                    f"{local.name} is already on the destination and its server refuses to overwrite it (a partial "
                    "file from an earlier attempt?); remove it there and the next attempt pushes cleanly"
                )
            if not resume:
                raise
            if warn is not None:
                warn(f"continuing that partial {local.name} with sftp (its marker still verifies the result)")
            _sftp_reput(local, target, options, report)
        if marker == "none":
            return target.scp_arg()
        with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
            marker_file = Path(work) / f"{local.name}.sha256"
            marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
            # A second run, only after the data succeeded: one scp run carries on past a failed source, which
            # could put the marker beside a truncated file.
            copy(marker_file, directory.child(marker_file.name), options=options, report=report)
    return target.scp_arg()


def tcp_open(host: str, port: int = 22, timeout: float = 5.0) -> bool:
    """True when host:port accepts a TCP connection within `timeout` seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Transfer:
    """Transfers for hooks, as `ctx.transfer`: relative local paths start at the watch directory, every scp run
    is logged as a `command` record (like ctx.run), and the hook's remaining time caps `timeout`.

    Options (keyword arguments of copy, pull and push): protocol ("scp" or "sftp"), password_env (the name
    of an environment variable holding the password), password_mode ("askpass", the default, or "conpty": type
    the password at a Windows pseudo-console, for servers that refuse askpass — see `keepwatch docs transfers`),
    identity (a private key file), known_hosts (a known_hosts file), port, ssh_options (extra scp arguments),
    timeout (seconds, per scp run) and deadline (the epoch after which this call's scp must not run; the hook's
    remaining time by default). A check may only use tcp_open: transferring files changes things, which a check
    must not do.

    `pull` also takes `resume=True` (needs protocol="sftp" and a known size and sha256): an unfinished
    `.NAME.part` from an earlier attempt is continued with the sftp client instead of being discarded.
    """

    def __init__(
        self,
        *,
        base: Path,
        report: Report | None = None,
        remaining: Callable[[], float] | None = None,
        writable: bool = True,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._base = base
        self._report = report
        self._remaining = remaining
        self._writable = writable
        self._warn = warn

    def _options(self, options: dict[str, Any]) -> ScpOptions:
        if not self._writable:
            raise TransferFailed("a check must not transfer files; do it in an action (ctx.transfer.tcp_open is fine)")
        deadline = options.pop("deadline", None)  # a caller's own deadline wins over the hook's remaining time
        if deadline is None and self._remaining is not None:
            left = self._remaining()
            if left <= 0:
                raise TransferFailed("no time left in this hook for a transfer (raise action_timeout)")
            deadline = time.time() + left  # each scp run (the file, then its marker) gets only what is left
        return ScpOptions(deadline=deadline, **options)

    def _local(self, value: str | os.PathLike[str] | Endpoint) -> Endpoint:
        endpoint = _endpoint(value)
        if endpoint.remote or Path(endpoint.path).is_absolute():
            return endpoint
        return Endpoint(path=str(self._base / endpoint.path))

    def copy(self, source: str | os.PathLike[str], destination: str | os.PathLike[str], **options: Any) -> None:
        """Copy one file with scp; one side must be remote (user@host:path or scp://user@host:port/path)."""
        scp = self._options(options)
        copy(self._local(source), self._local(destination), options=scp, report=self._report)

    def pull(
        self,
        remote: str,
        local_dir: str | os.PathLike[str],
        *,
        size: int | None = None,
        sha256: str | None = None,
        on_conflict: str = "skip-identical",
        resume: bool = False,
        **options: Any,
    ) -> Path:
        """Copy `remote` into local_dir through `.NAME.part`, verify size/sha256 when given, rename; returns the path.

        An existing file with the given sha256 makes this a no-op; any other existing file is an error unless
        on_conflict is "rename" (NAME-1.ext) or "overwrite". With `resume` (needs protocol="sftp" and a
        sha256) an unfinished `.NAME.part` is continued with sftp's reget instead of starting over.
        """
        scp = self._options(options)
        directory = Path(local_dir)
        if not directory.is_absolute():
            directory = self._base / directory
        return pull(remote, directory, size=size, sha256=sha256, on_conflict=on_conflict, options=scp,
                    resume=resume, warn=self._warn, report=self._report)

    def push(self, path: str | os.PathLike[str], remote_dir: str, *, marker: str = "sha256", resume: bool = False,
             **options: Any) -> str:
        """Upload a file into remote_dir, then NAME.sha256 (sha256sum format) unless marker="none"; returns the remote path.

        With `resume` (needs protocol="sftp"), a destination that refuses to overwrite a partial file from an
        earlier attempt is continued with sftp's reput instead of failing.
        """
        scp = self._options(options)
        local = Path(path)
        if not local.is_absolute():
            local = self._base / local
        return push(local, remote_dir, marker=marker, options=scp, resume=resume, warn=self._warn, report=self._report)

    def tcp_open(self, host: str, port: int = 22, timeout: float = 5.0) -> bool:
        """True when host:port accepts a TCP connection within `timeout` seconds (allowed in a check)."""
        if self._remaining is not None:
            timeout = max(min(timeout, self._remaining()), 0.1)
        return tcp_open(host, port, timeout)
