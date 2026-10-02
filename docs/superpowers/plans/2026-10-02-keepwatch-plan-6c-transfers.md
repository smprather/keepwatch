# keepwatch Plan 6c (Transfers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give hooks safe file transfers through the system's OpenSSH `scp`: `copy`, `pull` (to a hidden part file, verified by size and sha256, then renamed), `push` (then a `.sha256` completion marker), `tcp_open`, with passwords from an environment variable through `SSH_ASKPASS`, strict host keys, and logging. Python hooks get them as `ctx.transfer`; command hooks as `keepwatch kit …`.

**Architecture:** A new stdlib-only module `keepwatch/transfer.py` (it also runs inside plugin environments): endpoint parsing, OpenSSH version detection, the scp command line, an askpass launcher written to a private temp dir per call, and the four operations. `keepwatch/askpass.py` prints the password from a named environment variable, so the secret is never in a file, an argument or shell quoting. `Ctx.transfer` returns a `Transfer` bound to the hook (relative paths start at the watch directory, calls are logged as `command` records, the hook's remaining time caps `timeout`, a check may only use `tcp_open`). `keepwatch kit` is a command group exposing the same operations; the generated CLI reference learns to document subcommands.

**Tech Stack:** Python 3.12+, system OpenSSH scp; test dependency `asyncssh` (an in-process scp server with password and key auth).

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 4, and section 8 "Transfers"). Builds on Plans 6a-6b2 (implemented).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests, stop and report on plan defects, attribution trailer).
- `src/keepwatch/transfer.py` and `src/keepwatch/askpass.py` import only the standard library and keepwatch's stdlib-only modules (`platform`, `durations`, `ctx`); never `rich`, `click` or `watchdog` (they run inside plugin environments that lack them).
- Never put a password in an argument, a file or a log record. Only the *name* of the variable (`password_env`) travels.
- Facts established by the spike on 2026-10-02 (OpenSSH 10.5p1 against asyncssh), which the code relies on:
  - classic protocol needs `-O` on OpenSSH ≥ 9.0; `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` answers password and keyboard-interactive prompts; a password with spaces, quotes and `$` works;
  - wrong password, unknown host key and missing remote file each exit 1 with a clear stderr line;
  - **classic `scp -3 -O` exits 0 when the second host fails**; SFTP-mode `-3` exits 255. Hence two remote endpoints require `protocol = "sftp"` and keys;
  - asyncssh's SFTP server sends no exit status (scp exits 1 after a complete copy), so SFTP mode is only tested at the command-line level.

## Review Focus

- A wrong password must fail fast and once (no three attempts that could trip a lockout), and the error must say "Permission denied". Test: Task 2 `test_wrong_password_fails`, Task 1 `test_scp_argv_with_a_password`.
- A pull that fails verification must leave nothing behind that a later watch could take for a finished file. Test: Task 2 `test_pull_with_a_wrong_checksum_leaves_nothing`.
- A host key that is not already known must be refused, never accepted. Test: Task 2 `test_unknown_host_key_is_refused`.
- A check calling `ctx.transfer.pull` must fail with a clear message instead of changing things during `--dry-run`. Test: Task 3 `test_a_check_cannot_transfer`.
- `C:\data\in` and `C:/data/in` must be local paths, not host `C`. Test: Task 1 `test_parse_endpoint`.

---

### Task 1: Endpoints, the scp command line, askpass

**Files:**
- Create: `src/keepwatch/transfer.py`, `src/keepwatch/askpass.py`
- Test: `tests/test_transfer.py` (new)

**Interfaces:**
- Produces (all in `keepwatch.transfer`):
  - `TransferFailed(Exception)` — a transfer that cannot be attempted or verified (scp itself failing raises `keepwatch.CommandFailed`)
  - `Endpoint(path: str, host: str | None = None, user: str | None = None, port: int | None = None, uri: bool = False)` (frozen): `.remote`, `.name`, `.scp_arg()`, `.child(name)`
  - `parse_endpoint(text: str) -> Endpoint` (ValueError for malformed `scp://`)
  - `parse_openssh_version(text: str) -> tuple[int, int] | None`; `openssh_version(ssh: str = "ssh") -> tuple[int, int] | None` (cached)
  - `ScpOptions(protocol="scp", password_env=None, identity=None, known_hosts=None, port=None, ssh_options=(), timeout=None, scp_command=("scp",))` (frozen; ValueError for bad values)
  - `scp_argv(source: Endpoint, destination: Endpoint, options: ScpOptions, version: tuple[int, int] | None) -> list[str]`
  - `write_askpass(directory: Path, variable: str) -> Path`
  - constants `PROTOCOLS`, `CONFLICTS`, `MARKERS`, `SCP_DEFAULTS`
  - `keepwatch.askpass.main(argv: list[str]) -> int`

- [ ] **Step 1: Write the failing tests** — create `tests/test_transfer.py`:

