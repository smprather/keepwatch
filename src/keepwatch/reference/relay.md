# Relay: host A → this machine → host B

A common setup: files appear on a source host (linux1) and must reach a destination host (linux2) that this machine, the **hub**, can only upload to, and that is often disconnected. keepwatch on the hub does it with two watches chained by a staging folder:

| Host | Reachability | Role |
|---|---|---|
| hub (keepwatch, often Windows) | ssh/scp to linux1; scp uploads to linux2 | runs both watches |
| linux1 | none needed towards the hub or linux2 | produces the files (Python 3.6+ present) |
| linux2 | accepts scp uploads from the hub (password), often away | receives the files |

1. **relay-pull** (`recipe = "pull"`): one long-lived ssh connection to linux1 runs keepwatch's remote watcher, which reports every settled file with its sha256 (inotify where linux1 has it, rescans where not, catching up after every reconnect). Each new file is pulled into the staging folder through a hidden `.NAME.part` file, verified, renamed, and recorded in the ledger `pulled`.
2. **relay-push** (`recipe = "push"`): when a file settles in the staging folder and linux2 answers on its ssh port, the file is uploaded, then `NAME.sha256`, and the staged copy is deleted. While linux2 is away the check answers **unknown**: nothing fails, nothing goes offline, files simply wait in the staging folder (the queue).

Both are in `keepwatch docs examples` (`relay-pull`, `relay-push`).

## Setup on the hub

1. **Install keepwatch for good:** `uv tool install keepwatch` (not `uvx`: `keepwatch install` refuses temporary environments), then `keepwatch init`.
2. **A key for linux1:** `ssh-keygen -t ed25519 -f ~/.ssh/id_relay` (no passphrase, or one held by ssh-agent; on Windows the "OpenSSH Authentication Agent" service), then append `~/.ssh/id_relay.pub` to `~/.ssh/authorized_keys` on linux1. Set `identity = '~/.ssh/id_relay'` in relay-pull's `[settings]`.
3. **Known host keys:** connect once by hand to each host and accept its key: `ssh me@linux1 true`, and for linux2 `scp some-small-file me@linux2:incoming/` (enter the password once). keepwatch never accepts an unknown host key; the keys land in `~/.ssh/known_hosts` (or point `known_hosts` at a file of your own).
4. **Silent login shells:** the login scripts on linux1 and linux2 must print nothing for non-interactive sessions, or scp breaks ("Received message too long"). In `.cshrc`/`.tcshrc`: wrap any `echo` in `if ($?prompt) then … endif`; in `.bashrc`/`.profile`: `case $- in *i*) … ;; esac`.
5. **The password for linux2:** keep it in an environment variable, never in a file keepwatch reads. Windows: `setx RELAY_PASSWORD "…"`, then restart keepwatch (it only sees variables that existed when it started). Linux: an `Environment=` line in `systemctl --user edit keepwatch`.
6. **The watches:** copy the two examples into your watches directory (`keepwatch docs examples` prints them), set `remote`, `remote_dir` and `dest`, and give both the same path in `local_dir` (the examples use `~/relay/staging`; a relative path would be relative to each watch's own directory, and the two would never meet), then `keepwatch validate relay-pull relay-push`.
7. **Try each part:** `keepwatch observe relay-pull remote --for 1m` must print the files on linux1 as JSON lines; `keepwatch poll relay-push --dry-run` shows whether linux2 is reachable (unknown when it is not). Then `keepwatch install`.

## Receiving on linux2

Run keepwatch on linux2 too, with a watch like the `relay-receive` example: a `files` observer with `marker = "sha256"` on the incoming directory. It reports `NAME` only once `NAME.sha256` has arrived beside it and matches, so the hook never sees a half-uploaded file:

```toml
[observe.arrivals]
kind = "files"
path = "~/incoming"
pattern = "*.tar.gz"
marker = "sha256"
```

Each event carries the verified `sha256` and the `marker` path; record handled files in a ledger (events can come more than once) and move or delete the file and its marker when done. Without keepwatch there, a script can do the same: wait for the marker, then `sha256sum -c NAME.sha256`.

## Sweep: delete from the source

Set `delete_remote = true` in relay-pull to delete each file from linux1 once its copy on the hub is verified (size and sha256) and recorded. Only an unchanged regular file directly in `remote_dir` is deleted: a file that changed since it was pulled stays on linux1 (with a WARNING). Deletes go through keepwatch's remote helper in one short ssh call per poll; a delete that fails (permissions, linux1 away) never fails the poll and is retried after `delete_retry` (10 minutes). linux1 then holds only files that have not reached the hub yet, plus any it refused to delete (a symlink, say) or that changed after they were pulled; both are logged.

## Watching it work

- `keepwatch status` shows each watch, its pending events and its observer (running, restarts, last event).
- `keepwatch logs relay-pull --event observer.stopped` tells why the ssh connection to linux1 dropped (ssh's own last lines are in `stderr_tail`).
- `keepwatch logs relay-push --failed` shows failed uploads (a wrong password says "Permission denied").

## Variations and limits

- **Keep or archive instead of delete:** `after = "keep"` or `after = "archive"` (with `archive_dir`, `keep_for`) in relay-push.
- **Direct copy, no staging (`scp -3`):** only with SFTP on both hosts and keys on both (classic scp reports success even when the destination fails, so keepwatch refuses it); not for an upload-only, password-only destination like linux2. Write a watch.py using `ctx.transfer.copy` if your hosts allow it.
- **Several sources:** one relay-pull watch per source host, all writing into the same staging folder.
- Files stay on linux1; a pulled file is pulled again only if its size or modification time changes.
