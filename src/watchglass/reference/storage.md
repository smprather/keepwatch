# Directories and storage

## Layout

| Path | Contents |
| --- | --- |
| `$XDG_CONFIG_HOME/watchglass/config.toml` | The global config (optional). |
| `$XDG_CONFIG_HOME/watchglass/watches/<name>/` | A watch: `config.toml` plus code. |
| `$XDG_STATE_HOME/watchglass/logs/watchglass.jsonl` | The log, rotated to `watchglass.jsonl.1`, `.2`, … |
| `$XDG_STATE_HOME/watchglass/status.json` | Written by the service every master tick, with a `format` number (see `watchglass docs tui`); each watch's entry says whether it is running a hook right now (`in_flight`) and what that hook is transferring (`transfer`, with the partial file's size). |
| `$XDG_STATE_HOME/watchglass/watches/<name>/offline.json` | Present while the watch is offline. |
| `$XDG_STATE_HOME/watchglass/watches/<name>/data/` | The watch's persistent storage (`ctx.data_dir`). |
| `$XDG_STATE_HOME/watchglass/watches/<name>/data/ledgers/<ledger>.json` | Ledgers. |
| `$XDG_RUNTIME_DIR/watchglass/<pid>/<name>/` | Run-only scratch space for one watchglass process (`ctx.run_dir`). |
| `$XDG_RUNTIME_DIR/watchglass/locks/<name>.lock` | Held while the watch is being polled. |

On Windows the defaults are `%APPDATA%\watchglass` for configuration and watches, `%LOCALAPPDATA%\watchglass` for state and logs, and `%LOCALAPPDATA%\watchglass\run` for run-only data. Setting the XDG variables overrides them on Windows too.

`XDG_CONFIG_HOME` defaults to `~/.config` and `XDG_STATE_HOME` to `~/.local/state`. `XDG_RUNTIME_DIR` is normally `/run/user/<uid>`: private, in memory, and deleted at logout. Without it watchglass uses `$TMPDIR/watchglass-<uid>` (or `/tmp/watchglass-<uid>`), created with mode 0700, and refuses to use it if anyone else could read it.

## Upgrading from keepwatch

The first `watchglass` command moves `$XDG_CONFIG_HOME/keepwatch` and `$XDG_STATE_HOME/keepwatch` (or the
Windows defaults, `%APPDATA%\keepwatch` and `%LOCALAPPDATA%\keepwatch`) to the watchglass paths once, when the
new directories do not exist. Stop the old service first: the migration refuses while the old service lock is
held. If both the old and new directories exist, both are left alone for you to reconcile. Run-only data is not
moved (old runtime directories are ignored and swept normally), and the environment variables are
`WATCHGLASS_*` now.

## Persistent and run-only data

- `ctx.data_dir` (`WATCHGLASS_DATA_DIR`) survives restarts and reboots. The watch owns its contents and must keep them tidy.
- `ctx.run_dir` (`WATCHGLASS_RUN_DIR`) lasts as long as the watchglass process: the service, or one manual `watchglass poll`. Each process has its own; it is deleted when the process exits, and leftovers of crashed processes are removed at the next start.
- Never store data in the watch directory: it holds code and config, and may be a git repository.

## Ledgers

`ctx.ledger(name)` opens a persistent set of string keys, each stamped with the time it was added. Use it to remember finished work ("this file was already sent").

```python
sent = ctx.ledger("sent")                  # read-only inside check
if ctx.file_key(path) not in sent:
    ...
sent.add(ctx.file_key(path))               # saved at once, atomically
```

- Every `add` and `discard` rewrites the file atomically, so a crash in the middle of a loop keeps everything recorded so far.
- `ctx.file_key(path)` is `"<absolute path>|<size>|<mtime_ns>"`: a new file that reuses an old name gets a new key, so it is processed.
- `ctx.ledger(name, expire="90d")` forgets entries older than that. **Only expire keys for things that go away.** If the thing a key stands for can still be seen after its entry expires, the watch processes it again.
- A ledger file that cannot be read raises `watchglass.LedgerCorrupt`, whose message names the file. Fix it, or delete it to start with an empty ledger.
- The format is JSON: `{"version": 1, "entries": {"<key>": <unix time added>}}`.

## Renaming and deleting watches

State is kept under the watch's name. Rename a watch with `watchglass rename OLD NEW`, which moves the watch directory and its state together; renaming the directory with `mv` leaves the state behind, and the watch starts with empty ledgers (a watch that sends files would send them all again). Deleting a watch directory leaves its state in place; `watchglass status` lists such orphaned state directories, and you may delete them by hand.