```python
import os
import subprocess
from pathlib import Path

import pytest

from keepwatch import askpass
from keepwatch.transfer import (
    SCP_DEFAULTS,
    Endpoint,
    ScpOptions,
    TransferFailed,
    parse_endpoint,
    parse_openssh_version,
    scp_argv,
    write_askpass,
)

OPTS = list(SCP_DEFAULTS)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/data/a.gz", Endpoint(path="/data/a.gz")),
        ("a.gz", Endpoint(path="a.gz")),
        ("C:\\data\\a.gz", Endpoint(path="C:\\data\\a.gz")),
        ("C:/data/a.gz", Endpoint(path="C:/data/a.gz")),
        ("./odd:name", Endpoint(path="./odd:name")),
        ("me@linux1:/data/a.gz", Endpoint(path="/data/a.gz", host="linux1", user="me")),
        ("linux1:out/", Endpoint(path="out/", host="linux1")),
        ("me@linux1:", Endpoint(path="", host="linux1", user="me")),
        ("scp://me@linux2:2222/incoming/a.gz", Endpoint(path="incoming/a.gz", host="linux2", user="me", port=2222, uri=True)),
        ("scp://linux2/x", Endpoint(path="x", host="linux2", uri=True)),
    ],
)
def test_parse_endpoint(text, expected):
    endpoint = parse_endpoint(text)
    assert endpoint == expected
    assert endpoint.scp_arg() == text


def test_bad_endpoints():
    for text in ("", "scp://me@/x", "scp://host"):
        with pytest.raises(ValueError):
            parse_endpoint(text)


def test_endpoint_name_and_child():
    assert parse_endpoint("me@h:/data/a.tar.gz").name == "a.tar.gz"
    assert parse_endpoint("h:in/").child("a.gz").scp_arg() == "h:in/a.gz"
    assert parse_endpoint("h:").child("a.gz").scp_arg() == "h:a.gz"
    assert parse_endpoint("h:/").child("a.gz").scp_arg() == "h:/a.gz"
    assert parse_endpoint("scp://h:2222/in").child("a.gz").scp_arg() == "scp://h:2222/in/a.gz"
    assert parse_endpoint("out").child("a.gz").path == str(Path("out") / "a.gz")


def test_parse_openssh_version():
    assert parse_openssh_version("OpenSSH_10.5p1, OpenSSL 3.6.5 29 Sep 2026") == (10, 5)
    assert parse_openssh_version("OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2") == (9, 5)
    assert parse_openssh_version("OpenSSH_8.0p1, OpenSSL 1.1.1k  FIPS 25 Mar 2021") == (8, 0)
    assert parse_openssh_version("something else") is None


def argv_for(source, destination, version=(10, 5), **options):
    return scp_argv(parse_endpoint(source), parse_endpoint(destination), ScpOptions(**options), version)


def test_scp_argv_defaults():
    assert argv_for("a.gz", "me@h:in/") == ["scp", *OPTS, "-o", "BatchMode=yes", "-O", "--", "a.gz", "me@h:in/"]


def test_classic_protocol_needs_dash_o_only_on_openssh_9():
    assert "-O" not in argv_for("a.gz", "h:", version=(8, 9))
    assert "-O" not in argv_for("a.gz", "h:", version=None)


def test_scp_argv_with_a_password():
    argv = argv_for("a.gz", "h:", password_env="RELAY_PASSWORD")
    assert ["-o", "NumberOfPasswordPrompts=1"] == argv[len(OPTS) + 1 : len(OPTS) + 3]
    assert "BatchMode=yes" not in argv and "RELAY_PASSWORD" not in " ".join(argv)


def test_scp_argv_options_order():
    argv = argv_for(
        "a.gz", "h:", ssh_options=["-F", "cfg"], identity="k", port=2222, known_hosts="C:/my keys/kh"
    )
    assert argv == [
        "scp",
        "-F",
        "cfg",
        *OPTS,
        "-o",
        "BatchMode=yes",
        "-o",
        'UserKnownHostsFile="C:/my keys/kh"',
        "-i",
        "k",
        "-o",
        "IdentitiesOnly=yes",
        "-P",
        "2222",
        "-O",
        "--",
        "a.gz",
        "h:",
    ]


def test_sftp_protocol():
    assert "-O" not in argv_for("a.gz", "h:", protocol="sftp")
    with pytest.raises(TransferFailed, match="needs OpenSSH 9.0"):
        argv_for("a.gz", "h:", version=(8, 9), protocol="sftp")


def test_two_remote_endpoints_need_sftp_and_keys():
    with pytest.raises(TransferFailed, match="needs protocol = \"sftp\""):
        argv_for("a:x", "b:y")
    with pytest.raises(TransferFailed, match="key authentication"):
        argv_for("a:x", "b:y", protocol="sftp", password_env="PW")
    argv = argv_for("a:x", "b:y", protocol="sftp")
    assert "-3" in argv and "-O" not in argv


def test_a_transfer_needs_a_remote_endpoint():
    with pytest.raises(ValueError, match="at least one remote endpoint"):
        argv_for("a", "b")


def test_bad_options():
    for bad in ({"protocol": "ftp"}, {"password_env": "1BAD"}, {"port": 0}, {"timeout": 0}):
        with pytest.raises(ValueError):
            ScpOptions(**bad)


def test_askpass_prints_the_variable(monkeypatch, capfd):
    monkeypatch.setenv("KW_TEST_PW", "s3 cr'et\"$")
    assert askpass.main(["KW_TEST_PW", "u@h's password: "]) == 0
    assert capfd.readouterr().out == "s3 cr'et\"$\n"


def test_askpass_refuses_host_key_questions(monkeypatch, capfd):
    monkeypatch.setenv("KW_TEST_PW", "x")
    assert askpass.main(["KW_TEST_PW", "Are you sure you want to continue connecting (yes/no/[fingerprint])? "]) == 1
    captured = capfd.readouterr()
    assert captured.out == "" and "host keys must already be known" in captured.err


def test_askpass_without_the_variable(monkeypatch, capfd):
    monkeypatch.delenv("KW_TEST_PW", raising=False)
    assert askpass.main(["KW_TEST_PW", "password: "]) == 1
    assert "KW_TEST_PW is not set" in capfd.readouterr().err


def test_askpass_launcher_runs_the_helper(tmp_path):
    launcher = write_askpass(tmp_path, "KW_TEST_PW")
    env = {**os.environ, "KW_TEST_PW": "pa ss'\"$"}
    completed = subprocess.run([str(launcher), "u@h's password: "], env=env, capture_output=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.decode("utf-8").rstrip("\r\n") == "pa ss'\"$"
    assert "pa ss" not in launcher.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'keepwatch.transfer'`.

- [ ] **Step 3: Implement** — create `src/keepwatch/askpass.py`:

```python
"""SSH_ASKPASS helper: `python -m keepwatch.askpass VAR [prompt]` prints $VAR for ssh/scp. Stdlib only.

keepwatch points SSH_ASKPASS at a small launcher that runs this module with the *name* of the
environment variable holding the password, so the secret is never written to a file, passed as an
argument or quoted for a shell.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: python -m keepwatch.askpass VAR [prompt]\n")
        return 2
    name, prompt = argv[0], " ".join(argv[1:])
    if "yes/no" in prompt:
        sys.stderr.write(f"keepwatch askpass: not answering {prompt.strip()!r}: host keys must already be known\n")
        return 1
    value = os.environ.get(name)
    if value is None:
        sys.stderr.write(f"keepwatch askpass: the environment variable {name} is not set\n")
        return 1
    os.write(1, (value + "\n").encode("utf-8"))  # fd 1 also works under pythonw, where sys.stdout is None
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Create `src/keepwatch/transfer.py`:

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/transfer.py src/keepwatch/askpass.py tests/test_transfer.py
git commit -m "Transfers: endpoints, the scp command line, askpass"
```

