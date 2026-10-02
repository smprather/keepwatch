"""recipe = "pull": copy settled files from a remote directory into a local staging folder.

The `remote_files` observer reports settled remote files (with their sha256). check() answers TRUE with
the files not yet in the ledger "pulled"; on_true() pulls each one (verified) and records it. The same
ledger tells the observer which files to skip when it reconnects. With delete_remote, verified files are also
queued in the ledger "to_delete" and deleted from the source host by keepwatch's remote helper (see
keepwatch docs recipes).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any

from keepwatch.ctx import Ctx
from keepwatch.remote import ssh_argv, watcher_source
from keepwatch.transfer import known_hosts_option

LEDGER = "pulled"
TO_DELETE = "to_delete"


def event_key(event: dict[str, Any]) -> str:
    """The ledger key of a remote file: "<path>|<size>|<mtime!r>" (the remote watcher builds the same text)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    options = {name: settings[name] for name in ("port", "identity", "known_hosts") if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def split_key(key: str) -> dict[str, Any]:
    """A ledger key back into the helper's {"path", "size", "mtime"}."""
    path, size, mtime = key.rsplit("|", 2)
    return {"path": path, "size": int(size), "mtime": float(mtime)}


def due_deletions(ctx: Ctx) -> list[str]:
    """Queued deletions whose last attempt is at least delete_retry old."""
    settings = ctx.settings
    if not settings["delete_remote"]:
        return []
    pending = ctx.ledger(TO_DELETE)
    now = datetime.now(timezone.utc)
    return [key for key in pending if (now - pending.added_at(key)).total_seconds() >= settings["delete_retry"]]


def delete_remote(ctx: Ctx, keys: list[str]) -> None:
    """Delete these pulled files from the source host in one ssh call. Never raises: problems are logged and retried."""
    if not keys:
        return
    settings = ctx.settings
    pending = ctx.ledger(TO_DELETE)
    remote = settings["remote"]
    ssh_options = [*settings["ssh_options"], *(known_hosts_option(settings["known_hosts"]) if settings["known_hosts"] else [])]
    argv = ssh_argv(
        remote,
        port=settings["port"],
        identity=settings["identity"],
        ssh_options=ssh_options,
        ssh_command=settings["ssh_command"],
        remote_python=settings["remote_python"],
    )
    options = {"mode": "delete", "dir": settings["remote_dir"], "files": [split_key(key) for key in keys]}
    try:
        completed = ctx.run(argv, input=watcher_source(options), check=False)
        stdout, stderr = completed.stdout, completed.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        stdout, stderr = "", str(exc)
    results: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("path"), str):
            results[message["path"]] = message
    for key in keys:
        path = split_key(key)["path"]
        name = path.rsplit("/", 1)[-1]
        result = results.get(path)
        if result is None:
            pending.add(key)  # refreshes its stamp: retried after delete_retry
            tail = " | ".join(stderr.strip().splitlines()[-3:]) or "no answer"
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, tail)
        elif result["event"] == "deleted":
            pending.discard(key)
            ctx.log.info("deleted %s from %s", name, remote)
        elif result["event"] == "gone":
            pending.discard(key)
            ctx.log.info("%s was already gone from %s", name, remote)
        elif result["event"] in ("changed", "refused"):
            pending.discard(key)
            reason = result.get("reason") or "it is not the file that was pulled"
            if result["event"] == "changed":
                ctx.log.warning("%s changed on %s since it was pulled; kept there", name, remote)
            else:
                ctx.log.warning("not deleting %s on %s: %s", name, remote, reason)
        else:
            pending.add(key)
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, result.get("reason"))


def check(ctx: Ctx) -> tuple[bool, list[dict[str, Any]]]:
    pulled = ctx.ledger(LEDGER)
    new: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in ctx.events:
        if event.get("event") != "file":
            continue
        key = event_key(event)
        if key in pulled or key in seen:
            continue
        seen.add(key)
        new.append(
            {"path": event["path"], "name": event["name"], "size": event["size"], "sha256": event.get("sha256"), "key": key}
        )
    return bool(new) or bool(due_deletions(ctx)), new


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    queue = ctx.ledger(TO_DELETE) if settings["delete_remote"] else None
    options = transfer_options(settings)
    fresh: list[str] = []
    try:
        for item in ctx.payload:
            if item["key"] in pulled:
                continue
            sha256 = item["sha256"] if settings["checksum"] else None
            final = ctx.transfer.pull(
                f"{settings['remote']}:{item['path']}",
                settings["local_dir"],
                size=item["size"],
                sha256=sha256,
                on_conflict=settings["on_conflict"],
                **options,
            )
            pulled.add(item["key"])
            if queue is not None:
                queue.add(item["key"])
                fresh.append(item["key"])
            ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
    finally:
        if queue is not None:
            delete_remote(ctx, sorted(set(fresh) | set(due_deletions(ctx))))
