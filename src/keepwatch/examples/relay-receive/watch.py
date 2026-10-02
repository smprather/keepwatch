"""Handle each export once it has fully arrived and its .sha256 marker matched (see keepwatch docs relay).

Replace the body of on_true with your own processing; keep recording handled files in the ledger, because
events can arrive more than once.
"""

import shutil
from pathlib import Path


def check(ctx):
    handled = ctx.ledger("handled")
    new = [event for event in ctx.events if event.get("event") == "file" and event["sha256"] not in handled]
    return bool(new), [{"path": event["path"], "marker": event["marker"], "sha256": event["sha256"]} for event in new]


def on_true(ctx):
    handled = ctx.ledger("handled")
    done = Path(ctx.settings["done_dir"]).expanduser()
    done.mkdir(parents=True, exist_ok=True)
    for item in ctx.payload:
        if item["sha256"] in handled:
            continue
        for path in (item["path"], item["marker"]):
            if Path(path).exists():
                shutil.move(path, str(done / Path(path).name))
        handled.add(item["sha256"])
        ctx.log.info("received %s", Path(item["path"]).name, extra={"sha256": item["sha256"]})
