# keepwatch sweep: delete after a safe pull, verified arrival — design

Date: 2026-10-02
Status: design approved in conversation; written spec awaiting review
Release target: 2026.10.4
Builds on: `2026-10-01-keepwatch-observers-relay-design.md` (observers, transfers, recipes, relay; released 2026.10.3)

## 1. Purpose

The user's first plugin is a **sweep**: a file spawned on linux1 must end up on linux2 as if linux1 had written
it there directly, and disappear from linux1 once it is safely on the hub (Windows). keepwatch runs on the hub
(the relay) and also on linux2 (to act on each file when it arrives).

Facts from the user (2026-10-02):

- linux1 writes into **one flat directory**; files land in one directory on linux2.
- A burst is **a few large files** (`.tar.gz`, MBs to GBs); connection overhead per file does not matter.
- File names **never repeat**.
- The **`NAME.sha256` marker** on linux2 is fine; the reader there waits for it.
- linux1 → hub works over ssh/scp with keys; hub → linux2 is scp only (password); IT blocks rsync.

The relay (2026.10.3) already moves files linux1 → staging → linux2 with verification and markers. This design
adds what the sweep still lacks, as general tools: **deleting the source after a verified pull**, and
**reporting a file on the receiving side only once its marker has arrived and matches**.

Not in scope (no use case yet): directory mirroring, recursive sweeps, batching many files per scp, rsync.
`ScpOptions` keeps room for a later `method = "rsync"`.

## 2. Deleting from the source after a safe pull

### 2.1 Configuration

The `pull` recipe gains `delete_remote` (bool, default `false`): delete each file from the source host once its
copy in `local_dir` is verified. `delete_remote = true` requires `checksum = true` (the default); otherwise it is
a config error ("deleting the only copy needs a verified pull: set checksum = true").

### 2.2 When a file is deleted

A file becomes **eligible** only after all of: pulled through `.NAME.part`, size and sha256 verified against the
remote watcher's report, renamed into `local_dir`, and its key (`"<path>|<size>|<mtime!r>"`) added to the ledger
`pulled`. In the same step its key is added to the ledger **`to_delete`**.

`on_true` first pulls the new files (as today), then deletes every eligible key in `to_delete` that is due, in
**one ssh call** to the source host. A key is due when it was added (or last attempted) at least
`delete_retry` ago, except right after its own pull, when it is due at once. `delete_retry` is a new pull
setting (duration, default `10m`).

`check` answers TRUE when there are new files **or** due keys in `to_delete`, so pending deletions are retried
on later polls even when nothing new arrives. Its payload stays the list of new files.

### 2.3 The delete helper

`remote_watcher.py` gains a delete mode, sent over ssh's stdin exactly like the watcher (nothing installed,
Python 3.6+, standard library only, ASCII). Options: `{"mode": "delete", "dir": <remote_dir>, "files":
[{"path", "size", "mtime"}, ...]}`. For each file it:

1. requires the path's directory to be `dir` itself (resolved with `os.path.realpath`), so it never deletes
   outside the watched directory;
2. `lstat`s the path: it must be a regular file (never a symlink or anything else);
3. requires the size and `repr(st_mtime)` to equal the pulled version's, so a file changed or replaced since the
   pull is kept;
4. unlinks it.

It prints one JSON line per file: `{"event": "deleted" | "gone" | "changed" | "refused" | "failed", "path",
"reason"?}`, then `{"event": "done"}`, and exits 0 (2 for bad options).

The ssh command line is the observer's (same `remote`, `port`, `identity`, `known_hosts`, `ssh_options`,
`ssh_command`, `remote_python`), run through `ctx.run` (logged as a `command` record, capped by the hook's
deadline). The functions that build it (`remote_command`, `watcher_source`) move to a new stdlib-only module
`keepwatch.remote`, because the recipe runs in the Python worker, which cannot import `observers.py`
(it needs `watchdog`); `observers.py` imports them from there.

### 2.4 Results

| Helper result | Effect on `to_delete` | Log (plugin record) |
|---|---|---|
| `deleted` | removed | INFO `deleted NAME from <remote>` |
| `gone` | removed | INFO `NAME was already gone from <remote>` |
| `changed` | removed | WARNING `NAME changed on <remote> since it was pulled; kept there` |
| `refused` | removed | WARNING with the reason (not a regular file, outside `remote_dir`) |
| `failed` | kept, stamp refreshed (retried after `delete_retry`) | WARNING with the reason (e.g. Permission denied) |
| ssh fails (unreachable, auth) | all kept, stamps refreshed | WARNING with ssh's last stderr lines |

