"""recipe = "push": upload settled files from a local folder when the destination is reachable.

check() probes the destination first: no answer means unknown (no failure, no backoff; files wait). Then
it answers TRUE with the settled files (rescanned here; the files observer only wakes the watch). on_true()
pushes each one with its .sha256 marker, then deletes, archives or keeps it (a ledger remembers kept files).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from pathlib import Path
from typing import Any

from keepwatch.ctx import Ctx, Unknown
from keepwatch.transfer import parse_endpoint

LEDGER = "pushed"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    names = ("protocol", "password_env", "port", "identity", "known_hosts")
    options = {name: settings[name] for name in names if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def reachable(settings: dict[str, Any]) -> tuple[str, int]:
    """The host and port to probe: reachable_host/port, else dest's host and port (else `port`, else 22)."""
    dest = parse_endpoint(settings["dest"])
    host = settings["reachable_host"] or dest.host
    port = settings["reachable_port"] or settings["port"] or dest.port or 22
    return str(host), int(port)


def settled_files(settings: dict[str, Any]) -> list[Path]:
    """Regular files in local_dir matching pattern, not ignore, last modified at least `settle` ago."""
    directory = Path(settings["local_dir"])
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    now = time.time()
    found = []
    for path in entries:
        name = path.name
        if not fnmatch.fnmatch(name, settings["pattern"]):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in settings["ignore"]):
            continue
        try:
            if path.is_file() and now - path.stat().st_mtime >= settings["settle"]:
                found.append(path)
        except OSError:
            continue
    return found


def check(ctx: Ctx) -> tuple[bool, list[str]]:
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; files wait in {settings['local_dir']}")
    pushed = ctx.ledger(LEDGER) if settings["after"] == "keep" else None
    files = []
    for path in settled_files(settings):
        try:
            if pushed is None or ctx.file_key(path) not in pushed:
                files.append(str(path))
        except OSError:
            continue  # removed since the scan
    return bool(files), files


def _archive(path: Path, directory: Path, keep_for: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    shutil.move(str(path), str(target))
    os.utime(target)  # the archive's own clock starts now
    cutoff = time.time() - keep_for
    for old in directory.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    options = transfer_options(settings)
    pushed = ctx.ledger(LEDGER)
    for name in ctx.payload:
        path = Path(name)
        if not path.is_file():
            continue  # moved or deleted since the check
        key = ctx.file_key(path)
        if settings["after"] == "keep" and key in pushed:
            continue
        remote = ctx.transfer.push(path, settings["dest"], marker=settings["marker"], **options)
        ctx.log.info("pushed %s", path.name, extra={"remote": remote})
        if settings["after"] == "delete":
            path.unlink()
        elif settings["after"] == "archive":
            _archive(path, Path(settings["archive_dir"]), settings["keep_for"])
        else:
            pushed.add(key)
