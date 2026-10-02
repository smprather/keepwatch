"""Handle each export once it has fully arrived and its .sha256 marker matched (see keepwatch docs relay).

Replace the body of on_true with your own processing; keep recording handled files in the ledger, because
events can arrive more than once.
"""

import shutil
from pathlib import Path


def check(ctx):
    handled = ctx.ledger("handled")
    new = [event for event in ctx.events if event.get("event") == "file" and key(event) not in handled]
    return bool(new), [{"path": event["path"], "marker": event["marker"], "size": event["size"], "mtime": event["mtime"], "sha256": event["sha256"]} for event in new]


def on_true(ctx):
    handled = ctx.ledger("handled")
    done = Path(ctx.settings["done_dir"]).expanduser()
    done.mkdir(parents=True, exist_ok=True)
    for item in ctx.payload:
        if key(item) in handled:
            continue
        try:
            info = Path(item["path"]).stat()
        except OSError:
            continue  # moved or deleted since it was reported
        if (info.st_size, info.st_mtime) != (item["size"], item["mtime"]):
            ctx.log.warning("%s changed since it was reported; left for its next report", Path(item["path"]).name)
            continue
        for path in (item["path"], item["marker"]):
            if Path(path).exists():
                shutil.move(path, str(done / Path(path).name))
        handled.add(key(item))
        ctx.log.info("received %s", Path(item["path"]).name, extra={"sha256": item["sha256"]})


def key(event):
    """One arrival: path, size, mtime and content (the same bytes arriving again later is a new arrival)."""
    return f"{event['path']}|{event['size']}|{event['mtime']!r}|{event['sha256']}"
