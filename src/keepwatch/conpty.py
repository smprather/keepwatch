"""Run a command under a Windows pseudo-console and type a password at its prompt.

Some servers reject a password delivered through `SSH_ASKPASS` while accepting the same bytes typed at a
console (mod_sftp among them — see `keepwatch docs transfers`), so `password_mode = "conpty"` runs scp through
ConPTY and types it. This module is imported only on that path: pywinpty is an optional extra, and nothing here
is needed anywhere else (the rest of keepwatch is standard library only).
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Mapping, Sequence
from importlib import import_module

# What scp, ssh and keyboard-interactive prompts look like: "... password:", "Password for user@host:".
PROMPT = re.compile(r"password[^:\n]*:\s*$", re.IGNORECASE | re.MULTILINE)
READ_SIZE = 4096
POLL = 0.01


def scrub(text: str, password: str) -> str:
    """`text` with the password removed: ssh turns the tty's echo off, but a pty is not always polite."""
    if not password:
        return text
    return text.replace(password, "***")


def _spawn(argv: Sequence[str], env: Mapping[str, str]):
    """The pty process, or a clear error naming the extra to install (never an ImportError in a hook's log)."""
    try:
        winpty = import_module("winpty")  # pywinpty: a Windows-only optional extra
    except ImportError as exc:  # pragma: no cover - exercised with a fake module in the tests
        raise ImportError(
            "password_mode = \"conpty\" needs pywinpty: install keepwatch with the extra, e.g. "
            "uv tool install 'keepwatch[conpty]' (or pip install pywinpty)"
        ) from exc
    return winpty.PtyProcess.spawn(list(argv), env=dict(env), dimensions=(24, 200))


def run(
    argv: Sequence[str],
    password: str,
    *,
    limit: float | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int | None, str, str, bool]:
    """Run argv under a pty, type the password at its prompt, and return (exit, stdout, stderr, timed_out).

    The pty merges the command's stderr into stdout, so stderr is always empty. Nothing read back ever contains
    the password: it is scrubbed anyway. `limit` is a whole-run timeout in seconds (None: wait forever); a run
    that has to be killed for it comes back with timed_out=True, like `transfer._run`.
    """
    process = _spawn(argv, env if env is not None else os.environ)
    collected: list[str] = []
    deadline = None if limit is None else time.monotonic() + limit
    typed = False
    timed_out = False
    try:
        while True:
            try:
                chunk = process.read(READ_SIZE)
            except EOFError:
                chunk = ""
            if chunk:
                collected.append(chunk)
                if not typed and PROMPT.search("".join(collected)):
                    process.write(password + "\r")
                    typed = True
            if not process.isalive():
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                process.terminate(force=True)
                break
            time.sleep(POLL)  # pi-lens-ignore: python-sleep-in-test -- the poll loop of a pty reader, not a test
    finally:
        for closer in ("cancel", "close"):
            method = getattr(process, closer, None)  # winpty's API differs a little between versions
            if method is not None:
                with contextlib.suppress(Exception):  # best effort: winpty's API differs between versions
                    method()
    output = scrub("".join(collected), password)
    exit_code = None if timed_out else process.exitstatus
    return exit_code, output, "", timed_out
