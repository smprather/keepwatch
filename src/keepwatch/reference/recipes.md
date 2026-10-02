# Recipes: built-in watches

Some watches are the same everywhere. A **recipe** is one keepwatch provides: set `recipe = "<name>"` in config.toml and configure it in `[settings]`. A recipe watch has no watch.py, no `[hooks]` and no `[observe.*]` tables (the recipe brings its own); everything else (`interval`, timeouts, `max_failures`, `retry_after`, `[environment]`) works as for any watch. `keepwatch validate`, `poll`, `observe`, `status` and `logs` work too; hook records show `recipe pull:check` and so on.

## pull: a remote directory → a local staging folder

```toml
description = "Pull finished exports from linux1"
recipe = "pull"
action_timeout = "2h"      # big files over a slow link

[settings]
remote = "me@linux1"
remote_dir = "/data/out"
local_dir = 'C:/relay/staging'
pattern = "*.tar.gz"
settle = "30s"
```

- A `remote_files` observer (named `remote`) keeps one ssh connection to `remote` and reports each settled file with its sha256 (see `keepwatch docs observers` for what the source host needs: Python 3.6+, key authentication, a known host key).
- **check** answers TRUE with the reported files not yet in the ledger `pulled`.
- **on_true** pulls each one into `local_dir` through `.NAME.part`, verifies size and sha256, renames it, and adds it to `pulled`. A failure stops the action (the files already done stay recorded) and the poll fails: the watch backs off and retries, and the events stay queued (at-least-once).
- When `local_dir` already holds a different file of the same name (a newer version arrived while the old one still waits to be pushed), `on_conflict` decides: `rename` (default: the new one becomes NAME-1.ext), `overwrite`, or `skip-identical` (fail). An identical file (same sha256) is never copied again.
- With `delete_remote = true`, each verified file is also queued in the ledger `to_delete` (with the sha256 it was verified with) and deleted from the source host by keepwatch's remote helper, in one ssh call per poll. The helper deletes only a regular file directly in `remote_dir` that still has the same size and modification time **and the same content** (it re-hashes it, so even a rewrite with the same size and modification time is kept). Only files pulled while the option is on are deleted (they carry the verified sha256 the check needs); files pulled before stay on the source host. A rewrite that keeps both size and modification time is invisible to the watcher (as to any watcher that goes by modification times). A delete problem never fails the poll; a failed delete is retried at the first poll after `delete_retry`, which without new files comes with the watch's `interval`. Needs `checksum = true`. A file replaced in the instant between the helper's check and its delete cannot be told apart; producers should write under a temporary name and rename, as most tools do.
- The same ledger is the observer's `skip_ledger`: after a reconnect, files already pulled are neither hashed nor reported again. Without `delete_remote`, files stay on the source host and a pulled file is pulled again only if its size or modification time changes.

## push: a local folder → a remote directory, when it is reachable

```toml
description = "Push staged files to linux2 when it is connected"
recipe = "push"
action_timeout = "2h"

[settings]
local_dir = 'C:/relay/staging'
dest = "me@linux2:incoming/"
password_env = "RELAY_PASSWORD"
pattern = "*.tar.gz"
```

- A `files` observer (named `local`) on `local_dir` wakes the watch when a file settles.
- **check** first probes the destination (`reachable_host`, else the host in `dest`; `reachable_port`, else `port`, else 22). **No answer means unknown**: no failure, no backoff, no going offline; the files simply wait in `local_dir`, which is the queue. Set `reachable_host` when `dest` uses a Host alias from `~/.ssh/config` (an alias is not a DNS name). Otherwise check answers TRUE with the settled files in `local_dir`.
- **on_true** pushes each file, then its `NAME.sha256` marker (`marker = "none"` to skip), then deletes it (`after = "delete"`, the default), moves it to `archive_dir` (`"archive"`, pruning files older than `keep_for`) or keeps it (`"keep"`; the ledger `pushed` remembers it).

The two chain naturally through a staging folder: pull's `local_dir` is push's `local_dir`. See `keepwatch docs relay` for the whole setup.
