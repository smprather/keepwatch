# Python hooks (watch.py)

A watch's `watch.py` may define these module-level functions. Each one receives a `Ctx` (see `keepwatch docs ctx`).

```python
from keepwatch import Ctx

def check(ctx: Ctx): ...            # required (unless [hooks] has check)
def on_rise(ctx: Ctx) -> None: ...  # the condition became TRUE
def on_fall(ctx: Ctx) -> None: ...  # the condition became FALSE
def on_true(ctx: Ctx) -> None: ...  # every poll while TRUE
def on_false(ctx: Ctx) -> None: ... # every poll while FALSE
```

Only **top-level `def` statements** count as hooks. A hook that is imported (`from helpers import on_rise`), assigned (`on_rise = make()`), written as `async def` or defined inside an `if` never runs. `keepwatch validate` reports such hooks.

## What check returns

| Return value | Outcome |
|---|---|
| `True` / `False` | `true` / `false` |
| `(True, payload)` / `(False, payload)` | the answer, plus a payload for the actions |
| `None`, or `(None, payload)` | `unknown`: the check ran fine but cannot tell right now |
| `raise keepwatch.Unknown("reason")` | `unknown`, with the reason in the log |
| anything else (a list, a string, a number) | `error` |
| any other exception | `error`, with the traceback in the log |

The payload must be JSON-serializable and at most 1 MiB once encoded. `pathlib.Path` values become strings and tuples become lists; anything else that JSON cannot represent (sets, objects) makes the outcome `error`. The actions receive the payload as `ctx.payload`, and it is logged in full.

## Actions

An action succeeds when it returns and fails when it raises. Its return value is ignored. `ctx.run` raises `keepwatch.CommandFailed` for a nonzero exit, so a failed command fails the action unless you catch it.

## Rules

- **check MUST only observe.** It must not change anything outside `ctx.run_dir`: `keepwatch poll --dry-run` runs real checks, and ledgers are read-only inside `check` (writing raises `keepwatch.LedgerReadOnly`).
- `keepwatch.Unknown` only has a meaning in `check`; raising it in an action is an error.
- Each hook call is a **fresh process**: module-level variables do not survive between calls or from check to actions. Pass data from check to actions through the payload; keep anything longer-lived in `ctx.data_dir` or a ledger.
- watch.py is imported as the module `keepwatch_watch`. The watch directory is first on `sys.path`, so `watch.py` can import sibling modules (`helpers.py`) and packages in the watch directory. The working directory is the watch directory.
- `print()` and anything written to stdout or stderr (including by programs you start without `ctx.run`) is captured into the hook's log record (the first and last 32 KiB with the default `capture_bytes`).
- Logging: use `ctx.log` (a standard `logging.Logger`); see `keepwatch docs logging`.
- Third-party packages: list them in `python_dependencies`; see `keepwatch docs dependencies`. Without it, watch.py can import the standard library and `keepwatch`.
- `sys.exit()` in a hook is treated like any other exception: the hook fails.