---

### Task 2: copy, pull, push against a real scp

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add --dev asyncssh`)
- Create: `tests/scp_server.py` (test helper, not a test module)
- Test: `tests/test_transfer_scp.py` (new)

**Interfaces:**
- Consumes: Task 1.
- Produces: `tests/scp_server.ScpServer(root, workdir)` with `.start() -> ScpServer`, `.stop()`, `.port`, `.known_hosts` (Path), `.client_key` (Path), `scp_server.PASSWORD`, `scp_server.USER = "u"`.

- [ ] **Step 1: Add the test dependency**

Run: `uv add --dev asyncssh`
Expected: `asyncssh` appears in `[dependency-groups] dev` of `pyproject.toml`; `uv.lock` updated.

- [ ] **Step 2: Create the test server** — `tests/scp_server.py`:

```python
"""An scp server for tests (asyncssh, in a background thread): password and key auth, a pinned host key, one root."""

import asyncio
import os
import threading
from pathlib import Path

import asyncssh

USER = "u"
PASSWORD = "pa ss'\"$word"


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == USER and password == PASSWORD

    def public_key_auth_supported(self):
        return True


class ScpServer:
    def __init__(self, root: Path, workdir: Path) -> None:
        self.root = root
        self.client_key = workdir / "client_key"
        self.known_hosts = workdir / "known_hosts"
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "ScpServer":
        self._thread.start()
        if not self._ready.wait(60):
            raise RuntimeError("the test scp server did not start")
        if self._error is not None:
            raise self._error
        return self

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._listen())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _listen(self) -> None:
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key.write_private_key(str(self.client_key))
        if os.name != "nt":
            self.client_key.chmod(0o600)
        authorized = asyncssh.import_authorized_keys(client_key.export_public_key().decode())
        self._server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            server_factory=_Server,
            authorized_client_keys=authorized,
            allow_scp=True,
            sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(self.root)),
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.port} {host_key.export_public_key().decode().strip()}\n", encoding="utf-8"
        )

    def stop(self) -> None:
        def close() -> None:
            self._server.close()
            self._loop.stop()

        self._loop.call_soon_threadsafe(close)
        self._thread.join(10)
```

- [ ] **Step 3: Write the tests** — `tests/test_transfer_scp.py`:

```python
import hashlib
import shutil
import socket

import pytest

from keepwatch import CommandFailed
from keepwatch.transfer import ScpOptions, TransferFailed, pull, push, tcp_open
from scp_server import PASSWORD, ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")
REMOTE = "u@127.0.0.1:"


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


