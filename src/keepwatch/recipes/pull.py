"""recipe = "pull": copy settled files from a remote directory into a local staging folder.

The `remote_files` observer reports settled remote files (with their sha256). check() answers TRUE with the
files not yet pulled; on_true() pulls each one (verified) and records it in the ledger "pulled", which also tells
the observer which files to skip when it reconnects.

With delete_remote, each verified file is queued in "to_delete" (with the sha256 it was verified with) and
deleted from the source host by keepwatch's remote helper, which first checks that it is still exactly that
file. Deleted or gone keys leave "pulled" for "skipped" (forgotten after 7 days; stale events for them are ignored); changed ones just leave "pulled" (the new version is pulled when reported); refused ones go to "kept". Files pulled while delete_remote was off have no recorded sha256 and are never deleted (an entry without one goes to "kept"). See keepwatch docs recipes.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from keepwatch.ctx import CommandFailed, Ctx, as_text
from keepwatch.recipes import file_budget
from keepwatch.remote import ssh_argv, watcher_source
from keepwatch.transfer import TransferFailed, TransferMismatch, with_known_hosts

LEDGER = "pulled"
TO_DELETE = "to_delete"
SKIPPED = "skipped"
KEPT = "kept"
SKIP_FOR = "7d"
UNKNOWN_SHA = "-"


def event_key(event: dict[str, Any]) -> str:
    """The ledger key of a remote file: "<path>|<size>|<mtime!r>" (the remote watcher builds the same text)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}"


