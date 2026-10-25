# Writing a watchglass watch (start here)

watchglass runs *watches*. A watch is a directory holding a `config.toml` and some code. On every poll watchglass runs the watch's **check**, which answers TRUE or FALSE, and then runs the **actions** that the state rules call for. You write the check and the actions; watchglass does the scheduling, state keeping, timeouts, retries, failure limits and logging.

## The contract

1. **Location.** A watch is a subdirectory of a watches directory: `$XDG_CONFIG_HOME/watchglass/watches/<name>/` by default (`~/.config/watchglass/watches/<name>/`), or any directory listed in `watch_dirs` of the global config. The directory name is the watch name: letters, digits, `_`, `.` and `-`, starting with a letter or digit. Directories starting with `.` or `_` are ignored.
2. **config.toml** is required (it may be empty). Unknown keys are errors. Put the watch's own parameters in `[settings]`; Python reads them as `ctx.settings`, commands as `WATCHGLASS_SETTING_<KEY>`. See `watchglass docs config`.
3. **Hooks.** `check` is required; `on_rise`, `on_fall`, `on_true` and `on_false` are optional. Each hook is either a top-level `def <hook>(ctx):` in `watch.py` (see `watchglass docs python`) or a command in `[hooks]` of config.toml (see `watchglass docs executables`), never both. Hooks written any other way (imported, assigned, `async def`, nested) never run; `watchglass validate` reports them.
4. **The check MUST only look, never change anything.** It returns `True` or `False`, `(answer, payload)` to hand JSON data to the actions, or `None` for "cannot tell right now" (or raises `watchglass.Unknown("reason")`). Any other return value or exception is an error.
5. **Actions** succeed by returning (exit status 0) and fail by raising (nonzero exit).
6. **When actions run** (full rules: `watchglass docs states`):
   - `on_true` runs on every poll while the condition is TRUE; `on_false` on every poll while it is FALSE.
   - `on_rise` runs once when the condition becomes TRUE, `on_fall` once when it becomes FALSE. A failed `on_rise`/`on_fall` is retried on later polls until it succeeds or the condition flips back.
   - Every watch starts FALSE (`initial_condition = false`), so a condition that is already TRUE when watchglass starts fires `on_rise`. Write checks so that TRUE is the thing to act on.
   - Actions can run more than once. Make them safe to repeat, and record finished work in a ledger (`ctx.ledger`).
7. **Every hook runs in a fresh process** with no standard input, the watch directory as working directory, and a deadline (`check_timeout`, `action_timeout`, default 60s). Nothing may prompt for input. See `watchglass docs environment`.
8. **State** belongs in `ctx.data_dir` and ledgers (persistent) or `ctx.run_dir` (until watchglass exits), never in the watch directory. See `watchglass docs storage`.
9. **On Windows,** string hooks run in Windows PowerShell; list hooks ending in `.ps1`, `.py`, `.cmd` or `.bat` run with their interpreter; `sh` scripts and Expect need extra tools. Start from `watchglass new <name> --template powershell` or the default Python template.

## Development loop

```sh
watchglass new <name>                                  # python template; --template shell or expect
watchglass validate <name>                             # every config and code problem, with line numbers
watchglass poll <name> --fake true,true,false --dry-run   # the state logic only: which actions would run
watchglass poll <name> --dry-run                       # the real check; actions are reported, not run
watchglass poll <name>                                 # one real poll, everything printed as it happens
watchglass logs --poll <id> -v                         # everything a poll did, with captured output
```

`validate`, `poll`, `status` and `logs` accept `--json`. Exit status is 0 for success, 1 for problems found or a failed poll, 2 for wrong usage.

## Done checklist

- [ ] `watchglass validate <name>` exits 0.
- [ ] `watchglass poll <name> --dry-run` shows the check answering correctly for the current state of the world.
- [ ] `watchglass poll <name> --fake true,true,false --dry-run` plans exactly the actions you intend.
- [ ] A real `watchglass poll <name>` succeeds, and running it again does not repeat work already done.
- [ ] Anything that can hang (network, ssh, scp) is non-interactive (`ssh -o BatchMode=yes`) and fits within the timeouts.
- [ ] `description` in config.toml says in one line what the watch does.

## Common mistakes

- Returning a list or a string from `check`. Return `bool(items), items`.
- Changing things in `check` (sending, deleting, adding to a ledger). Checks run for real during `--dry-run`, and ledgers are read-only in `check`.
- Doing per-item work in `on_rise`. It runs once per FALSE→TRUE change, so items that arrive while the condition stays TRUE are missed. Use `on_true` plus a ledger.
- Running commands that prompt: ssh passwords, unknown host keys, `sudo`. stdin is `/dev/null`, so they fail or hang until the timeout.
- Assuming your login shell's environment. The service runs under systemd with the PATH captured by `watchglass install` and without your shell's ssh-agent. See `watchglass docs environment`.
- Setting a ledger `expire` for things that stay visible. When an entry expires, the thing it stood for is processed again.
- Renaming a watch directory with `mv`. Its state stays behind under the old name, and the watch starts with empty ledgers. Use `watchglass rename`.
- Storing data in the watch directory. Use `ctx.data_dir`.
- Writing Windows paths in double-quoted TOML strings: `\` starts an escape there. Use single-quoted strings (`'C:\data\in'`) or forward slashes (`"C:/data/in"`).
- Assuming each observer event arrives exactly once. Delivery is at-least-once (events come again after a failed or unknown poll); record handled items in a ledger. See `watchglass docs observers`.
- Calling `scp` or `sftp` yourself, or putting a password in a hook. Use `ctx.transfer` / `watchglass kit` with `password_env`: host keys are checked, nothing can prompt, partial pulls never appear under the real name, and the password stays in the environment. See `watchglass docs transfers`.
- Writing a watch.py to move files between machines. Try a recipe first (`recipe = "pull"` or `"push"`, `watchglass docs recipes`); it already handles settling, verification, markers, reachability and retries.

## Reference topics

Run `watchglass docs` for the list of topics, or `watchglass docs --all` for everything at once.
