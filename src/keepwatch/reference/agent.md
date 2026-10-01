# Writing a keepwatch watch (start here)

keepwatch runs *watches*. A watch is a directory holding a `config.toml` and some code. On every poll keepwatch runs the watch's **check**, which answers TRUE or FALSE, and then runs the **actions** that the state rules call for. You write the check and the actions; keepwatch does the scheduling, state keeping, timeouts, retries, failure limits and logging.

## The contract

1. **Location.** A watch is a subdirectory of a watches directory: `$XDG_CONFIG_HOME/keepwatch/watches/<name>/` by default (`~/.config/keepwatch/watches/<name>/`), or any directory listed in `watch_dirs` of the global config. The directory name is the watch name: letters, digits, `_`, `.` and `-`, starting with a letter or digit. Directories starting with `.` or `_` are ignored.
2. **config.toml** is required (it may be empty). Unknown keys are errors. Put the watch's own parameters in `[settings]`; Python reads them as `ctx.settings`, commands as `KEEPWATCH_SETTING_<KEY>`. See `keepwatch docs config`.
3. **Hooks.** `check` is required; `on_rise`, `on_fall`, `on_true` and `on_false` are optional. Each hook is either a top-level `def <hook>(ctx):` in `watch.py` (see `keepwatch docs python`) or a command in `[hooks]` of config.toml (see `keepwatch docs executables`), never both. Hooks written any other way (imported, assigned, `async def`, nested) never run; `keepwatch validate` reports them.
4. **The check MUST only look, never change anything.** It returns `True` or `False`, `(answer, payload)` to hand JSON data to the actions, or `None` for "cannot tell right now" (or raises `keepwatch.Unknown("reason")`). Any other return value or exception is an error.
5. **Actions** succeed by returning (exit status 0) and fail by raising (nonzero exit).
6. **When actions run** (full rules: `keepwatch docs states`):
   - `on_true` runs on every poll while the condition is TRUE; `on_false` on every poll while it is FALSE.
   - `on_rise` runs once when the condition becomes TRUE, `on_fall` once when it becomes FALSE. A failed `on_rise`/`on_fall` is retried on later polls until it succeeds or the condition flips back.
   - Every watch starts FALSE (`initial_condition = false`), so a condition that is already TRUE when keepwatch starts fires `on_rise`. Write checks so that TRUE is the thing to act on.
   - Actions can run more than once. Make them safe to repeat, and record finished work in a ledger (`ctx.ledger`).
7. **Every hook runs in a fresh process** with stdin `/dev/null`, the watch directory as working directory, and a deadline (`check_timeout`, `action_timeout`, default 60s). Nothing may prompt for input. See `keepwatch docs environment`.
8. **State** belongs in `ctx.data_dir` and ledgers (persistent) or `ctx.run_dir` (until keepwatch exits), never in the watch directory. See `keepwatch docs storage`.

## Development loop

```sh
keepwatch new <name>                                  # python template; --template shell or expect
keepwatch validate <name>                             # every config and code problem, with line numbers
keepwatch poll <name> --fake true,true,false --dry-run   # the state logic only: which actions would run
keepwatch poll <name> --dry-run                       # the real check; actions are reported, not run
keepwatch poll <name>                                 # one real poll, everything printed as it happens
keepwatch logs --poll <id> -v                         # everything a poll did, with captured output
```

`validate`, `poll`, `status` and `logs` accept `--json`. Exit status is 0 for success, 1 for problems found or a failed poll, 2 for wrong usage.

## Done checklist

- [ ] `keepwatch validate <name>` exits 0.
- [ ] `keepwatch poll <name> --dry-run` shows the check answering correctly for the current state of the world.
- [ ] `keepwatch poll <name> --fake true,true,false --dry-run` plans exactly the actions you intend.
- [ ] A real `keepwatch poll <name>` succeeds, and running it again does not repeat work already done.
- [ ] Anything that can hang (network, ssh, scp) is non-interactive (`ssh -o BatchMode=yes`) and fits within the timeouts.
- [ ] `description` in config.toml says in one line what the watch does.

## Common mistakes

- Returning a list or a string from `check`. Return `bool(items), items`.
- Changing things in `check` (sending, deleting, adding to a ledger). Checks run for real during `--dry-run`, and ledgers are read-only in `check`.
- Doing per-item work in `on_rise`. It runs once per FALSE→TRUE change, so items that arrive while the condition stays TRUE are missed. Use `on_true` plus a ledger.
- Running commands that prompt: ssh passwords, unknown host keys, `sudo`. stdin is `/dev/null`, so they fail or hang until the timeout.
- Assuming your login shell's environment. The service runs under systemd with the PATH captured by `keepwatch install` and without your shell's ssh-agent. See `keepwatch docs environment`.
- Setting a ledger `expire` for things that stay visible. When an entry expires, the thing it stood for is processed again.
- Renaming a watch directory with `mv`. Its state stays behind under the old name, and the watch starts with empty ledgers. Use `keepwatch rename`.
- Storing data in the watch directory. Use `ctx.data_dir`.

## Reference topics

Run `keepwatch docs` for the list of topics, or `keepwatch docs --all` for everything at once.