def transfer_options(settings: Mapping[str, Any]) -> dict[str, Any]:
    options = {name: settings[name] for name in ("port", "identity", "known_hosts") if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def queue_key(key: str, sha256: str | None) -> str:
    """A to_delete entry: the pulled file's key plus the sha256 it was verified with ("-" when unknown)."""
    return f"{key}|{sha256 or UNKNOWN_SHA}"


def split_queue_key(entry: str) -> tuple[str, dict[str, Any]]:
    """(the pulled key, the helper's {"path", "size", "mtime", "sha256"?}) for a to_delete entry."""
    key, sha256 = entry.rsplit("|", 1)
    path, size, mtime = key.rsplit("|", 2)
    item: dict[str, Any] = {"path": path, "size": int(size), "mtime": float(mtime)}
    if sha256 != UNKNOWN_SHA:
        item["sha256"] = sha256
    return key, item


def due_deletions(ctx: Ctx) -> list[str]:
    """Queued deletions due for an attempt (only files pulled and verified while delete_remote was on are queued)."""
    settings = ctx.settings
    if not settings["delete_remote"]:
        return []
    pending = ctx.ledger(TO_DELETE)
    now = datetime.now(timezone.utc)
    return [entry for entry in pending
            if (now - cast(datetime, pending.added_at(entry))).total_seconds() >= settings["delete_retry"]]


def delete_remote(ctx: Ctx, entries: list[str]) -> None:
    """Delete these pulled files from the source host in one ssh call; problems are logged and retried."""
    if not entries:
        return
    settings = ctx.settings
    pending, pulled, kept = ctx.ledger(TO_DELETE), ctx.ledger(LEDGER), ctx.ledger(KEPT)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    remote = settings["remote"]
    queued = [(entry, *split_queue_key(entry)) for entry in entries]
    for entry, key, item in queued:
        if "sha256" not in item:
            pending.discard(entry)
            kept.add(key)
            name = item["path"].rsplit("/", 1)[-1]
            ctx.log.warning("not deleting %s on %s: no verified sha256 was recorded for it (delete it by hand)", name, remote)
    queued = [(entry, key, item) for entry, key, item in queued if "sha256" in item]
    if not queued:
        return
    argv = ssh_argv(
        remote,
        port=settings["port"],
        identity=settings["identity"],
        ssh_options=with_known_hosts(settings["ssh_options"], settings["known_hosts"]),
        ssh_command=settings["ssh_command"],
        remote_python=settings["remote_python"],
    )
    options = {"mode": "delete", "dir": settings["remote_dir"], "files": [item for _, _, item in queued]}
    try:
        completed = ctx.run(argv, input=watcher_source(options), check=False)
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        # Keep what the helper reported before the deadline: files it already deleted are not retried.
        stdout = as_text(exc.stdout)
        stderr = as_text(exc.stderr) + "\ntimed out (for large files raise action_timeout: the helper re-reads each one)"
    except OSError as exc:
        stdout, stderr = "", str(exc)
    results: dict[int, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("index"), int):
            results[message["index"]] = message
    for index, (entry, key, item) in enumerate(queued):
        name = item["path"].rsplit("/", 1)[-1]
        result = results.get(index)
        event = result["event"] if result else None
        if event in ("deleted", "gone"):
            pending.discard(entry)
            pulled.discard(key)
            skipped.add(key)
            if event == "deleted":
                ctx.log.info("deleted %s from %s", name, remote)
            else:
                ctx.log.info("%s was already gone from %s", name, remote)
        elif event == "changed":
            pending.discard(entry)
            pulled.discard(key)  # not skipped: the new content is pulled when it is reported
            ctx.log.warning("%s changed on %s since it was pulled; kept there", name, remote)
        elif event == "refused":
            pending.discard(entry)
            kept.add(key)
            reason = (result or {}).get("reason")
            ctx.log.warning("not deleting %s on %s: %s", name, remote, reason)
        else:
            pending.add(entry)  # refreshes its stamp: retried after delete_retry
            if result is not None:
                reason = result.get("reason")
            else:
                reason = " | ".join(stderr.strip().splitlines()[-3:]) or "no answer"
            ctx.log.warning("could not delete %s from %s (will retry): %s", name, remote, reason)


def check(ctx: Ctx) -> tuple[bool, list[dict[str, Any]]]:
    pulled = ctx.ledger(LEDGER)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    new: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in ctx.events:
        if event.get("event") != "file":
            continue
        key = event_key(event)
        if key in pulled or key in skipped or key in seen:
            continue
        seen.add(key)
        new.append(
            {"path": event["path"], "name": event["name"], "size": event["size"], "sha256": event.get("sha256"), "key": key}
        )
    return bool(new) or bool(due_deletions(ctx)), new


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    skipped = ctx.ledger(SKIPPED, expire=SKIP_FOR)
    queue = ctx.ledger(TO_DELETE) if settings["delete_remote"] else None
    options = transfer_options(settings)
    fresh: list[str] = []
    error: Exception | None = None
    try:
        items = [item for item in ctx.payload if item["key"] not in pulled and item["key"] not in skipped]
        for index, item in enumerate(items):
            if ctx.remaining <= 0:
                error = error or TransferFailed("no time left for the rest of this queue (raise action_timeout)")
                break
            sha256 = item["sha256"] if settings["checksum"] else None
            # A fair share of what is left, so one slow file cannot starve the rest of the queue (`file_timeout`
            # caps it harder). A file that fails or runs out of its share is logged and the queue carries on.
            budget = file_budget(ctx.remaining, len(items) - index, settings["file_timeout"])
            try:
                final = ctx.transfer.pull(
                    f"{settings['remote']}:{item['path']}",
                    settings["local_dir"],
                    size=item["size"],
                    sha256=sha256,
                    on_conflict=settings["on_conflict"],
                    deadline=time.time() + budget,
                    **options,
                )
            except TransferMismatch as exc:
                # The file changed after it was reported; its new version comes as a new event.
                ctx.log.warning("%s changed on %s after it was reported (%s); not pulled", item["name"], settings["remote"], exc)
                continue
            except (TransferFailed, CommandFailed, OSError) as exc:
                error = error or exc
                ctx.log.warning("pulling %s from %s failed (%s); the rest of the queue is still tried",
                                item["name"], settings["remote"], exc)
                continue
            if queue is not None:  # queued first: a crash before the next line still gets the file deleted
                entry = queue_key(item["key"], sha256)
                queue.add(entry)
                fresh.append(entry)
            pulled.add(item["key"])
            ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
    except Exception as exc:  # deletes below still run for what was pulled; the error is raised after them
        error = error or exc
    if queue is not None and (fresh or error is None):
        entries = set(fresh) if error is not None else set(fresh) | set(due_deletions(ctx))
        try:
            delete_remote(ctx, sorted(entries))
        except Exception as exc:
            if error is None:
                raise
            ctx.log.warning("deleting from %s failed as well: %s", settings["remote"], exc)
    if error is not None:
        raise error
