# Outcomes and the state rules

## Outcomes

| Outcome | Meaning |
|---|---|
| `true` / `false` | An answer. |
| `unknown` | The check ran correctly but cannot tell right now (Python `None` or `keepwatch.Unknown`; a command's `unknown` exit codes). |
| `timeout` | keepwatch killed the check at `check_timeout`. |
| `error` | The check broke: an exception, an unlisted exit code, a signal, a bad return value or payload, or it could not start. |

## What one poll does

| Check says | Condition was FALSE | Condition was TRUE |
|---|---|---|
| `true` | becomes TRUE; run `on_rise`, then `on_true` | run `on_true` |
| `false` | run `on_false` | becomes FALSE; run `on_fall`, then `on_false` |
| `unknown` | nothing runs; condition unchanged | nothing runs; condition unchanged |
| `timeout` / `error` | nothing runs; condition unchanged; the poll fails | same |

## Rules

1. **Start.** When the service starts, and when a watch appears while it runs, the condition is `initial_condition` (default `false`). A condition that is already TRUE then fires `on_rise`; one that is FALSE fires nothing. Write the check so that TRUE is the thing to act on.
2. **Edges are retried until they succeed.** A FALSE→TRUE change leaves a pending `on_rise` that is cleared only when `on_rise` succeeds; until then it runs again on every poll that answers `true`. If the condition flips back first, the pending edge is replaced by the opposite one. So actions run *at least once*: make them safe to repeat.
3. **Actions in one poll run in order and stop at the first failure.** If `on_rise` fails, `on_true` does not run in that poll.
4. **Polls of one watch never overlap.** `interval` is measured from the end of a poll to the start of the next (longer after failures; see `keepwatch docs failures`). Different watches run independently and in parallel.
5. **Config changes keep the state.** A changed config applies from the next poll; the condition, pending edge and failure count are kept.
6. **Missing hooks are skipped.** A pending edge whose hook does not exist is dropped at once.
7. **The condition is not saved across restarts.** Restarting the service starts every watch from `initial_condition` again; durable facts belong in ledgers.

## Testing the rules

`keepwatch poll NAME --fake true,true,false,timeout --dry-run` feeds made-up outcomes through these rules without running the check, and shows which actions each poll would run. Dry runs assume every action succeeds. Without `--dry-run`, the actions run for real with the payload from `--payload`. `--initial-condition true` starts from TRUE. A manual poll starts from `initial_condition` and does not share the running service's condition or failure count.
