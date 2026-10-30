# Recipes: built-in watches

Some watches are the same everywhere. A **recipe** is one watchglass provides: set `recipe = "<name>"` in config.toml and configure it in `[settings]`. A recipe watch has no watch.py, no `[hooks]` and no `[observe.*]` tables (the recipe brings its own); everything else (`interval`, timeouts, `max_failures`, `retry_after`, `[environment]`) works as for any watch. `watchglass validate`, `poll`, `observe`, `status` and `logs` work too; hook records show `recipe pull:check` and so on.

## Time budgets

`action_timeout` is the deadline for one whole action, and a recipe action handles every file of one poll. So
that one slow file cannot starve the queue, each file gets an **equal share of what is left** of that budget
(the last file gets the remainder), and `file_timeout` in `[settings]` caps every file harder. A file that fails
or runs out of its share is logged, the rest of the queue is still attempted, and the action says it failed —
so the poll retries, and the files already completed are recorded as they finish: they are not pulled or pushed
twice. For `pull`, a source file that is simply **gone** is a different matter: it is skipped, not failed (see
the pull section below). `push` and `mirror` have no such case: they report the failure and try again on the next
poll.

The `pull` and `push` recipes also write plain-language **headlines** (`ctx.event`, see `watchglass docs logging`)
for their story points: a file found/staged, a transfer starting (or resuming at an offset), finishing with its
size and duration, a sweep, and failures (which stay visible at the TUI's quiet level).

A pull that is killed (a stop, a service restart, a timeout that kills the process) leaves a hidden
`.NAME.part` in the staging folder. The next attempt **discards it and says how many bytes that cost**, and
`part_max_age` (default 7 days) deletes stale ones after a successful pull. With `resume = true` — which needs
`protocol = "sftp"` and `checksum = true` — that partial is **continued** with the sftp client instead of being
thrown away (scp itself cannot resume), and the result is verified like any other pull: a resumed file that does
not verify is pulled again from the start, once.

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

- A `remote_files` observer (named `remote`) keeps one ssh connection to `remote` and reports each settled file with its sha256 (see `watchglass docs observers` for what the source host needs: Python 3.6+, key authentication, a known host key).
- **check** answers TRUE with the reported files not yet in the ledger `pulled`.
- **on_true** pulls each one into `local_dir` through `.NAME.part`, verifies size and sha256, renames it, and adds it to `pulled`. A failure fails the action as a whole (the files already done stay recorded, and the rest of the queue is still attempted) and the poll fails: the watch backs off and retries, and the events stay queued (at-least-once). The exception is a rejected credential or host key: that is permanent, so the queue stops at the first one (one failed login, not one per file) and the watch is parked at once (see `watchglass docs failures`).
- A file reported by the watcher that is **gone from the source by the time it is pulled** — the producer moved, renamed or deleted it; nothing is installed on the source host, so any writer can — is *withdrawn*, not failed. With `on_missing = "skip"` (the default) it is logged with a WARNING, recorded in the ledger `skipped` (forgotten after 7 days) and the poll **succeeds**: the event is acknowledged and the files behind it keep flowing (an offline watch with `retry_after` comes back online on its next trial poll, which now succeeds). Retrying it instead would fail on every poll for a condition that cannot change, and after `max_failures` the watch would go offline with its observer stopped and the real queue starved. `on_missing = "fail"` keeps that strict behavior. scp's and sftp's diagnostics tell a source that is gone from a failure on the local staging side (which names the part file), so no extra ssh call is needed. Those diagnostics are matched in English (and Windows' English wording): on a source host whose locale is not English — where scp reports "No such file or directory" in that locale — set `LC_ALL=C` for the service, or the withdrawal reads as an ordinary failure and is retried on every poll.
- When `local_dir` already holds a different file of the same name (a newer version arrived while the old one still waits to be pushed), `on_conflict` decides: `rename` (default: the new one becomes NAME-1.ext), `overwrite`, or `skip-identical` (fail). An identical file (same sha256) is never copied again.
- With `delete_remote = true`, each verified file is also queued in the ledger `to_delete` (with the sha256 it was verified with) and deleted from the source host by watchglass's remote helper, in one ssh call per poll. The helper deletes only a regular file directly in `remote_dir` that still has the same size and modification time **and the same content** (it re-hashes it, so even a rewrite with the same size and modification time is kept). Only files pulled while the option is on are deleted (they carry the verified sha256 the check needs); files pulled before stay on the source host, and a queued entry without a sha256 is moved to `kept` with a WARNING. A rewrite that keeps both size and modification time is invisible to the watcher (as to any watcher that goes by modification times). A delete problem never fails the poll; a failed delete is retried at the first poll after `delete_retry`, which without new files comes with the watch's `interval`. Needs `checksum = true`. A file replaced in the instant between the helper's check and its delete cannot be told apart; producers should write under a temporary name and rename, as most tools do.
- The same ledger is the observer's `skip_ledger`: after a reconnect, files already pulled are neither hashed nor reported again. Without `delete_remote`, files stay on the source host and a pulled file is pulled again only if its size or modification time changes. The ledger `pulled` has no expiry — a file that stays on the source has to stay remembered — so it grows for as long as the watch runs, and the whole list travels to the watcher on each reconnect; with `delete_remote` the entries it deletes leave it again. A poll records its whole queue in one ledger write, not one per file.

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
- **on_true** pushes each file, then its `NAME.sha256` marker (`marker = "none"` to skip), then deletes it (`after = "delete"`, the default), moves it to `archive_dir` (`"archive"`, pruning files older than `keep_for`) or keeps it (`"keep"`; the ledger `pushed` remembers it). A rejected credential or host key stops the queue after that one failed login and parks the watch immediately (`max_auth_failures`; see `watchglass docs failures`), so it is never asked again for every file.

The two chain naturally through a staging folder: pull's `local_dir` is push's `local_dir`. See `watchglass docs relay` for the whole setup.

## mirror: a tree kept in sync in place

```toml
description = "Keep the Downloads tree on linux1"
recipe = "mirror"

[settings]
direction = "push"          # push: local_dir -> remote:remote_dir; pull: the reverse
remote = "me@linux1"
remote_dir = "/home/me/Downloads"
local_dir = 'C:/Users/me/Downloads'
delete = false              # propagate the source's deletions
ignore = [".git", "__pycache__", "*.tmp"]
mtime_tolerance = 2.0       # seconds; FAT and NTFS round mtimes to two
```

Where `pull` and `push` are queues — one file per unit, staged, then renamed or archived — the mirror keeps a
**living tree equal to another one, in place**: a changed file is overwritten rather than renamed aside, and the
difference is by size and modification time, as rsync does it. This is for the case `pull` and `push`
deliberately do not serve: a tree that has to stay a tree, over plain ssh and scp (what is left when the network
blocks rsync).

- **The destination is mirror-owned.** The mirror never scans it: the source side is the truth, and the
  checkpoint `mirror` (see `watchglass docs ctx`) is its record of what has been synced. That is what makes a poll
  cheap, and it is the one thing to be clear about before turning `delete` on: `delete = true` removes what the
  mirror synced and the source no longer has, and it never removes a file that was already in the destination
  before the mirror ran. Point it at a directory nothing else writes.
- **push** walks `local_dir` itself — no ssh at all, the source is local — after probing the host
  (`reachable_host`/`reachable_port`, else `remote`/`port`; `reachable_timeout`). **No answer means unknown**: no
  failure, no backoff, the tree simply waits. A walk that could not read a subtree is `unknown` too, never a
  deletion — a partial view must not look like a source that lost files.
- **pull** is driven by one live ssh connection running watchglass's remote watcher in tree mode: it reports the
  difference against the checkpoint (`files`, `dirs`, `gone`) and each of those wakes the watch. A file deleted on
  the source while watchglass was down is still reported when the connection returns, because the checkpoint is the
  watcher's expected list. A quiet connection means "nothing to do", never "everything was deleted"; if the event
  queue ever drops one, the observer drops the connection, reconnects, and the walk re-reports from the
  checkpoint.
- **Modification times travel with the files** (`scp -p`), so the two trees agree and a file that did not change
  is never copied twice; `mtime_tolerance` absorbs the two-second rounding of FAT and NTFS. Where scp could not
  set the time (Windows' `local set times: Input/output error`), the pull side sets it from the time the watcher
  reported, and says so in the log if even that fails.
- **Work is recorded as it lands.** The checkpoint is saved in batches (500 entries or 64 MiB), so a killed poll
  retries only what is missing and a service restart repeats nothing. A file that fails is logged, the rest of
  the plan still runs, and the poll fails — so the watch backs off and tries again instead of quietly staying
  behind; the failed file's entry is not recorded, so it is copied next time.
- Remote directories are created in batches (`mkdir -p`, at most 100 paths per ssh call) and deletions are
  batched the same way, with one `find … -type d -empty -delete` for the directories that just became empty;
  paths with spaces or shell characters are quoted for the remote shell. Beyond Python 3.6 for the watcher,
  nothing is installed on the other host.

Not in this recipe: symlinks (skipped, and said so in the log), resuming a half-copied file (the next poll copies
it again, since its entry was never recorded), a Windows host on the remote side, and a `verify` mode that would
scan the destination as well. If drift on the destination ever matters, that last one is the next step — not
something this recipe pretends to do.
