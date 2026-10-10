# keepwatch mirror: mtimes that survive transfers, a check-readable checkpoint, and `recipe = "mirror"` — design

Date: 2026-10-09
Status: design written; awaiting review before implementation
Release targets: 2026.10.18 (kit mtimes), 2026.10.19 (`ctx.checkpoint`), 2026.10.20 (`recipe = "mirror"`)
Builds on: `2026-09-29-keepwatch-design.md` (§9 plugin API, §5 config), `2026-10-01-keepwatch-observers-relay-design.md`
(the remote watcher, `remote_files`), `2026-10-02-keepwatch-sweep-design.md` (remote delete mode, `marker`), and
2026.10.6–17 (transfers, `resume`, the refused-overwrite case)

Issues: #12 (mtimes), #13 (a persistent snapshot a check may read), #11 (the mirror recipe)

## 1. Purpose

Three issues, one subsystem. #11 asks for `recipe = "mirror"`: keeping a recursive tree in sync **in place** over
plain ssh/scp — copy what changed, propagate deletions, excludes pruned at the walk — because rsync is blocked at
the DPI layer in the target network. #12 and #13 are its foundations: #12 because a mirror diffs by size and mtime,
so a transfer that does not carry the source mtime makes every synced file look changed; #13 because the record of
what was synced must survive a restart, and a check may read a persistent record but not write one.

The workload behind it is real and is the acceptance test: `C:\Users\mylesp\Downloads` → a Linux host, and a config
tree back, on Windows 11 with Win32-OpenSSH 10.0p2. Today that runs as two custom watches (~450 lines of plugin
code) plus raw `scp -p` calls that bypass `ctx.transfer` and its host-key checks and logging. When this lands,
those become two `config.toml` files.

**Non-goals.** A `verify` mode that scans both sides (later, once mtimes are trustworthy); rsync's delta-transfer
algorithm, hard links, ownership and permission replication, and symlink replication (regular files only — a
symlinked file is skipped and logged); resuming an interrupted mirror transfer (the next poll repairs it); a
remote host that is not POSIX with Python 3.6+ (the observer contract already requires that); and making the
watcher's tree mode a public observer kind (section 9, decision 1).

## 2. The three releases, and why