A delete problem never fails the poll: the pull succeeded, and failing would back off and eventually take the
watch offline over a cleanup step. A deletion never causes a second pull (the key stays in `pulled`).

## 3. Verified arrival on the receiving side

### 3.1 Configuration

The `files` observer gains `marker` (`"none"` default, or `"sha256"`).

### 3.2 Behaviour with `marker = "sha256"`

- Files named `*.sha256` are never reported.
- A settled file `NAME` is reported only when `NAME.sha256` exists beside it, is settled too, and its first
  field (the hex digest, `sha256sum` format: `HEX  NAME` or `HEX *NAME`) equals the sha256 of `NAME`, computed by
  the observer. The event gains `"sha256"` and `"marker"` (the marker's path).
- No marker yet: the file waits silently (the marker's arrival triggers a rescan).
- Mismatch, or a marker that is not a sha256 digest: one `observer.output` WARNING per (file version, marker
  version); the file is reported once either changes and they match (a re-upload).
- Each verified file is reported once per (path, size, mtime), as before.

### 3.3 On linux2

A watch there declares `[observe.arrivals] kind = "files"`, `path = "~/incoming"`, `pattern = "*.tar.gz"`,
`marker = "sha256"`, and its hook acts on `ctx.events` (each a complete, verified file), recording handled files
in a ledger. Deleting the file and its marker afterwards is the hook's choice. A new example, `relay-receive`,
shows it.

## 4. Push: one scp per file (revised in section 8: two runs)

`transfer.push` uploads the file and its marker in **one scp run** (`scp FILE NAME.sha256 user@host:dir/`), so
there is one connection and, for linux2, one password login per file. scp sends its sources in order, so the
data still arrives before the marker. `copy` gains support for several sources into one remote directory for
this. With `marker = "none"` nothing changes.

## 5. Docs and examples

- `relay-pull` example: a commented `# delete_remote = true` line explaining it.
- New example `relay-receive` (the linux2 side, §3.3) with a `watch.py`.
- `keepwatch docs relay`: a "Sweep: delete from the source" section and a "Receiving on linux2" section.
- `keepwatch docs recipes`: `delete_remote`, `delete_retry`; `keepwatch docs observers`: `marker`.

## 6. Decisions

| Decision | Reason | Rejected |
|---|---|---|
| Delete through a one-shot ssh running keepwatch's helper | the observer session stays read-only; deletion works even while the observer reconnects | a two-way protocol over the observer's stdin |
| Delete only after a verified pull, and only an unchanged regular file in `remote_dir` | the hub's copy is proven identical before the only other copy goes; a replaced file is never lost | deleting by name; deleting after the push to linux2 (the user wants linux1 cleared once the hub has it) |
| Delete problems never fail the poll; retried every `delete_retry` | a cleanup step must not take the watch offline | failing the action |
| Marker-verified arrival as a `files` observer option | the receiving watch's hook only ever sees complete, verified files | each hook re-implementing marker waits |
| File and marker in one scp | one password login per file | two scp runs |
| No mirror, batching or rsync now | no use case; the sweep is a few large files | building them speculatively |

## 7. Testing

- Delete mode: selftests (Python 3.6 in CI and the main suite) for deleted, gone, changed, refused (symlink,
  outside `dir`) and failed (a read-only directory, POSIX non-root) results.
- Pull recipe with `delete_remote`: the in-process scp server with a non-chroot root and the fake ssh (which runs
  the helper locally): the file is pulled, verified and deleted; a changed file is kept; a failure keeps the key
  and is retried after `delete_retry`; the poll never fails because of a delete.
- `files` observer with `marker = "sha256"`: waits without a marker; reports with a matching one; warns once and
  holds on a mismatch; reports after a corrected re-upload; never reports markers.
- `push`: one scp run carries file and marker (reports count), data first.
- End-to-end: linux1 → hub → linux2 through the service with `delete_remote = true`, and a receiving watch with
  `marker = "sha256"` sees the file once, verified; the source file is gone.
- By hand before release: the real sshd lab container (tcsh login, Python 3.6) for the delete helper.

## 8. Revisions after the code review (2026-10-02)

- **Content check before deleting.** `to_delete` entries carry the sha256 the pull was verified with
  (`"<path>|<size>|<mtime!r>|<sha256>"`; `-` when unknown). The helper re-hashes the file and keeps it
  ("changed") on a mismatch, so a rewrite with the same size and preserved mtime (`cp -p`, `touch -r`) is never
  deleted. (A replacement in the instant between the hash and the unlink cannot be excluded; the docs say so.)
- **Results by position.** The helper's result lines carry `"index"` (position in `files`), so two queued
  versions of one name cannot get each other's result.
- **Backfill and bounded ledgers.** With `delete_remote`, every key in `pulled` that is neither queued nor in
  the ledger `kept` counts as due, so files pulled before the option was switched on (or before a crash between
  recording and queueing) are deleted too. After `deleted`, `gone` or `changed` the key leaves `pulled` (so the
  skip list stays small) and goes to the ledger `skipped` (entries expire after 7 days), which `check` uses to
  ignore stale events for files that are gone. After `refused` the key goes to `kept`: the file stays and is not
  retried.
- **Stale events never fail the poll.** A pull whose size or sha256 differs from the report (`TransferMismatch`)
  means the file changed after it was reported: a WARNING, and the poll goes on; the new version is reported
  under its own key. (Before, such an event failed every poll until the watch went offline.)
- **Deletes and pull failures.** After a failed pull, deletes run only for files pulled in that action, and a
  delete problem never replaces the pull's error.
- **Push reverts to two scp runs.** scp carries on to its next source after a failed one, so one run could put
  `NAME.sha256` beside a truncated `NAME`. The marker is sent only after the data succeeded; the extra login per
  file is irrelevant for a few large files. `copy(..., extra_sources=...)` remains as a general tool.
- **Marker waits.** A file waiting for its marker is re-checked every second (it stays a candidate); only a
  mismatched one waits for a filesystem event or the 30s rescan.
- **Retry timing.** A due delete is attempted at the watch's next poll; without new files that poll comes from
  `interval`, so a retry happens after `delete_retry` or `interval`, whichever is later.

## 9. Second revisions (2026-10-02, after reviewing section 8)

- **No backfill.** Only files pulled (and verified) while `delete_remote` is on are deleted, because only they
  carry a recorded sha256 for the helper's content check. Keys in `pulled` that were never queued are logged
  once and left alone (delete them by hand). To close the crash window, a file's `to_delete` entry is written
  *before* its key goes into `pulled`.
- **`changed` keeps the new version moving.** After a `changed` result the key leaves `pulled` but does not go
  to `skipped`, so the new content is pulled at its next report. `skipped` is only for `deleted` and `gone`.
- **Timeouts keep partial results.** When the delete call times out, the lines it printed before the timeout
  still count (files already deleted are not re-queued as failures).
- **In-place rewrites that keep size and mtime** are invisible to the remote watcher (as to any mtime-based
  watcher): a pull that then mismatches is skipped with a WARNING, and the docs say so.
- **Waiting for a marker** re-checks every second for at most 60s after the file settled; after that only
  filesystem events and the 30s rescan re-check it (a stray file without a marker costs nothing).
- **Cleanups:** one hashing loop in the remote helper; `extra_sources` removed again (nothing uses it);
  `with_known_hosts` lives in `transfer.py`, so `keepwatch.remote` is stdlib-only again; the receive example keys
  arrivals by path, size, mtime and sha256.
- Two scp runs per pushed file stay (section 8): the marker must follow a successful upload.

## 10. Third revisions (2026-10-02, after reviewing section 9)

- **The marker is part of a file's version** (replaces the 60s marker wait of section 9). The `files` observer
  tracks `(size, mtime_ns)` of the file and of its marker together: when the marker appears or changes, the pair
  settles again and is checked then. A file that settled without a matching marker is held, costs nothing, and
  is checked again only when the file or its marker changes (a notification or the 30s rescan). No timers.
- **Reads do not trigger rescans.** watchdog 6 reports opens and closes after reading; the observer ignores
  them, so its own hashing (or any reader) does not cause another scan.
- **Entries without a sha256 are never deleted.** A `to_delete` entry with `-` (none written now, but earlier
  builds wrote them) goes to `kept` with a WARNING instead of reaching the helper.
- **Delete timeouts keep ssh's stderr** next to the hint about `action_timeout`.
- **The receive example checks before moving**: a file whose size or mtime differs from its report is left for
  its next report.
- **Known limit, documented:** a rewrite that keeps size and mtime is not re-reported by the remote watcher
  until it reconnects; after a `changed` delete result such a file stays on the source host.

## 11. Last revisions (2026-10-02)

- **The delete helper requires a sha256**: an item without one is `refused`, whoever sends it (the pull recipe
  already never sends one).
- **An unreadable file stays a candidate** (on Windows a file can be locked by its writer): it is checked again
  after another settle period instead of waiting for the 30s rescan.
- Accepted as they are: on Windows with last-access updates enabled, a read can cause one extra scan (never a
  loop: a held file is not re-read); where notifications are missed, a late marker is seen by the 30s rescan.
