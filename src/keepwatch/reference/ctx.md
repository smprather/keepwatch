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
| `ctx.settings` | mapping | The watch's `[settings]` table. |
| `ctx.watch_dir` | `Path` | The watch directory, which is also the working directory. |
| `ctx.log` | `logging.Logger` | Logs into keepwatch's log; see `keepwatch docs logging`. |

## Exceptions

`keepwatch.Unknown(reason)` (raise in `check` for "cannot tell right now"), `keepwatch.CommandFailed` (raised by `ctx.run`), `keepwatch.LedgerReadOnly` (a ledger change inside `check`) and `keepwatch.LedgerCorrupt` (an unreadable ledger file) can be imported from `keepwatch`.
