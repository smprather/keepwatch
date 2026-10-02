"""File transfers with the system's OpenSSH scp (see `keepwatch docs transfers`). Stdlib only.

This module also runs inside plugin environments (through ctx.transfer), so it imports only the standard
library and keepwatch's stdlib-only modules.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from keepwatch import platform
from keepwatch.ctx import CommandFailed
from keepwatch.durations import format_duration

PROTOCOLS = ("scp", "sftp")
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

Report = Callable[[list[str], int | None, bool, float, str, str], None]


class TransferFailed(Exception):
    """A transfer could not be attempted or verified. (scp itself failing raises keepwatch.CommandFailed.)"""


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
    if colon and before and "/" not in before and "\\" not in before:
        user, _, host = before.rpartition("@")
        return Endpoint(path=after, host=host, user=user or None)
    return Endpoint(path=text)


def parse_openssh_version(text: str) -> tuple[int, int] | None:
    match = _VERSION.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


@functools.lru_cache(maxsize=8)
def openssh_version(ssh: str = "ssh") -> tuple[int, int] | None:
    """The local OpenSSH version from `ssh -V` (cached per program), or None if it cannot be told."""
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
        return None
    return parse_openssh_version(completed.stderr + completed.stdout)


def _ssh_beside(scp: str) -> str:
    """The ssh program that belongs to this scp (same directory when scp is a path)."""
    path = Path(scp)
    if not platform.has_path_separator(scp):
        return "ssh"
    return str(path.with_name("ssh" + path.suffix))


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
    scp_command: Sequence[str] = ("scp",)

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"protocol must be one of {', '.join(PROTOCOLS)}, got {self.protocol!r}")
        if self.password_env is not None and not _VARIABLE.fullmatch(self.password_env):
            raise ValueError(f"password_env must be an environment variable name, got {self.password_env!r}")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout must be positive, got {self.timeout}")
        object.__setattr__(self, "ssh_options", tuple(self.ssh_options))
        object.__setattr__(self, "scp_command", tuple(self.scp_command))


def _shown(version: tuple[int, int] | None) -> str:
    return "of unknown version" if version is None else f"OpenSSH {version[0]}.{version[1]}"


def scp_argv(
    source: Endpoint, destination: Endpoint, options: ScpOptions, version: tuple[int, int] | None
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
        argv += ["-o", f'UserKnownHostsFile="{Path(options.known_hosts).as_posix()}"']
    if options.identity is not None:
        argv += ["-i", str(options.identity), "-o", "IdentitiesOnly=yes"]
    if options.port is not None:
        argv += ["-P", str(options.port)]
    if options.protocol == "scp" and version is not None and version >= (9, 0):
        argv.append("-O")
    if both:
        argv.append("-3")
    return [*argv, "--", source.scp_arg(), destination.scp_arg()]


def _askpass_python() -> str:
    """A console Python for the askpass launcher (pythonw.exe has no usable stdout of its own)."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe" and executable.with_name("python.exe").exists():
        return str(executable.with_name("python.exe"))
    return str(executable)


def write_askpass(directory: Path, variable: str) -> Path:
    """Write the SSH_ASKPASS launcher: it runs `python -m keepwatch.askpass VARIABLE` (no secret in the file)."""
    if not _VARIABLE.fullmatch(variable):
        raise ValueError(f"not an environment variable name: {variable!r}")
    if platform.IS_WINDOWS:
        path = directory / "askpass.cmd"
        path.write_text(f'@echo off\r\n"{_askpass_python()}" -m keepwatch.askpass {variable} %*\r\n', encoding="utf-8")
        return path
    path = directory / "askpass.sh"
    path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(_askpass_python())} -m keepwatch.askpass {variable} "$@"\n', encoding="utf-8"
    )
    path.chmod(0o700)
    return path


