# Command hooks (shell, Expect, any language)

Any hook can be a command instead of a Python function. Define it in `[hooks]` of config.toml:

```toml
[hooks]
check   = "test -e /run/vpn.up"            # a string runs through /bin/sh -c
on_true = ["expect", "./send.exp"]          # a list runs directly (argv)
on_fall = ["./notify.sh", "VPN went down"]
```

A hook defined both in `[hooks]` and in watch.py is an error.

## Which program runs

- **A string** runs through the platform shell: `/bin/sh -c` on Linux and macOS, **Windows PowerShell** (`powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command`) on Windows. The watch key `shell` (or `[defaults]` in the global config) replaces it, e.g. `shell = ["pwsh", "-NoProfile", "-Command"]` or `shell = ["cmd.exe", "/d", "/c"]`. `exit N` sets the exit code in both default shells.
- **A list** runs directly. A program named with a path separator (`./notify.sh`, `.\notify.ps1`) is relative to the watch directory; a bare name (`expect`) is looked up on PATH. On Linux and macOS the file must be executable.
- **On Windows**, list hooks are started by file type: `.ps1` with Windows PowerShell (`-File`), `.py` with keepwatch's own Python, `.cmd`/`.bat` with `cmd.exe /d /c`; `.exe` files and other programs run directly.

`keepwatch validate` checks list-form programs; strings are only checked when they run. A program named with a `/` (`./notify.sh`) is relative to the watch directory and must be executable; a bare name (`expect`) is looked up on PATH. `keepwatch validate` checks list-form programs; strings are only checked when they run.

## What the exit code means

For the **check**, `[check_exit_codes]` maps exit codes to outcomes:

```toml
[check_exit_codes]
true = [0]        # default
false = [1]       # default
unknown = []      # default: no code means "unknown"
```

Any exit code that is not listed is an `error`; a check killed by a signal is an `error`; one that runs past `check_timeout` is a `timeout`. No code maps to `unknown` by default because many programs use low codes for real errors (curl exits 3 for a malformed URL). List the codes that mean "cannot tell right now", for example curl's `unknown = [6, 7, 28]` (cannot resolve host, cannot connect, timed out). A code may appear in only one list.

An **action** succeeds with exit status 0 and fails otherwise.

## What the command receives

- Working directory: the watch directory. Standard input: none (`/dev/null`, or `NUL` on Windows).
- Environment: the service's environment, then the global `[environment]`, then the watch's `[environment]`, then these variables. Inherited `KEEPWATCH_*` variables are removed first (except `KEEPWATCH_CONFIG`).

| Variable | Meaning |
|---|---|
| `KEEPWATCH_WATCH` | The watch name. |
| `KEEPWATCH_HOOK` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `KEEPWATCH_POLL_ID` | This poll's ID; it is on every log record of the poll. |
| `KEEPWATCH_CONDITION` | `true` or `false`: the condition before the answer in the check, after it in actions. |
| `KEEPWATCH_WATCH_DIR` | The watch directory. |
| `KEEPWATCH_DATA_DIR` | Persistent storage for this watch (created before the call). |
| `KEEPWATCH_RUN_DIR` | Run-only scratch space (created before the call). |
| `KEEPWATCH_SETTINGS_FILE` | A JSON file holding the whole `[settings]` table. |
| `KEEPWATCH_SETTING_<KEY>` | Each top-level scalar setting; the key is upper-cased and anything other than letters and digits becomes `_` (`dest-host` → `KEEPWATCH_SETTING_DEST_HOST`). Booleans are `true`/`false`. |
| `KEEPWATCH_EVENTS_FILE` | A JSON file holding this poll's observer events, oldest first (`[]` when there are none). See `keepwatch docs observers`. |
| `KEEPWATCH_PAYLOAD_OUT` | Check only: write JSON here to hand a payload to the actions. |
| `KEEPWATCH_PAYLOAD_FILE` | Actions only, and only when there is a payload: the JSON file holding it. |

Python hooks get the same variables, so programs they start can use them too.

Command hooks transfer files with `keepwatch kit` (`copy`, `pull`, `push`, `tcp-open`): the same scp transfers Python hooks get as `ctx.transfer`, with the same checks and logging. See `keepwatch docs transfers`.

## Passing a payload from a command check

```sh
#!/bin/sh
files=$(ls ~/incoming/*.tar.gz 2>/dev/null) || exit 1   # nothing there: FALSE
printf '%s\n' "$files" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split()))' > "$KEEPWATCH_PAYLOAD_OUT"
exit 0
```

An empty or missing payload file means no payload. A file that is not valid JSON, or is larger than 1 MiB, makes the outcome `error`. The payload and settings files are deleted after the call.

## Output

stdout and stderr are captured into the hook's log record (the first and last 32 KiB of each with the default `capture_bytes`) and decoded as UTF-8; anything that is not valid UTF-8 is replaced, not fatal. String hooks in the default Windows PowerShell are switched to UTF-8 output automatically. A `.ps1` script should start with `try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }` (the templates and examples do), otherwise non-ASCII output arrives garbled. A command that leaves a background process holding its stdout open is not waited for: once the command exits, keepwatch waits two seconds and then stops its process tree.

## Expect

Expect spawns programs on their own terminal, so it can drive interactive logins even though the hook's stdin is `/dev/null`. Always set `timeout` and handle it (`timeout { exit 1 }`), so a missing prompt fails the action instead of waiting for `action_timeout`. Read settings as `$env(KEEPWATCH_SETTING_HOST)`.