- **2026.10.18 — kit carries mtimes (#12).** Independently useful, small, and needed before a mirror can diff by
  mtime. Closes #12.
- **2026.10.19 — `ctx.checkpoint(name)` (#13).** Independently useful to any watch that must not re-do work after a
  restart. Closes #13.
- **2026.10.20 — `recipe = "mirror"` (#11), including the remote watcher's tree mode.** Closes #11.

The helper's tree mode folds into `.20` rather than getting its own release: it has exactly one consumer, and a
release whose deliverable is an internal mode nobody can use yet is a release without a user. The tree mode is
built as the first phase of `.20`, its own module-level tests, before the recipe consumes it. Ratchet, so this
stays a decision and not a slide: if that phase outgrows the release, it ships first as `.20` "the remote watcher
speaks trees" and the mirror becomes `.21`.

## 3. Release .18 — transfers that carry mtimes (#12)

### 3.1 The option

`ScpOptions` gains `preserve_mtime: bool = False`, so `ctx.transfer.copy/pull/push(..., preserve_mtime=True)` and
every existing caller work unchanged; default off keeps today's argv byte for byte. `keepwatch kit copy`, `pull`
and `push` gain `--preserve-mtime` (the flag the whole `kit` family already shares its scp options through).

### 3.2 The argv

- `scp_argv()` appends `-p` when the option is set. `scp -p` preserves modification times, access times and mode
  bits in both protocol modes — classic (`-O`) and the sftp protocol scp uses by default on OpenSSH ≥ 9 — because
  scp sets the times itself through `SSH_FXP_SETSTAT` in sftp mode.
- `sftp_argv()` (the `resume` path: `reget`/`reput`) appends `-p` too; `sftp(1)`'s `-p` covers the file commands
  it runs.
- Mode bits ride along with the times; that is scp's behaviour, not separable, and is documented rather than
  worked around.

### 3.3 The `set times` case

A transfer whose **data** succeeded can still fail on the times step, in two shapes seen in the field:

- Windows local side, a pull: `scp -p` exits non-zero with `local set times: Input/output error` although the file
  arrived (this repo's own transfer code and `laptop-sync` both hit it).
- A server refusing the times: `set times: Operation not permitted` (Posh-SSH reports the same shape).

When `preserve_mtime` is on and scp fails with a `set times` error, keepwatch treats the transfer as **succeeded
with a warning**, in the same family as `_refused_overwrite`: the times step comes last, so that stderr is a
strong signal the data landed. For a pull, the warning is raised only when the `.NAME.part` has passed its size
and sha256 checks (when given), so a broken transfer still fails. The warning names the file and says its
destination mtime is the transfer time. Without `preserve_mtime`, a `set times` failure remains a failure.

### 3.4 What .18 does not do

It does not repair a remote mtime after a push (that would cost an ssh call per file, and the mirror does not need
it: its checkpoint records the *source* state, not the destination's). It does not learn a remote mtime for a
pull — the caller that knows it (the mirror, from its walk or its events) repairs locally with `os.utime` after the
transfer, which is the robust path on Windows.

## 4. Release .19 — `ctx.checkpoint(name)` (#13)

### 4.1 The API

```python
state = ctx.checkpoint("mirror")   # letters, digits, '_', '.', '-'; same pattern as ctx.ledger's names
state.value        # the JSON document, or None if it has never been saved
state.save({...})  # replace the document, atomically; raises CheckpointReadOnly in check()
state.clear()      # forget it; raises CheckpointReadOnly in check()
```

`CheckpointReadOnly` and `CheckpointCorrupt` mirror `LedgerReadOnly` and `LedgerCorrupt`, including the corrupt
message naming the file to fix or delete. `checkpoint_file(data_dir, name)` sits beside `ledger_file`. The
docstrings are the source of `keepwatch docs ctx`.

### 4.2 The file

`data_dir/checkpoints/NAME.json`, written through a temporary file, `fsync`, then `platform.replace` — the same
atomic dance as a ledger, so a reader sees the old document or the complete new one. `sort_keys=True` for stable
diffs. No expiry: a checkpoint is a record, not a queue.

### 4.3 What it is not

A checkpoint is for a **document** (a tree listing, a cursor, a marker), rewritten whole on each save. Per-key
churn stays the ledger's job: a ledger stamps each key and rewrites only on add/discard, and its read-only-in-check
discipline is already the model this follows.

### 4.4 The pattern it documents

`keepwatch docs ctx` gains the *promote on success* pattern next to the new member: a check reads the checkpoint
and proposes work; the action performs the work and promotes the checkpoint **as each unit succeeds** (or in
bounded batches, section 5.5); therefore a failed or killed poll retries what is still missing and a restart
repeats no work. Checks run for real during `--dry-run`, which is exactly why they may not write.

## 5. Release .20 — `recipe = "mirror"` (#11)

### 5.1 Settings

```toml
recipe = "mirror"

[settings]
direction = "push"           # push: local_dir -> remote:remote_dir; pull: the reverse
remote = "linuxbox"          # user@host, or a Host alias from ~/.ssh/config
remote_dir = "/home/user/mirror"
local_dir = 'C:/Users/mylesp/Downloads'
delete = false               # propagate source deletions to the destination
ignore = [".git", "__pycache__", "*.tmp"]   # name globs, directories pruned at the walk
mtime_tolerance = 2.0        # seconds; FAT/NTFS round mtimes to two
settle = 0.0                 # skip files modified this recently (0: mirror what is there now)
file_timeout = 0.0           # as in push/pull: 0 shares action_timeout between the files
# plus the transfer settings pull/push share: protocol, password_env, password_mode, identity,
# known_hosts, ssh_options, ssh_command, remote_python, port, reachable_host, reachable_port,
# reachable_timeout, part_max_age (cleanup of `.NAME.part` files from killed pulls)
```

`remote` + `remote_dir` in both directions — `push`'s single `dest` cannot serve a pull, and deletions need the
directory named on its own. `ignore` defaults to empty, unlike the queue recipes' `DEFAULT_IGNORE`: a mirror's job
is fidelity, so dotfiles are mirrored unless named. `pattern`, `marker`, `after`, `checksum`, `on_conflict`,
`resume` and `archive_dir` have no meaning here and are not accepted.

### 5.2 The invariant: the destination is mirror-owned

The mirror never scans the destination. It is derived: the source side is the truth, the checkpoint is the record
of what has been synced, and the destination is assumed to be written by nothing else. That is what buys the cheap
poll, and it is the honest boundary of this design:

- `delete` removes what **the mirror synced** and the source no longer has. A file that was already in the
  destination before the mirror ever ran is never removed — the mirror does not know it exists.
- A hand-edit on the destination is invisible until it collides with the source; the next sync repairs it.
- A `verify` mode that scans both sides and repairs drift is the later, separate step.

### 5.3 What the checkpoint holds

```json
{"version": 1,
 "dirs": ["sub", "sub/deep"],
 "files": {"sub/a.txt": [1234, 1759999999.5]}}
```

Relative POSIX paths; `files` maps to `[size, mtime]`. It is the source's listing at the last successful sync —
not the destination's. Both directions use the same document, so a direction change is a rescan, not a migration.

### 5.4 Direction `push`: the check

Reachability first: `ctx.transfer.tcp_open(host, port, reachable_timeout)`; no answer raises `Unknown` and files
wait (no failure count, no backoff), exactly as `push` does. Then a local `os.walk` of `local_dir`, `ignore`
matched against names and pruned for directories, files below `settle` skipped, symlinks skipped and logged. The
walk collects errors instead of swallowing them (`os.walk`'s `onerror`): a subtree that could not be read is not
an absence, so a walk that did not finish completely answers `Unknown` rather than proposing deletions.

Diff against the checkpoint: a path whose size differs, or whose mtime differs by more than `mtime_tolerance`, is
to copy; a path not in the checkpoint is to copy; a path in the checkpoint that the walk did not see is to forget
— and to remove at the destination only with `delete = true`; directories new to the walk are to create. Forgetting
is work, so the check answers TRUE for it even when `delete = false`. Nothing to do answers FALSE. The check makes
no ssh calls.

### 5.5 Direction `push`: the action

In order, all under `file_budget` sharing of the remaining action time:

1. **Directories**: one ssh call per ≤100 paths, `mkdir -p` with each path `shlex.quote`d, for the directories the
   plan needs (a source directory, or the parents of a file to copy).
2. **Files**: one `ctx.transfer.push(path, f"{remote}:{remote_dir}/{parent}", marker="none",
   preserve_mtime=True)` per changed file — `push` gives the progress record the TUI reads and the refused-overwrite
   diagnosis, without writing `.sha256` markers a mirror does not need.
3. **Deletions**, only with `delete = true` and only from the complete walk of this poll: files in ≤100-path ssh
   batches of `rm -f`, then one `find <remote_dir> -type d -empty -delete` to take the directories that just became
   empty (this also removes empty directories the mirror never created; they hold nothing, so nothing is lost).
4. **Promotion**: the checkpoint's entries are updated for what landed. Promotion is **batched** — when the
   un-promoted work since the last save passes 500 files or 64 MiB of file size, and once at the end. A kill
   therefore re-copies at most that much; per-file promotion was rejected because a whole-document rewrite per file
   makes a large first sync quadratic (a 20 000-file tree would write tens of gigabytes of checkpoint).

A single file's failure is logged and the rest of the plan carries on, then the first failure is raised — the
existing recipe pattern. An interrupted push leaves a truncated file at the destination with the wrong mtime, so
the next diff copies it again (its entry was not promoted).

### 5.6 Direction `pull`: the tree watcher, and why absence is never a deletion

The pull side is the remote side, so it is watched, not scanned: the recipe declares its own observer, which holds
one ssh to `remote_dir` running the remote watcher in tree mode (section 6), and the watch is woken when events
arrive. `tcp_open` is still probed in the check for the unreachable case, so a host that is away answers `Unknown`
rather than "nothing to do".

The safety rule that makes this work: **a check never infers a deletion from the absence of an event.** A file the
checkpoint knows is deleted locally only when the watcher says so — a `gone` entry in a delta, or a first walk
that expected the path and did not find it. A quiet stream (connection down, walk not finished, poll before the
first walk) means "do nothing", never "everything was deleted". With `delete = false` a `gone` still removes the
path from the checkpoint: the mirror no longer manages it, and if the source re-creates it, it arrives as a new
file.

### 5.7 Direction `pull`: check and action

The check consumes `ctx.events` (the watcher's `files`, `dirs` and `gone` payloads) and diffs them against the
checkpoint: to copy = paths the checkpoint does not have, or has with a different size or mtime (by more than
`mtime_tolerance`); to create = directories unknown to the checkpoint; to forget = `gone` paths, always, because
the mirror no longer manages them; to remove locally = those same paths, only with `delete = true`. Forgetting is
work too, so the check answers TRUE for a batch of `gone` paths even when `delete = false`. Nothing to do answers
FALSE.

The action creates directories with `os.makedirs`, pulls each file with
`ctx.transfer.pull(f"{remote}:{path}", local_dir, on_conflict="overwrite", preserve_mtime=True)` into its
subdirectory, and — this is the Windows repair of section 3.3 — sets the local mtime from the event's mtime with
`os.utime` when it differs by more than the tolerance, so the destination carries the source's time even when
`scp -p` could not set it. Deletions unlink the local files and then remove now-empty directories that the
checkpoint knew, deepest first. Stale `.NAME.part` files are cleaned up with the `pull` recipe's existing helper.

### 5.8 Failure semantics

- Host unreachable → `Unknown`, files wait, no failure count. Same both directions.
- A source walk or event stream that is not complete is not authoritative: the push check only deletes from a walk
  that finished, and the pull check only deletes on a `gone` the watcher stated. Nothing is ever deleted on a
  partial view.
- A transfer failure is logged and the rest of the plan continues; the first failure is raised at the end.
- A watcher reconnect is not a failure of the watch: the observer owns it (section 6.4).

### 5.9 Windows notes

The issue's Windows lessons are **dissolved** rather than handled: no `find -printf` (the remote side is a Python
helper walking the tree and writing JSON lines, so no null-byte text pipes and no `-print0`), no ssh multiplexing
(one long-lived connection for pull, discrete short calls for the push-side batches, and `remote.ssh_argv` already
sets `ControlMaster=no` deliberately), and every batched remote command line is built for ≤100 paths so the ~32k
`CreateProcess` limit is never approached. Remote paths in ssh command strings are `shlex.quote`d; scp operands
keep the existing `remote_path_arg` rules for classic/sftp and for Windows.

## 6. The remote watcher's tree mode

### 6.1 Options

`mode: "tree"` alongside `watch` and `delete`, with `dir`, `ignore` (name globs, pruned), `settle` (the recipe's;
a file modified more recently than this is not reported yet, 0 by default so the mirror takes what is there now),
`interval` (the polling fallback, 60 s here), `rescan` (the inotify safety net, 30 s), `heartbeat`, and — new and
only for tree mode — the expected state from the checkpoint:

```text
expected_files: [{"path": "<absolute remote path>", "size": <int>, "mtime": <float>}, ...]
expected_dirs:  ["<absolute remote path>", ...]
```

Structured rather than the `watch` mode's opaque `skip` strings, because tree mode must iterate the expected set to
find what is gone, not merely test membership. The `watch` mode keeps its string format and its behaviour
unchanged, so the `pull` recipe does not move.

### 6.2 Output

`hello` and `ready` are observer-level records (`observer.connected`, `observer.ready` with counts and walk
duration); they are not delivered as events. Delivered:

- `{"event": "files", "entries": [[path, size, mtime], ...]}` — new or changed against the expected state,
  chunked so one line stays well under `MAX_LINE` (1 MiB). Paths are absolute, as the helper sees them; the
  recipe makes them relative to `remote_dir`.
- `{"event": "dirs", "paths": [path, ...]}` — new directories, chunked.
- `{"event": "gone", "paths": [path, ...]}` — expected files or directories that the walk did not find, and
  previously reported paths that are now absent; chunked.

Chunking is what keeps the initial sync of a large tree inside the event queue: the first walk of a 20 000-file
tree is a handful of `files` events, not 20 000 queue entries.

### 6.3 The walk, and the skip list

Each walk compares the live tree against the expected state and its own memory of what it reported:

- a path that matches an expected entry → nothing on the wire (this is what makes a reconnect after a long time
  away cheap: the walk happens on the remote host, the wire carries only the difference);
- a path that differs from its expected entry, or is not expected and not already reported → a `files` entry;
- an expected path the walk did not find → a `gone` entry, then forgotten;
- a previously reported path now absent → a `gone` entry, then forgotten.

So a deletion that happened while keepwatch was down is still reported on the next connect, because the checkpoint
was the expected state.

### 6.4 A dropped event is a reason to reconnect, not a signal

The event queue drops the oldest past 10 000 events or 64 MiB and already logs `observer.dropped`. A tree walk can
overflow it, and a check must never see a stream with a hole in it. Rather than invent a `resync` event the check
must interpret, the observer treats a drop as a reason to drop its connection: the supervisor reconnects, the
helper re-walks from the **current** checkpoint (which the action has been promoting all along), and the delta is
sent again. Duplicates are harmless — every event is diffed against the checkpoint, which is idempotent.

This needs one small signature change: `Deliver` currently returns `None`, so the observer cannot see the drop
count. It returns the dropped count (`int`), and the remote tree observer stops its process when it sees a
non-zero one.

### 6.5 Versioning

`WATCHER_VERSION` bumps to 2. The helper's source is always sent over stdin by keepwatch, so a version skew cannot
arise in practice; the number is in `hello` for the log and for the tests that pin the protocol.

## 7. Docs and examples

- `keepwatch docs recipes`: the `mirror` recipe — its settings table (generated from the config keys), the
  mirror-owned destination invariant and what `delete` does and does not remove, the live connection on the pull
  side, the mtime fidelity story, and the Windows notes as user-facing advice (`--preserve-mtime`, batching).
- `keepwatch docs transfers`: `preserve_mtime` / `--preserve-mtime`, the `set times` trap and when it is a warning,
  and that mode bits travel with the times.
- `keepwatch docs ctx`: `ctx.checkpoint` and the promote-on-success pattern (section 4.4).
- README: one clause in the introduction — built-in recipes move files between machines; the mirror recipe keeps a
  tree in sync in place.
- The three issues are closed with their releases, each release naming the issue it closes.

## 8. Testing

- **`tests/test_transfer.py`**: `-p` appears in `scp_argv` for both protocols and in `sftp_argv`; the default argv
  is unchanged; a `set times` failure with a verified part warns and succeeds, and without `preserve_mtime` still
  fails; a failed data transfer still fails when its stderr mentions times.
- **`tests/test_ctx.py`** (where the ledger tests live): a checkpoint saves and loads; a check gets
  `CheckpointReadOnly` on `save`/`clear`; the write is atomic (a corrupt reader never sees a partial document); a
  corrupt file raises `CheckpointCorrupt` naming it; the name pattern is enforced.
- **`tests/test_remote_watcher.py`**: tree mode under the file's Python 3.6 / stdlib-only / ASCII rules — the walk,
  `ignore` pruning, expected-but-absent → `gone`, reported-then-absent → `gone`, chunking under `MAX_LINE`,
  `ready` counts, and that `watch` mode is untouched.
- **`tests/test_mirror.py`** (new): the pure diff/plan functions against two listings plus a checkpoint — new,
  changed, changed-within-tolerance, deleted, ignored and pruned, `settle`, and the promote batching rule; the
  command builders for the batched `mkdir`/`rm`; the pull path's local `os.utime` repair.
- **`tests/test_config_recipe.py`**: `mirror` settings — required keys, `direction` choice, `mtime_tolerance`
  non-negative, `delete`/`settle` types, `password_mode` needing `password_env`, and that the recipe's observers
  are the tree observer for pull and the recursive files observer for push.
- **Network-marked**: an end-to-end mirror against a local sshd when one is available (`-m network`), otherwise
  documented as a user-machine step. The real proof is the workload: two `config.toml` files where the custom
  watches are today, a scratch tree first, then `Downloads`.
- CI: `uv run pytest -q`, `uv run ruff check src tests`, `uv run ty check src` — the standing gates.

## 9. Decisions taken (say if you disagree)

1. **The tree mode is internal for now.** The mirror recipe declares its own observer; there is no public
   `remote_tree` kind and no `keepwatch docs observers` entry until a second consumer exists. Freezing a
   one-consumer interface as documented surface is how a contract becomes unchangeable.
2. **Three releases, `.20` folding in the tree mode**, with the ratchet to `.21` stated up front.
3. **The destination is mirror-owned and never scanned**; `delete` removes only what the mirror synced, and a
   `verify` mode is a later, separate step. This is the design's one real limitation and it is deliberate.
4. **Absence is never a deletion.** Deletions come only from a stated `gone` or from a walk that finished, which
   is what makes a live-connection check safe when the connection is down, restarting, or has just connected.
5. **A dropped event reconnects the watcher** instead of a `resync` event the check must interpret; the
   checkpoint makes the re-walk exactly equivalent.
6. **Promotion is batched** (500 files or 64 MiB), not per file: per-file promotion of a whole-document checkpoint
   is quadratic on a large first sync.
7. **`preserve_mtime` defaults off** and `set times` failures are warnings only when the data verifiably landed;
   the mirror repairs the pull side's local mtime itself with `os.utime`, which is the robust Windows path.
8. **A mirror ignores nothing by default** (no `DEFAULT_IGNORE`) and does not accept `pattern`: fidelity is the
   job. `marker`, `after`, `checksum`, `on_conflict`, `resume` and `archive_dir` are not mirror settings.
9. **Mirror transfers use `push`/`pull` under the hood** (`marker="none"`, `on_conflict="overwrite"`,
   `preserve_mtime=True`), so host-key checking, askpass/ConPTY, logging and progress stay in one place rather than
   being re-implemented the way the custom watches had to.
10. **The push direction gets a recursive local `files` observer** as a waker (it exists; it gains `recursive` on
    the recipe's own declaration), so a change is pushed without waiting for the interval.
