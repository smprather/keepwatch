"""recipe = "pull": copy settled files from a remote directory into a local staging folder.

The `remote_files` observer reports settled remote files (with their sha256). check() answers TRUE with
the files not yet in the ledger "pulled"; on_true() pulls each one (verified) and records it. The same
ledger tells the observer which files to skip when it reconnects.
"""

from __future__ import annotations

from typing import Any

from keepwatch.ctx import Ctx

LEDGER = "pulled"


def event_key(event: dict[str, Any]) -> str:
    """The ledger key of a remote file: "<path>|<size>|<mtime!r>" (the remote watcher builds the same text)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}"


def transfer_options(settings: dict[str, Any]) -> dict[str, Any]:
    options = {name: settings[name] for name in ("port", "identity", "known_hosts") if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


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
    return bool(new), new


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    pulled = ctx.ledger(LEDGER)
    options = transfer_options(settings)
    for item in ctx.payload:
        if item["key"] in pulled:
            continue
        sha256 = item["sha256"] if settings["checksum"] else None
        final = ctx.transfer.pull(
            f"{settings['remote']}:{item['path']}", settings["local_dir"], size=item["size"], sha256=sha256, **options
        )
        pulled.add(item["key"])
        ctx.log.info("pulled %s", item["name"], extra={"local": str(final), "size": item["size"]})
