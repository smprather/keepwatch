# The Ctx object

Every Python hook receives one argument, `ctx`, a `watchglass.Ctx`. The package ships type hints (`py.typed`), so editors can check watch.py against it.

## Attributes

| Attribute | Type | Meaning |
|---|---|---|
| `ctx.watch` | `str` | The watch name. |
| `ctx.hook` | `str` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `ctx.poll_id` | `str` | This poll's ID; it is on every log record of the poll. |
| `ctx.condition` | `bool` | The condition as this hook sees it: before the answer in `check`, after it in actions. |
| `ctx.payload` | JSON value or `None` | What `check` returned with its answer. Actions only. |
| `ctx.events` | `list[dict]` | Observer events delivered with this poll, oldest first; empty without observers. See `watchglass docs observers`. |
| `ctx.settings` | mapping | The watch's `[settings]` table. |
| `ctx.watch_dir` | `Path` | The watch directory, which is also the working directory. |
| `ctx.log` | `logging.Logger` | Logs into watchglass's log; see `watchglass docs logging`. |

## Headlines

`ctx.event(message, **fields)` writes a plain-language headline (`plugin.event`) about what happened, as opposed
to the detail `ctx.log` writes. Use it for the story a person watching the watch should read: a file found, a
transfer started or finished, a sweep done. `level="WARNING"` (or `ERROR`) makes the line visible even at the
TUI's quiet level, so a failure headline must use it. Keys become JSON fields in the log.

```python
def on_true(ctx):
    ctx.event("pulling %s from %s" % (name, host), name=name)
    try:
        pull(name)
    except Exception as exc:
        ctx.event("pulling %s failed: %s" % (name, exc), level="WARNING", name=name)
        raise
    ctx.event("pulled %s" % name, name=name)
```

## Ledgers

`ctx.ledger(name)` is a persistent **set of keys**, each stamped with when it was added: the right place for
"this one is done" when the set may be large or is checked key by key (`key in ledger`). `add(key)` and
`discard(key)` write the file atomically after every change, so a crash mid-loop keeps what the loop recorded.
A long queue is recorded in one write instead:

```python
def on_true(ctx):
    sent = ctx.ledger("sent")
    with sent.batch():            # one atomic write for the whole loop; still saved if it raises
        for name in ctx.payload:
            deliver(name)
            sent.add(name)
```

`ctx.ledger(name, expire="90d")` forgets entries older than that (they are pruned on the next write and ignored
before that). Only expire keys for things that go away: a key whose subject is still visible after it expires
makes the watch do the work again. A ledger with no `expire` keeps every key, which is what the `pull` recipe
needs of `pulled` — that list is also what the observer skips on reconnect — and it grows for as long as the
watch runs.

## Checkpoints

`ctx.checkpoint(name)` is a persistent JSON **document** for this watch (under its data directory), unlike
`ctx.ledger(name)`, which is a set of timestamped keys. A check may **read** it — `checkpoint.value` is the
document, or `None` before anything has been saved — and only an **action** may change it:
`checkpoint.save(document)` replaces the whole document atomically, `checkpoint.clear()` deletes it. Inside
`check`, both raise `watchglass.CheckpointReadOnly`.

Use it for "what has already been done", and save it as each unit of work succeeds:

```python
def check(ctx):
    done = ctx.checkpoint("uploads").value or {}
    todo = [name for name in names() if name not in done]
    return bool(todo), todo


def on_true(ctx):
    state = ctx.checkpoint("uploads")
    done = dict(state.value or {})
    for name in ctx.payload:
        upload(name)
        done[name] = name
        state.save(done)          # an interrupted action resumes from here
```

That is also why a check may not write one: `watchglass poll --dry-run` runs checks for real, so a check that
saved would leave a record of work that never happened. Saving rewrites the whole document — for per-key churn
(and for anything a check must not hold) a ledger is the right tool. The `mirror` recipe is built on this
pattern.

## Exceptions

`watchglass.Unknown(reason)` (raise in `check` for "cannot tell right now"), `watchglass.CommandFailed` (raised by `ctx.run`), `watchglass.TransferFailed` (what `ctx.transfer` refuses or fails on; `watchglass.transfer.TransferMismatch` and `watchglass.transfer.TransferMissing` are its subclasses — see `watchglass docs transfers`), `watchglass.LedgerReadOnly` (a ledger change inside `check`), `watchglass.LedgerCorrupt` (an unreadable ledger file), `watchglass.CheckpointReadOnly` (a checkpoint change inside `check`) and `watchglass.CheckpointCorrupt` (an unreadable checkpoint file) can be imported from `watchglass`.
