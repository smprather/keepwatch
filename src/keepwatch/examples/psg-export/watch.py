"""Send each finished PSG export to bar.com once.

The condition is "there are finished exports that have not been sent". It stays TRUE while sends fail
and when new files arrive, so the work happens in on_true (every poll while TRUE), not on_rise.
"""

from keepwatch import Ctx


def check(ctx: Ctx):
    sent = ctx.ledger("sent")
    pending = [
        str(path)
        for path in ctx.glob(ctx.settings["pattern"])
        if ctx.unchanged_for(path, ctx.settings["quiet"]) and ctx.file_key(path) not in sent
    ]
    return bool(pending), pending


def on_true(ctx: Ctx) -> None:
    # No expire: the tarballs stay in ~/incoming, so a forgotten entry would be sent again.
    sent = ctx.ledger("sent")
    for path in ctx.payload:
        # BatchMode makes scp fail instead of prompting for a password or a host key.
        ctx.run(["scp", "-q", "-o", "BatchMode=yes", path, ctx.settings["dest"]])
        sent.add(ctx.file_key(path))  # only reached when scp succeeded
        ctx.log.info("sent %s", path, extra={"file": path})
