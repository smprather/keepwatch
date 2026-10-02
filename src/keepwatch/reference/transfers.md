# Transfers: scp for hooks

Moving files between machines is what many watches do. keepwatch gives hooks one safe way to do it, built on the system's OpenSSH `scp` (part of Windows 10/11 and every Linux): **Python hooks** use `ctx.transfer`, **command hooks** use `keepwatch kit`. Every scp run is logged as a `command` record, like `ctx.run`.

```python
def on_true(ctx):
    done = ctx.ledger("pushed")
    for path in ctx.payload:
        ctx.transfer.push(path, "me@linux2:incoming/", password_env="RELAY_PASSWORD")
        done.add(ctx.file_key(path))
```

```sh
keepwatch kit push "$FILE" me@linux2:incoming/ --password-env RELAY_PASSWORD
```

## Endpoints

- A local path (`C:\data\out\a.gz` and `C:/data/out/a.gz` are local: a drive letter is never a host). In `ctx.transfer`, relative paths start at the watch directory.
- `[user@]host:path` (a colon before any slash): `path` is relative to the remote home unless it starts with `/`. `host` may be a Host alias from `~/.ssh/config`.
- `scp://[user@]host[:port]/path` when you need a port inside the endpoint.

At least one side must be remote. **Two remote endpoints** (copying linux1 → linux2 through this machine with `scp -3`) need `protocol = "sftp"` on both hosts and key authentication: with the classic protocol, `scp -3` reports success even when the destination host fails, so keepwatch refuses it. To an scp-only destination, copy through a local folder instead (pull, then push), which is also what lets a destination that is often offline catch up later.

## Operations

| Python | Command hook | What it does |
|---|---|---|
| `ctx.transfer.copy(src, dst)` | `keepwatch kit copy SRC DST` | One scp; nothing else. |
| `ctx.transfer.pull(remote, local_dir, size=, sha256=, on_conflict=)` | `keepwatch kit pull REMOTE DIR [--size N] [--sha256 HEX] [--on-conflict …]` | Copies to `DIR/.NAME.part`, checks size and sha256 when given, then renames to `DIR/NAME`. A failed check deletes the part file, so a half or wrong file never appears under its real name. Returns / prints the final path. |
| `ctx.transfer.push(path, remote_dir, marker=)` | `keepwatch kit push PATH REMOTE_DIR [--marker sha256\|none]` | Uploads `NAME`, then `NAME.sha256` in `sha256sum` format. Returns / prints the remote path. |
| `ctx.transfer.tcp_open(host, port=22, timeout=5)` | `keepwatch kit tcp-open HOST [--port N]` | Whether the port accepts a TCP connection (exit 0 open, 1 closed). Allowed in a check. |

`pull` when `DIR/NAME` already exists: with `sha256` given and matching, nothing is copied (an earlier pull finished); otherwise it fails unless `on_conflict = "rename"` (`NAME-1.ext`) or `"overwrite"`.

**The `.sha256` marker** exists for destinations where uploads cannot be renamed into place (upload-only accounts): the data arrives first, the marker last. A consumer there waits for `NAME.sha256` and runs `sha256sum -c NAME.sha256` before using `NAME`.

## Options

Keyword arguments of `copy`, `pull` and `push` (`--option` for `keepwatch kit`):

| Option | Meaning |
|---|---|
| `password_env` | Name of the environment variable holding the password. Default: keys only. |
| `identity` | A private key file (`ssh -i`); ssh then offers only that key. |
| `known_hosts` | A known_hosts file to check host keys against (default: ssh's own, `~/.ssh/known_hosts`). |
| `port` | ssh port for `user@host:path` endpoints. |
| `protocol` | `"scp"` (default): the classic protocol, which scp-only servers accept; keepwatch adds `-O` on OpenSSH 9 and newer, where scp otherwise speaks SFTP. `"sftp"`: needs OpenSSH 9+. |
| `ssh_options` | Extra scp arguments, placed before keepwatch's own (ssh keeps the first value given), e.g. `["-o", "ProxyJump=bastion"]`. |
| `timeout` | Give up after this many seconds. In `ctx.transfer` it never exceeds the hook's remaining time. |

Always set: `StrictHostKeyChecking=yes` (**host keys must already be known**: connect once by hand, `ssh me@host true`, or pass `known_hosts`), `ConnectTimeout=15`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`, and `BatchMode=yes` without a password (nothing can prompt), or `NumberOfPasswordPrompts=1` with one (a wrong password fails once, instead of three times that could trip a lockout such as fail2ban).

## Passwords

Never put a password in config.toml, a hook or a command line. Put it in an environment variable that the keepwatch service sees, and name the variable in `password_env`:

- **Windows:** `setx RELAY_PASSWORD "…"` (for your user), then restart the keepwatch logon task (`keepwatch stop`, then start it from Task Scheduler, or log off and on): processes only see variables that existed when they started.
- **Linux:** `Environment=` in a systemd drop-in (`systemctl --user edit keepwatch`), or the global `[environment]` table of keepwatch's config if that file is private to you.

keepwatch hands it to scp through `SSH_ASKPASS` (with `SSH_ASKPASS_REQUIRE=force`): a small launcher in a private temporary directory runs `python -m keepwatch.askpass VAR`, which prints the variable's value. The password is never written to a file, passed as an argument or logged; only the variable's name is. The helper refuses to answer host-key questions ("yes/no"). Password and keyboard-interactive logins both work.

## Errors

- scp failing (wrong password: "Permission denied"; unknown host key: "Host key verification failed"; missing file; network) raises `keepwatch.CommandFailed` with scp's last stderr lines, and `keepwatch kit` exits 1 with them on stderr.
- Anything keepwatch refuses or a failed check (sha256, size, conflict, unset password variable, a check trying to transfer, no time left) raises `keepwatch.TransferFailed` (exit 1 for `keepwatch kit`).
- An action that raises fails the poll: the watch backs off and retries, and observer events stay queued until a poll succeeds.
