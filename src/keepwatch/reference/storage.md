# Directories and storage

## Layout

| Path | Contents |
|---|---|
| `$XDG_CONFIG_HOME/keepwatch/config.toml` | The global config (optional). |
| `$XDG_CONFIG_HOME/keepwatch/watches/<name>/` | A watch: `config.toml` plus code. |
| `$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl` | The log, rotated to `keepwatch.jsonl.1`, `.2`, … |
| `$XDG_STATE_HOME/keepwatch/status.json` | Written by the service every master tick. |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/offline.json` | Present while the watch is offline. |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/data/` | The watch's persistent storage (`ctx.data_dir`). |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/data/ledgers/<ledger>.json` | Ledgers. |
| `$XDG_RUNTIME_DIR/keepwatch/<pid>/<name>/` | Run-only scratch space for one keepwatch process (`ctx.run_dir`). |
| `$XDG_RUNTIME_DIR/keepwatch/locks/<name>.lock` | Held while the watch is being polled. |

On Windows the defaults are `%APPDATA%\keepwatch` for configuration and watches, `%LOCALAPPDATA%\keepwatch` for state and logs, and `%LOCALAPPDATA%\keepwatch\run` for run-only data. Setting the XDG variables overrides them on Windows too.

`XDG_CONFIG_HOME` defaults to `~/.config` and `XDG_STATE_HOME` to `~/.local/state`. `XDG_RUNTIME_DIR` is normally `/run/user/<uid>`: private, in memory, and deleted at logout. Without it keepwatch uses `$TMPDIR/keepwatch-<uid>` (or `/tmp/keepwatch-<uid>`), created with mode 0700, and refuses to use it if anyone else could read it.

## Persistent and run-only data

- `ctx.data_dir` (`KEEPWATCH_DATA_DIR`) survives restarts and reboots. The watch owns its contents and must keep them tidy.
- `ctx.run_dir` (`KEEPWATCH_RUN_DIR`) lasts as long as the keepwatch process: the service, or one manual `keepwatch poll`. Each process has its own; it is deleted when the process exits, and leftovers of crashed processes are removed at the next start.
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
- A ledger file that cannot be read raises `keepwatch.LedgerCorrupt`, whose message names the file. Fix it, or delete it to start with an empty ledger.
- The format is JSON: `{"version": 1, "entries": {"<key>": <unix time added>}}`.

## Renaming and deleting watches

State is kept under the watch's name. Rename a watch with `keepwatch rename OLD NEW`, which moves the watch directory and its state together; renaming the directory with `mv` leaves the state behind, and the watch starts with empty ledgers (a watch that sends files would send them all again). Deleting a watch directory leaves its state in place; `keepwatch status` lists such orphaned state directories, and you may delete them by hand.
