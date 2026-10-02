"""Checks `keepwatch install` makes before it registers this interpreter to start at login.

keepwatch never searches for a Python (PATH, `py`, `uv python find` can find the Microsoft Store placeholder or
an interpreter without keepwatch): it registers the one running it, and these checks make sure that one lasts
and works.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from keepwatch import __version__, platform, systemd, winsched


def uv_cache_dirs(env: Mapping[str, str]) -> list[Path]:
    """Where uv keeps its cache (uvx and `uv run --with` environments live there and get pruned)."""
    dirs = []
    if env.get("UV_CACHE_DIR"):
        dirs.append(Path(env["UV_CACHE_DIR"]))
    uv = shutil.which("uv", path=env.get("PATH"))
    if uv:
        try:
            completed = subprocess.run(
                [uv, "cache", "dir"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                creationflags=platform.NO_WINDOW,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                dirs.append(Path(completed.stdout.strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if platform.IS_WINDOWS:
        if env.get("LOCALAPPDATA"):
            dirs.append(Path(env["LOCALAPPDATA"]) / "uv" / "cache")
    else:
        cache_home = Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else Path(env.get("HOME", "~")).expanduser() / ".cache"
        dirs.append(cache_home / "uv")
    return dirs


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except OSError:
        return False


def transient_reason(prefix: Path, *, cache_dirs: Sequence[Path], temp_dir: Path) -> str | None:
    """Why the environment at `prefix` (sys.prefix) will not last, or None."""
    for cache in cache_dirs:
        if _inside(prefix, cache):
            return f"this keepwatch runs from uv's cache ({prefix}): uvx and `uv run --with` environments are pruned later"
    if _inside(prefix, temp_dir):
        return f"this keepwatch runs from the temporary directory ({prefix})"
    return None


def service_check_argv() -> list[str]:
    """The command the login service will run, asked for its version."""
    if platform.IS_WINDOWS:
        python = Path(winsched.pythonw())
        console = python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python
        return [str(console), "-m", "keepwatch", "--version"]
    return [systemd.find_executable(), "--version"]


def verify_command(argv: Sequence[str], env: Mapping[str, str]) -> str | None:
    """Run argv and require it to report this keepwatch's version. Returns the problem, or None."""
    shown = " ".join(argv)
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            env=dict(env) or None,
            creationflags=platform.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot run {shown}: {exc}"
    if completed.returncode == 0 and re.search(rf"(?<![\w.]){re.escape(__version__)}(?![\w.])", completed.stdout):
        return None
    output = " | ".join((completed.stdout + completed.stderr).strip().splitlines()[-3:]) or "no output"
    return f"{shown} exited {completed.returncode} without reporting keepwatch {__version__}: {output}"