@pytest.fixture
def ssh_config(tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")  # keep the developer's ~/.ssh/config out of the tests
    return config


def password_options(server, ssh_config, monkeypatch, **extra):
    monkeypatch.setenv("KW_TEST_PASSWORD", PASSWORD)
    return ScpOptions(
        port=server.port,
        known_hosts=server.known_hosts,
        password_env="KW_TEST_PASSWORD",
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none", "-o", "PubkeyAuthentication=no"),
        timeout=60,
        **extra,
    )


def key_options(server, ssh_config, **extra):
    return ScpOptions(
        port=server.port,
        known_hosts=server.known_hosts,
        identity=server.client_key,
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none"),
        timeout=60,
        **extra,
    )


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_push_with_a_password_writes_the_file_and_its_marker(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    remote = push(local, REMOTE, options=password_options(server, ssh_config, monkeypatch))
    assert remote == "u@127.0.0.1:a.tar.gz"
    assert (server.root / "a.tar.gz").read_bytes() == b"payload"
    assert (server.root / "a.tar.gz.sha256").read_text(encoding="utf-8") == f"{sha(b'payload')}  a.tar.gz\n"


def test_push_without_a_marker(tmp_path, server, ssh_config):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    push(local, REMOTE, marker="none", options=key_options(server, ssh_config))
    assert sorted(path.name for path in server.root.iterdir()) == ["a.tar.gz"]


def test_pull_verifies_then_renames(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    final = pull(REMOTE + "b.gz", stage, size=11, sha256=sha(b"remote data"), options=key_options(server, ssh_config))
    assert final == stage / "b.gz" and final.read_bytes() == b"remote data"
    assert sorted(path.name for path in stage.iterdir()) == ["b.gz"]


def test_pull_with_a_wrong_checksum_leaves_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    with pytest.raises(TransferFailed, match="sha256"):
        pull(REMOTE + "b.gz", stage, sha256="0" * 64, options=key_options(server, ssh_config))
    assert list(stage.iterdir()) == []


def test_pull_with_a_wrong_size_leaves_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    with pytest.raises(TransferFailed, match="expected 99"):
        pull(REMOTE + "b.gz", stage, size=99, options=key_options(server, ssh_config))
    assert list(stage.iterdir()) == []


def test_pulling_an_identical_file_again_does_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    calls = []
    options = key_options(server, ssh_config)
    pull(REMOTE + "b.gz", stage, sha256=sha(b"remote data"), options=options, report=lambda *a: calls.append(a))
    pull(REMOTE + "b.gz", stage, sha256=sha(b"remote data"), options=options, report=lambda *a: calls.append(a))
    assert len(calls) == 1


def test_pull_conflicts(tmp_path, server, ssh_config):
    (server.root / "b.tar.gz").write_bytes(b"new")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "b.tar.gz").write_bytes(b"old")
    options = key_options(server, ssh_config)
    with pytest.raises(TransferFailed, match="already exists"):
        pull(REMOTE + "b.tar.gz", stage, options=options)
    assert pull(REMOTE + "b.tar.gz", stage, on_conflict="rename", options=options) == stage / "b-1.tar.gz"
    assert pull(REMOTE + "b.tar.gz", stage, on_conflict="overwrite", options=options) == stage / "b.tar.gz"
    assert (stage / "b.tar.gz").read_bytes() == b"new" and (stage / "b-1.tar.gz").read_bytes() == b"new"


def test_wrong_password_fails(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    options = password_options(server, ssh_config, monkeypatch)
    monkeypatch.setenv("KW_TEST_PASSWORD", "wrong")
    with pytest.raises(CommandFailed, match="Permission denied"):
        push(local, REMOTE, options=options)
    assert not (server.root / "a.gz").exists()


def test_unknown_host_key_is_refused(tmp_path, server, ssh_config):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    empty = tmp_path / "empty_known_hosts"
    empty.write_text("", encoding="utf-8")
    options = ScpOptions(
        port=server.port,
        known_hosts=empty,
        identity=server.client_key,
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none"),
        timeout=60,
    )
    with pytest.raises(CommandFailed, match="Host key verification failed"):
        push(local, REMOTE, options=options)


def test_missing_remote_file(tmp_path, server, ssh_config):
    with pytest.raises(CommandFailed):
        pull(REMOTE + "nothere.gz", tmp_path / "stage", options=key_options(server, ssh_config))
    assert list((tmp_path / "stage").iterdir()) == []


def test_unset_password_variable(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    options = password_options(server, ssh_config, monkeypatch)
    monkeypatch.delenv("KW_TEST_PASSWORD")
    with pytest.raises(TransferFailed, match="KW_TEST_PASSWORD, which is not set"):
        push(local, REMOTE, options=options)


def test_tcp_open(server):
    assert tcp_open("127.0.0.1", server.port, timeout=5) is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    assert tcp_open("127.0.0.1", closed, timeout=5) is False


def test_reports_each_scp_run(tmp_path, server, ssh_config):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    calls = []
    push(local, REMOTE, options=key_options(server, ssh_config), report=lambda *call: calls.append(call))
    assert len(calls) == 2  # the file, then its marker
    argv, returncode, timed_out, duration, stdout, stderr = calls[0]
    assert argv[0] == "scp" and returncode == 0 and timed_out is False and duration >= 0
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer_scp.py -q`
Expected: all pass (about 15 s). If a test fails, report scp's stderr from the failure; do not loosen assertions.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add pyproject.toml uv.lock tests/scp_server.py tests/test_transfer_scp.py
git commit -m "Transfers tested against an scp server: push, pull, passwords, host keys"
```

---

### Task 3: `ctx.transfer`

**Files:**
- Modify: `src/keepwatch/transfer.py` (add `Transfer`), `src/keepwatch/ctx.py` (`Ctx.transfer`, `Ctx._report_transfer`), `src/keepwatch/__init__.py` (export `TransferFailed`), `src/keepwatch/reference.py` (`ctx_topic`)
- Modify: `tests/test_docs.py` (`test_every_public_ctx_and_ledger_member_is_documented`)
- Test: `tests/test_ctx_transfer.py` (new)

**Interfaces:**
- Produces:
  - `transfer.Transfer(*, base: Path, report: Report | None = None, remaining: Callable[[], float] | None = None, writable: bool = True)` with `copy(source, destination, **options) -> None`, `pull(remote, local_dir, *, size=None, sha256=None, on_conflict="skip-identical", **options) -> Path`, `push(path, remote_dir, *, marker="sha256", **options) -> str`, `tcp_open(host, port=22, timeout=5.0) -> bool`; `**options` are `ScpOptions` fields
  - `Ctx.transfer` (property) returning a `Transfer` with `base = watch_dir`, `writable = hook != "check"`
  - `keepwatch.TransferFailed`

- [ ] **Step 1: Write the failing tests** — `tests/test_ctx_transfer.py`:

```python
import shutil
import time

import pytest

from keepwatch import Ctx, TransferFailed
from scp_server import ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def make_ctx(tmp_path, records, hook="on_true", deadline=None):
    return Ctx(
        watch="w",
        hook=hook,
        poll_id="p1",
        condition=True,
        payload=None,
        settings={},
        watch_dir=tmp_path,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        deadline=deadline or time.time() + 60,
        emit=records.append,
    )


def key_kwargs(server, tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return {
        "port": server.port,
        "known_hosts": server.known_hosts,
        "identity": server.client_key,
        "ssh_options": ("-F", str(config), "-o", "IdentityAgent=none"),
    }


def test_ctx_transfer_logs_like_ctx_run(tmp_path, server):
    (tmp_path / "a.gz").write_bytes(b"x")
    records = []
    ctx = make_ctx(tmp_path, records)
    assert ctx.transfer.push("a.gz", "u@127.0.0.1:", marker="none", **key_kwargs(server, tmp_path)) == "u@127.0.0.1:a.gz"
    [record] = records
    assert (record["type"], record["exit_code"], record["shell"], record["argv"][0]) == ("command", 0, False, "scp")
    assert (server.root / "a.gz").read_bytes() == b"x"


def test_relative_local_paths_start_at_the_watch_dir(tmp_path, server):
    (server.root / "b.gz").write_bytes(b"y")
    ctx = make_ctx(tmp_path, [])
    final = ctx.transfer.pull("u@127.0.0.1:b.gz", "stage", **key_kwargs(server, tmp_path))
    assert final == tmp_path / "stage" / "b.gz"


def test_a_check_cannot_transfer(tmp_path, server):
    ctx = make_ctx(tmp_path, [], hook="check")
    with pytest.raises(TransferFailed, match="a check must not transfer files"):
        ctx.transfer.pull("u@127.0.0.1:b.gz", "stage", **key_kwargs(server, tmp_path))
    assert ctx.transfer.tcp_open("127.0.0.1", server.port) is True


def test_the_hook_deadline_caps_the_timeout(tmp_path):
    ctx = make_ctx(tmp_path, [], deadline=time.time() - 1)
    with pytest.raises(TransferFailed, match="no time left"):
        ctx.transfer.copy("a.gz", "u@127.0.0.1:")


def test_unknown_option_names_are_errors(tmp_path):
    with pytest.raises(TypeError):
        make_ctx(tmp_path, []).transfer.copy("a.gz", "u@h:", pasword_env="X")
```

In `tests/test_docs.py`, `test_every_public_ctx_and_ledger_member_is_documented`: add `from keepwatch.transfer import Transfer` at the top of the test body and change the loop header to `for owner, prefix in ((Ctx, "ctx."), (Ledger, "ledger."), (Transfer, "ctx.transfer.")):`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ctx_transfer.py tests/test_docs.py -q`
Expected: FAIL — `ImportError: cannot import name 'TransferFailed' from 'keepwatch'`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/transfer.py`: add `from typing import Any` after `from pathlib import Path`, and append:

```python
class Transfer:
    """Transfers for hooks, as `ctx.transfer`: relative local paths start at the watch directory, every scp run
    is logged as a `command` record (like ctx.run), and the hook's remaining time caps `timeout`.

    Options (keyword arguments of copy, pull and push): protocol ("scp" or "sftp"), password_env (the name
    of an environment variable holding the password), identity (a private key file), known_hosts (a
    known_hosts file), port, ssh_options (extra scp arguments), timeout (seconds). A check may only use
    tcp_open: transferring files changes things, which a check must not do.
    """

    def __init__(
        self,
        *,
        base: Path,
        report: Report | None = None,
        remaining: Callable[[], float] | None = None,
        writable: bool = True,
    ) -> None:
        self._base = base
        self._report = report
        self._remaining = remaining
        self._writable = writable

    def _options(self, options: dict[str, Any]) -> ScpOptions:
        if not self._writable:
            raise TransferFailed("a check must not transfer files; do it in an action (ctx.transfer.tcp_open is fine)")
        timeout = options.pop("timeout", None)
        if self._remaining is not None:
            left = self._remaining()
            if left <= 0:
                raise TransferFailed("no time left in this hook for a transfer (raise action_timeout)")
            timeout = left if timeout is None else min(timeout, left)
        return ScpOptions(timeout=timeout, **options)

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
        **options: Any,
    ) -> Path:
        """Copy `remote` into local_dir through `.NAME.part`, verify size/sha256 when given, rename; returns the path.

        An existing file with the given sha256 makes this a no-op; any other existing file is an error unless
        on_conflict is "rename" (NAME-1.ext) or "overwrite".
        """
        scp = self._options(options)
        directory = Path(local_dir)
        if not directory.is_absolute():
            directory = self._base / directory
        return pull(remote, directory, size=size, sha256=sha256, on_conflict=on_conflict, options=scp, report=self._report)

    def push(self, path: str | os.PathLike[str], remote_dir: str, *, marker: str = "sha256", **options: Any) -> str:
        """Upload a file into remote_dir, then NAME.sha256 (sha256sum format) unless marker="none"; returns the remote path."""
        scp = self._options(options)
        local = Path(path)
        if not local.is_absolute():
            local = self._base / local
        return push(local, remote_dir, marker=marker, options=scp, report=self._report)

    def tcp_open(self, host: str, port: int = 22, timeout: float = 5.0) -> bool:
        """True when host:port accepts a TCP connection within `timeout` seconds (allowed in a check)."""
        return tcp_open(host, port, timeout)
```

(b) `src/keepwatch/ctx.py`: add to `Ctx` (after the `run_dir` property):

```python
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
```

and, for the annotation, add under the existing imports:

```python
if TYPE_CHECKING:
    from keepwatch.transfer import Transfer
```

changing `from typing import Any` to `from typing import TYPE_CHECKING, Any`. (`transfer.py` imports `CommandFailed` from `ctx.py`, so `ctx.py` must import `transfer` lazily.)

(c) `src/keepwatch/__init__.py`: after the `from keepwatch.ctx import …` line add

```python
from keepwatch.transfer import TransferFailed  # noqa: E402
```

and add `"TransferFailed"` to `__all__` (keep it sorted: after `"LedgerReadOnly"`, before `"Unknown"`).

(d) `src/keepwatch/reference.py`, `ctx_topic`: change the import to `from keepwatch.ctx import Ctx, Ledger` plus `from keepwatch.transfer import Transfer`, and before `return "\n".join(lines).rstrip()` add:

```python
    lines += ["## ctx.transfer", "", inspect.getdoc(Transfer) or "", ""]
    lines += _members(Transfer, "ctx.transfer.")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ctx_transfer.py tests/test_docs.py tests/test_ctx.py tests/test_runner_python.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/transfer.py src/keepwatch/ctx.py src/keepwatch/__init__.py src/keepwatch/reference.py tests/test_ctx_transfer.py tests/test_docs.py
git commit -m "ctx.transfer: logged, deadline-capped transfers for Python hooks"
```

---

### Task 4: `keepwatch kit` for command hooks

**Files:**
- Modify: `src/keepwatch/cli.py` (group `kit` with `copy`, `pull`, `push`, `tcp-open`; `COMMAND_GROUPS`), `src/keepwatch/reference.py` (`walk_commands`, `cli_topic` documents subcommands)
- Modify: `tests/test_docs.py` (`test_every_command_and_option_is_documented` walks subcommands)
- Test: `tests/test_cli_kit.py` (new)

**Interfaces:**
- Produces: `keepwatch kit copy SOURCE DESTINATION`, `keepwatch kit pull REMOTE LOCAL_DIR [--size N] [--sha256 HEX] [--on-conflict …]`, `keepwatch kit push PATH REMOTE_DIR [--marker sha256|none]`, all with `--protocol`, `--password-env VAR`, `--identity FILE`, `--known-hosts FILE`, `--port N`, `--ssh-option OPTION` (repeatable, becomes `-o OPTION`), `--timeout DURATION`; `keepwatch kit tcp-open HOST [--port 22] [--timeout 5s]`. `reference.walk_commands(group, parent, prefix) -> Iterator[tuple[str, click.Command, click.Context]]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_kit.py`:

```python
import shutil
import socket

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from scp_server import ScpServer

needs_scp = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


def run(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def common(server, tmp_path):
    return [
        "--port",
        str(server.port),
        "--known-hosts",
        str(server.known_hosts),
        "--identity",
        str(server.client_key),
        "--ssh-option",
        "IdentityAgent=none",
    ]


@needs_scp
def test_kit_push_then_pull(tmp_path, server, xdg):
    (tmp_path / "a.gz").write_bytes(b"x")
    pushed = run("kit", "push", str(tmp_path / "a.gz"), "u@127.0.0.1:", *common(server, tmp_path))
    assert pushed.exit_code == 0, pushed.output
    assert pushed.stdout.strip() == "u@127.0.0.1:a.gz"
    assert (server.root / "a.gz.sha256").exists()
    pulled = run("kit", "pull", "u@127.0.0.1:a.gz", str(tmp_path / "stage"), "--size", "1", *common(server, tmp_path))
    assert pulled.exit_code == 0, pulled.output
    assert pulled.stdout.strip() == str(tmp_path / "stage" / "a.gz")


@needs_scp
def test_kit_failure_exits_1_with_the_reason(tmp_path, server, xdg):
    result = run("kit", "pull", "u@127.0.0.1:nothere.gz", str(tmp_path / "stage"), *common(server, tmp_path))
    assert result.exit_code == 1
    assert "scp" in result.stderr


def test_kit_tcp_open(xdg):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        opened = run("kit", "tcp-open", "127.0.0.1", "--port", str(port))
    assert (opened.exit_code, opened.stdout.strip()) == (0, "open")
    closed = run("kit", "tcp-open", "127.0.0.1", "--port", str(port), "--timeout", "2s")
    assert (closed.exit_code, closed.stdout.strip()) == (1, "closed")


def test_kit_bad_usage(xdg, tmp_path):
    assert run("kit", "push", str(tmp_path / "a.gz"), "u@h:", "--protocol", "ftp").exit_code == 2
    assert run("kit", "copy", "a", "b").exit_code == 2  # no remote endpoint
    assert run("kit", "pull", "u@h:a.gz", "stage", "--timeout", "soon").exit_code == 2
```

In `tests/test_docs.py`, replace the body of `test_every_command_and_option_is_documented` with:

```python
    from keepwatch.cli import cli
    from keepwatch.reference import walk_commands

    text = render_topic("cli")
    root = click.Context(cli, info_name="keepwatch")
    names = []
    for name, command, _ in walk_commands(cli, root, "keepwatch"):
        names.append(name)
        assert command.help and command.help.strip(), name
        assert f"## {name}" in text
        for param in command.params:
            if isinstance(param, click.Option):
                assert param.help, f"{name} {param.opts}"
                assert param.opts[-1] in text
    assert "keepwatch kit pull" in names and "keepwatch run" in names
    assert "UNSET" not in text and "Sentinel" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_kit.py tests/test_docs.py -q`
Expected: FAIL — `No such command 'kit'`, `cannot import name 'walk_commands'`.

- [ ] **Step 3: Implement `src/keepwatch/reference.py`** — replace `cli_topic` with:

```python
def walk_commands(group: Any, parent: Any, prefix: str) -> Iterator[tuple[str, Any, Any]]:
    """(full name, command, context) for every command under `group`, depth first (subcommands of groups too)."""
    import click

    for name in group.list_commands(parent):
        command = group.get_command(parent, name)
        context = click.Context(command, info_name=name, parent=parent)
        full = f"{prefix} {name}"
        yield full, command, context
        if isinstance(command, click.Group):
            yield from walk_commands(command, context, full)


def cli_topic() -> str:
    import click

    from keepwatch.cli import cli

    root = click.Context(cli, info_name="keepwatch")
    lines = [narrative("cli"), "", "## keepwatch (global options)", ""]
    lines += _options(cli)
    for name, command, context in walk_commands(cli, root, "keepwatch"):
        usage = " ".join(command.collect_usage_pieces(context))
        lines += [f"## {name}", "", f"`{name} {usage}`", "", inspect.cleandoc(command.help or ""), ""]
        lines += _options(command)
    return "\n".join(lines).rstrip()
```

and change `from collections.abc import Callable` to `from collections.abc import Callable, Iterator`.

- [ ] **Step 4: Implement `src/keepwatch/cli.py`**

(a) Imports: add `from keepwatch import transfer` (extend the existing `from keepwatch import __version__, platform, systemd, winsched` to `from keepwatch import __version__, platform, systemd, transfer, winsched`) and `from keepwatch.ctx import CommandFailed`. `COMMAND_GROUPS`: add `{"name": "Hook tools", "commands": ["kit"]},` before the Reference group.

(b) Add before `def main()`:

```python
def _scp_options(function):
    """The scp options every `keepwatch kit` transfer accepts."""
    decorators = [
        click.option("--protocol", type=click.Choice(transfer.PROTOCOLS), default="scp", show_default=True,
                     help="scp: the classic protocol, which scp-only servers accept (adds -O on OpenSSH 9+). "
                     "sftp: needs OpenSSH 9+, and is required for two remote endpoints."),
        click.option("--password-env", metavar="VAR",
                     help="Name of the environment variable holding the password (given to scp through SSH_ASKPASS; "
                     "never pass the password itself). Default: key authentication only (BatchMode)."),
        click.option("--identity", type=click.Path(dir_okay=False), help="Private key file for ssh -i (only this key is offered)."),
        click.option("--known-hosts", type=click.Path(dir_okay=False),
                     help="known_hosts file to check the host key against. Unknown host keys are always refused."),
        click.option("--port", type=click.IntRange(1, 65535), help="ssh port for user@host:path endpoints (scp -P)."),
        click.option("--ssh-option", "ssh_options", multiple=True, metavar="OPTION",
                     help='An ssh option passed as -o OPTION, e.g. "ProxyJump=bastion" (repeatable).'),
        click.option("--timeout", "timeout_text", metavar="DURATION", help='Give up after this long, e.g. "10m". Default: no limit.'),
    ]
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _scp(protocol, password_env, identity, known_hosts, port, ssh_options, timeout_text) -> transfer.ScpOptions:
    timeout = None
    if timeout_text is not None:
        try:
            timeout = parse_duration(timeout_text)
        except DurationError as exc:
            raise click.BadParameter(str(exc), param_hint="--timeout") from None
    extra = tuple(item for option in ssh_options for item in ("-o", option))
    try:
        return transfer.ScpOptions(
            protocol=protocol,
            password_env=password_env,
            identity=identity,
            known_hosts=known_hosts,
            port=port,
            ssh_options=extra,
            timeout=timeout,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None


def _kit_run(action) -> None:
    try:
        result = action()
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    except (CommandFailed, transfer.TransferFailed) as exc:
        _fail(str(exc))
    if result is not None:
        click.echo(str(result))


@cli.group()
def kit() -> None:
    """Tools for command hooks: scp transfers and reachability checks (what Python hooks get as ctx.transfer).

    Each subcommand prints its result on stdout and, on failure, the reason on stderr. Endpoints are local
    paths, user@host:path or scp://user@host:port/path; host keys must already be known. Exit status: 0 on
    success, 1 on failure, 2 for bad usage. See: keepwatch docs transfers.
    """


@kit.command(name="copy")
@click.argument("source")
@click.argument("destination")
@_scp_options
def kit_copy(source: str, destination: str, **scp) -> None:
    """Copy one file from SOURCE to DESTINATION with scp (at least one of them remote). Prints nothing.

    Exit status: 0, 1 if scp failed, 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.copy(source, destination, options=options))


@kit.command(name="pull")
@click.argument("remote")
@click.argument("local_dir", type=click.Path(file_okay=False))
@click.option("--size", type=click.IntRange(0), help="Fail unless the pulled file has exactly this many bytes.")
@click.option("--sha256", metavar="HEX", help="Fail unless the pulled file has this sha256; also lets an identical existing file count as done.")
@click.option("--on-conflict", type=click.Choice(transfer.CONFLICTS), default="skip-identical", show_default=True,
              help="When LOCAL_DIR already has the name: skip-identical (no-op if --sha256 matches, else fail), "
              "rename (NAME-1.ext), overwrite.")
@_scp_options
def kit_pull(remote: str, local_dir: str, size: int | None, sha256: str | None, on_conflict: str, **scp) -> None:
    """Copy REMOTE into LOCAL_DIR through a hidden .NAME.part file, verify it, then rename it. Prints the final path.

    Exit status: 0, 1 if the copy or a check failed (nothing is left behind), 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.pull(remote, local_dir, size=size, sha256=sha256, on_conflict=on_conflict, options=options))


@kit.command(name="push")
@click.argument("path", type=click.Path(dir_okay=False))
@click.argument("remote_dir")
@click.option("--marker", type=click.Choice(transfer.MARKERS), default="sha256", show_default=True,
              help="After the file, upload NAME.sha256 (sha256sum format) as a completion marker, or none.")
@_scp_options
def kit_push(path: str, remote_dir: str, marker: str, **scp) -> None:
    """Upload PATH into REMOTE_DIR (user@host:dir), then its .sha256 marker. Prints the remote path.

    Exit status: 0, 1 if scp failed, 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.push(path, remote_dir, marker=marker, options=options))


@kit.command(name="tcp-open")
@click.argument("host")
@click.option("--port", type=click.IntRange(1, 65535), default=22, show_default=True, help="TCP port to try.")
@click.option("--timeout", "timeout_text", default="5s", show_default=True, metavar="DURATION", help="How long to wait for the connection.")
def kit_tcp_open(host: str, port: int, timeout_text: str) -> None:
    """Check whether HOST accepts TCP connections on --port. Prints open or closed.

    Exit status: 0 if open, 1 if closed or unreachable, 2 for bad usage.
    """
    try:
        timeout = parse_duration(timeout_text)
    except DurationError as exc:
        raise click.BadParameter(str(exc), param_hint="--timeout") from None
    opened = transfer.tcp_open(host, port, timeout)
    click.echo("open" if opened else "closed")
    raise SystemExit(0 if opened else 1)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_cli_kit.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the four files. Then:

```bash
git add src/keepwatch/cli.py src/keepwatch/reference.py tests/test_cli_kit.py tests/test_docs.py
git commit -m "keepwatch kit: transfers and tcp-open for command hooks; subcommands in the CLI reference"
```

---

### Task 5: Documentation

**Files:**
- Create: `src/keepwatch/reference/transfers.md`
- Modify: `src/keepwatch/reference.py` (`TOPICS`), `src/keepwatch/reference/executables.md`, `src/keepwatch/reference/agent.md`
- Test: `tests/test_docs.py` (`KEY_FACTS`)

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`, `KEY_FACTS`, add:

```python
    "transfers": ["ctx.transfer", "keepwatch kit", "password_env", "SSH_ASKPASS", "setx", "known_hosts", "-O", "scp -3", "protocol = \"sftp\"", ".part", ".sha256", "sha256sum -c", "tcp_open", "TransferFailed", "CommandFailed", "NumberOfPasswordPrompts"],
```

and append `"keepwatch kit"` to `KEY_FACTS["executables"]`.

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — no topic `transfers`.

- [ ] **Step 2: Write the docs**

(a) `src/keepwatch/reference.py`, `TOPICS`: insert after the `("observers", ...)` entry:

```python
    ("transfers", "scp transfers for hooks: ctx.transfer, keepwatch kit, passwords, host keys, markers"),
```

(b) Create `src/keepwatch/reference/transfers.md`:

````markdown
# Transfers: scp for hooks

Moving files between machines is what many watches do. keepwatch gives hooks one safe way to do it, built on the system's OpenSSH `scp` (part of Windows 10/11 and every Linux): **Python hooks** use `ctx.transfer`, **command hooks** use `keepwatch kit`. Every scp run is logged as a `command` record, like `ctx.run`.

```python
def on_true(ctx):
    done = ctx.ledger("pushed")
    for path in ctx.payload:
        ctx.transfer.push(path, "me@linux2:incoming/", password_env="RELAY_PASSWORD")
        done.add(ctx.file_key(path))
```

```sh
keepwatch kit push "$FILE" me@linux2:incoming/ --password-env RELAY_PASSWORD
```

## Endpoints

- A local path (`C:\data\out\a.gz` and `C:/data/out/a.gz` are local: a drive letter is never a host). In `ctx.transfer`, relative paths start at the watch directory.
- `[user@]host:path` (a colon before any slash): `path` is relative to the remote home unless it starts with `/`. `host` may be a Host alias from `~/.ssh/config`.
- `scp://[user@]host[:port]/path` when you need a port inside the endpoint.

At least one side must be remote. **Two remote endpoints** (copying linux1 → linux2 through this machine with `scp -3`) need `protocol = "sftp"` on both hosts and key authentication: with the classic protocol, `scp -3` reports success even when the destination host fails, so keepwatch refuses it. To an scp-only destination, copy through a local folder instead (pull, then push), which is also what lets a destination that is often offline catch up later.

## Operations

| Python | Command hook | What it does |
|---|---|---|
| `ctx.transfer.copy(src, dst)` | `keepwatch kit copy SRC DST` | One scp; nothing else. |
| `ctx.transfer.pull(remote, local_dir, size=, sha256=, on_conflict=)` | `keepwatch kit pull REMOTE DIR [--size N] [--sha256 HEX] [--on-conflict …]` | Copies to `DIR/.NAME.part`, checks size and sha256 when given, then renames to `DIR/NAME`. A failed check deletes the part file, so a half or wrong file never appears under its real name. Returns / prints the final path. |
| `ctx.transfer.push(path, remote_dir, marker=)` | `keepwatch kit push PATH REMOTE_DIR [--marker sha256\|none]` | Uploads `NAME`, then `NAME.sha256` in `sha256sum` format. Returns / prints the remote path. |
| `ctx.transfer.tcp_open(host, port=22, timeout=5)` | `keepwatch kit tcp-open HOST [--port N]` | Whether the port accepts a TCP connection (exit 0 open, 1 closed). Allowed in a check. |

`pull` when `DIR/NAME` already exists: with `sha256` given and matching, nothing is copied (an earlier pull finished); otherwise it fails unless `on_conflict = "rename"` (`NAME-1.ext`) or `"overwrite"`.

**The `.sha256` marker** exists for destinations where uploads cannot be renamed into place (upload-only accounts): the data arrives first, the marker last. A consumer there waits for `NAME.sha256` and runs `sha256sum -c NAME.sha256` before using `NAME`.

## Options

Keyword arguments of `copy`, `pull` and `push` (`--option` for `keepwatch kit`):

| Option | Meaning |
|---|---|
| `password_env` | Name of the environment variable holding the password. Default: keys only. |
| `identity` | A private key file (`ssh -i`); ssh then offers only that key. |
| `known_hosts` | A known_hosts file to check host keys against (default: ssh's own, `~/.ssh/known_hosts`). |
| `port` | ssh port for `user@host:path` endpoints. |
| `protocol` | `"scp"` (default): the classic protocol, which scp-only servers accept; keepwatch adds `-O` on OpenSSH 9 and newer, where scp otherwise speaks SFTP. `"sftp"`: needs OpenSSH 9+. |
| `ssh_options` | Extra scp arguments, placed before keepwatch's own (ssh keeps the first value given), e.g. `["-o", "ProxyJump=bastion"]`. |
| `timeout` | Give up after this many seconds. In `ctx.transfer` it never exceeds the hook's remaining time. |

Always set: `StrictHostKeyChecking=yes` (**host keys must already be known**: connect once by hand, `ssh me@host true`, or pass `known_hosts`), `ConnectTimeout=15`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`, and `BatchMode=yes` without a password (nothing can prompt), or `NumberOfPasswordPrompts=1` with one (a wrong password fails once, instead of three times that could trip a lockout such as fail2ban).

## Passwords

Never put a password in config.toml, a hook or a command line. Put it in an environment variable that the keepwatch service sees, and name the variable in `password_env`:

- **Windows:** `setx RELAY_PASSWORD "…"` (for your user), then restart the keepwatch logon task (`keepwatch stop`, then start it from Task Scheduler, or log off and on): processes only see variables that existed when they started.
- **Linux:** `Environment=` in a systemd drop-in (`systemctl --user edit keepwatch`), or the global `[environment]` table of keepwatch's config if that file is private to you.

keepwatch hands it to scp through `SSH_ASKPASS` (with `SSH_ASKPASS_REQUIRE=force`): a small launcher in a private temporary directory runs `python -m keepwatch.askpass VAR`, which prints the variable's value. The password is never written to a file, passed as an argument or logged; only the variable's name is. The helper refuses to answer host-key questions ("yes/no"). Password and keyboard-interactive logins both work.

## Errors

- scp failing (wrong password: "Permission denied"; unknown host key: "Host key verification failed"; missing file; network) raises `keepwatch.CommandFailed` with scp's last stderr lines, and `keepwatch kit` exits 1 with them on stderr.
- Anything keepwatch refuses or a failed check (sha256, size, conflict, unset password variable, a check trying to transfer, no time left) raises `keepwatch.TransferFailed` (exit 1 for `keepwatch kit`).
- An action that raises fails the poll: the watch backs off and retries, and observer events stay queued until a poll succeeds.
````

(c) `src/keepwatch/reference/executables.md`: after the paragraph `Python hooks get the same variables, so programs they start can use them too.`, add:

```markdown

Command hooks transfer files with `keepwatch kit` (`copy`, `pull`, `push`, `tcp-open`): the same scp transfers Python hooks get as `ctx.transfer`, with the same checks and logging. See `keepwatch docs transfers`.
```

(d) `src/keepwatch/reference/agent.md`, "Common mistakes": add at the end:

```markdown
- Calling `scp` or `sftp` yourself, or putting a password in a hook. Use `ctx.transfer` / `keepwatch kit` with `password_env`: host keys are checked, nothing can prompt, partial pulls never appear under the real name, and the password stays in the environment. See `keepwatch docs transfers`.
```

- [ ] **Step 3: Run the docs tests and read the topic**

Run: `uv run pytest tests/test_docs.py -q`, then `uv run keepwatch docs transfers` and `uv run keepwatch docs cli` (check the `keepwatch kit pull` section is there).
Expected: tests pass; both render.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the five files. Then:

```bash
git add src/keepwatch/reference.py src/keepwatch/reference/transfers.md src/keepwatch/reference/executables.md src/keepwatch/reference/agent.md tests/test_docs.py
git commit -m "Document transfers"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
