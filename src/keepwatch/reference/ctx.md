# The Ctx object

Every Python hook receives one argument, `ctx`, a `keepwatch.Ctx`. The package ships type hints (`py.typed`), so editors can check watch.py against it.

## Attributes

| Attribute | Type | Meaning |
|---|---|---|
| `ctx.watch` | `str` | The watch name. |
| `ctx.hook` | `str` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `ctx.poll_id` | `str` | This poll's ID; it is on every log record of the poll. |
| `ctx.condition` | `bool` | The condition as this hook sees it: before the answer in `check`, after it in actions. |
| `ctx.payload` | JSON value or `None` | What `check` returned with its answer. Actions only. |
| `ctx.events` | `list[dict]` | Observer events delivered with this poll, oldest first; empty without observers. See `keepwatch docs observers`. |
| `ctx.settings` | mapping | The watch's `[settings]` table. |
| `ctx.watch_dir` | `Path` | The watch directory, which is also the working directory. |
| `ctx.log` | `logging.Logger` | Logs into keepwatch's log; see `keepwatch docs logging`. |

## Checkpoints

`ctx.checkpoint(name)` is a persistent JSON **document** for this watch (under its data directory), unlike
`ctx.ledger(name)`, which is a set of timestamped keys. A check may **read** it — `checkpoint.value` is the
document, or `None` before anything has been saved — and only an **action** may change it:
`checkpoint.save(document)` replaces the whole document atomically, `checkpoint.clear()` deletes it. Inside
`check`, both raise `keepwatch.CheckpointReadOnly`.

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

That is also why a check may not write one: `keepwatch poll --dry-run` runs checks for real, so a check that
saved would leave a record of work that never happened. Saving rewrites the whole document — for per-key churn
(and for anything a check must not hold) a ledger is the right tool. The `mirror` recipe is built on this
pattern.

## Exceptions

`keepwatch.Unknown(reason)` (raise in `check` for "cannot tell right now"), `keepwatch.CommandFailed` (raised by `ctx.run`), `keepwatch.LedgerReadOnly` (a ledger change inside `check`), `keepwatch.LedgerCorrupt` (an unreadable ledger file), `keepwatch.CheckpointReadOnly` (a checkpoint change inside `check`) and `keepwatch.CheckpointCorrupt` (an unreadable checkpoint file) can be imported from `keepwatch`.