def _run(argv: list[str], env: dict[str, str], timeout: float | None) -> tuple[int | None, str, str, bool]:
    try:
        process = platform.start_process(
            argv, cwd=None, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        raise TransferFailed(f"cannot run {argv[0]}: {exc.strerror or exc} (is OpenSSH's scp installed?)") from exc
    timed_out = False
    try:
        try:
            out, err = process.popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            out, err = process.popen.communicate()
    finally:
        process.close()
    return process.popen.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out


def _endpoint(value: str | os.PathLike[str] | Endpoint) -> Endpoint:
    if isinstance(value, Endpoint):
        return value
    if isinstance(value, os.PathLike):
        return Endpoint(path=os.fspath(value))
    return parse_endpoint(value)


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
    env = dict(os.environ)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="keepwatch-scp-") as work:
        if options.password_env:
            if options.password_env not in os.environ:
                raise TransferFailed(
                    f"password_env names {options.password_env}, which is not set in keepwatch's environment "
                    "(on Windows: setx; for the service, restart it after setting the variable)"
                )
            env["SSH_ASKPASS"] = str(write_askpass(Path(work), options.password_env))
            env["SSH_ASKPASS_REQUIRE"] = "force"
        returncode, stdout, stderr, timed_out = _run(argv, env, options.timeout)
    if report is not None:
        report(argv, returncode, timed_out, time.monotonic() - started, stdout, stderr)
    if timed_out:
        limit = format_duration(options.timeout or 0)
        raise TransferFailed(f"scp timed out after {limit}: {src.scp_arg()} -> {dst.scp_arg()}")
    if returncode != 0:
        raise CommandFailed(argv, returncode if returncode is not None else -1, stderr)


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _free_name(directory: Path, name: str) -> Path:
    """name-1.ext, name-2.ext, …: the first one not taken (a.tar.gz -> a-1.tar.gz)."""
    head, dot, tail = name.partition(".") if not name.startswith(".") else (name, "", "")
    for number in itertools.count(1):
        candidate = directory / (f"{head}-{number}.{tail}" if dot else f"{head}-{number}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def pull(
    remote: str | Endpoint,
    local_dir: str | os.PathLike[str],
    *,
    size: int | None = None,
    sha256: str | None = None,
    on_conflict: str = "skip-identical",
    options: ScpOptions = ScpOptions(),
    report: Report | None = None,
) -> Path:
    """Copy a remote file into local_dir through `.NAME.part`, verify size and sha256 when given, rename it."""
    src = _endpoint(remote)
    if not src.remote:
        raise ValueError(f"pull needs a remote source (user@host:path), got {src.scp_arg()!r}")
    if on_conflict not in CONFLICTS:
        raise ValueError(f"on_conflict must be one of {', '.join(CONFLICTS)}, got {on_conflict!r}")
    name = src.name
    if not name or name in (".", ".."):
        raise ValueError(f"{src.scp_arg()!r} does not name a file")
    directory = Path(local_dir)
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / name
    if final.exists():
        if on_conflict == "skip-identical":
            if sha256 is not None and sha256_file(final) == sha256.lower():
                return final
            raise TransferFailed(
                f"{final} already exists with other or unknown content; pass sha256 to skip identical files, or "
                "on_conflict='rename' or 'overwrite'"
            )
        if on_conflict == "rename":
            final = _free_name(directory, name)
    part = directory / f".{name}.part"
    part.unlink(missing_ok=True)
    try:
        copy(src, Endpoint(path=str(part)), options=options, report=report)
        actual_size = part.stat().st_size
        if size is not None and actual_size != size:
            raise TransferFailed(f"pulled {actual_size} bytes of {src.scp_arg()}, expected {size}")
        if sha256 is not None:
            actual = sha256_file(part)
            if actual != sha256.lower():
                raise TransferFailed(f"sha256 of the pulled {src.scp_arg()} is {actual}, expected {sha256.lower()}")
        platform.replace(part, final)
    finally:
        part.unlink(missing_ok=True)
    return final


def push(
    path: str | os.PathLike[str],
    remote_dir: str | Endpoint,
    *,
    marker: str = "sha256",
    options: ScpOptions = ScpOptions(),
    report: Report | None = None,
) -> str:
    """Upload a file into remote_dir, then (marker="sha256") NAME.sha256 in sha256sum format. Returns the remote path."""
    local = Path(path)
    if not local.is_file():
        raise TransferFailed(f"{local} is not a file")
    if marker not in MARKERS:
        raise ValueError(f"marker must be one of {', '.join(MARKERS)}, got {marker!r}")
    directory = _endpoint(remote_dir)
    if not directory.remote:
        raise ValueError(f"push needs a remote directory (user@host:dir), got {directory.scp_arg()!r}")
    target = directory.child(local.name)
    copy(local, target, options=options, report=report)
    if marker == "sha256":
        with tempfile.TemporaryDirectory(prefix="keepwatch-marker-") as work:
            marker_file = Path(work) / f"{local.name}.sha256"
            marker_file.write_bytes(f"{sha256_file(local)}  {local.name}\n".encode())
            copy(marker_file, directory.child(marker_file.name), options=options, report=report)
    return target.scp_arg()


def tcp_open(host: str, port: int = 22, timeout: float = 5.0) -> bool:
    """True when host:port accepts a TCP connection within `timeout` seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
