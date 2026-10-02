# keepwatch observers, transfers and the relay — design

Date: 2026-10-01
Status: approved by delegation (decisions and reasons recorded here)
Release target: 2026.10.3
Builds on: `2026-09-29-keepwatch-design.md`, `2026-10-01-keepwatch-windows-design.md`

## 1. Purpose

Make keepwatch event-driven where polling is the wrong tool, and give it the file-transfer pieces every
"move files between machines" watch rewrites. The driving case is a three-host relay with keepwatch on
the hub:

| Host | Can reach | Role |
|---|---|---|
| Windows (keepwatch) | ssh + scp to/from linux1; scp **to** linux2 only | hub |
| linux1 (EL8, Python 3.6 at `/usr/bin/python3`, no inotify-tools) | nothing outbound to the others | produces large `.tar.gz` files |
| linux2 | nothing; accepts scp uploads from Windows (password auth; often disconnected for long periods) | destination |

Flow: a file appears on linux1 → Windows learns of it through **one long-lived outbound ssh** running a
remote watcher → Windows pulls it (sha256 verified) into a local staging folder → a second watch pushes
it to linux2 when linux2 is reachable (one-way; success = scp exit 0; a `.sha256` file is uploaded after
the data as a completion marker) → the staged copy is deleted.

Everything is general: any long-running command can be an event source, any local directory can be
observed, and the transfer tools work for any scp source and destination. The relay is the first
documented configuration.

## 2. Observers

An **observer** is an event source the service keeps running independently of any watch's polling
schedule. Watches declare them in `config.toml`:

```toml
[observe.<name>]
kind = "command" | "files" | "remote_files"
wake = true          # poll this watch as soon as an event arrives (default true)
...kind-specific keys...
```

### 2.1 Kinds

- **`command`** — runs `command` (a list) as a long-lived child process in its own process group / Job
  Object. Optional `stdin` text is written once at start, then stdin is closed. Each stdout line is one
  event: a JSON object is used as-is; any other text becomes `{"line": "<text>"}`. Objects with
  `"event": "heartbeat"` are not delivered; they only prove the source is alive. stderr is captured into
  `observer.output` log records (rate-limited). If the process exits, or no line (heartbeats included)
  arrives for `heartbeat_timeout` (default: none), it is stopped and restarted with backoff 5 s → 10 s →
  … → 5 min, reset after a run that lasted 5 minutes.
- **`files`** — watches a local directory (`path`, optional `pattern`, `ignore` (default `.*`, `*.tmp`, `*.part`, `*~`), `recursive = false`) with the
  `watchdog` library (inotify / ReadDirectoryChangesW / FSEvents). On start and on every filesystem
  event it rescans; a file is reported once it is **settled** (size and mtime unchanged for `settle`,
  default 10 s, and its mtime at least `settle` old). Emits `{"event": "file", "path", "name", "size",
  "mtime"}`, once per (path, size, mtime) per service run.
- **`remote_files`** — a `command` observer preconfigured to run keepwatch's built-in remote watcher
  (section 3) over ssh: keys `remote` (`user@host`), `dir`, `pattern`, `ignore`, `settle`, `interval`,
  `checksum`, `remote_python`, `port`, `identity`, `ssh_options`, `ssh_command` (default `["ssh"]`;
  replaceable for tests). Heartbeat timeout defaults to 3 × the watcher's heartbeat.

### 2.2 Delivery to watches

- The service keeps, per watch, one queue of pending events (each tagged `"observer": "<name>"` and
  `"received": <time>`), capped at 10 000 (oldest dropped with a WARNING).
- A poll hands the watch all pending events: Python hooks get `ctx.events` (a list of dicts, oldest
  first); command hooks get `KEEPWATCH_EVENTS_FILE` (JSON array). The check normally turns events into
  its answer and payload.
- **Events are acknowledged (removed) only when the poll succeeds** (not failed, not unknown). A failed
  or unknown poll keeps them for the next poll: at-least-once delivery. Watches deduplicate (the recipes
  use ledgers).
- With `wake = true`, an event wakes the watch's scheduler at once (subject to backoff and offline state).
- Observers start with their watch and stop when it is removed, parked or offline; config changes
  restart them.
- Manual `poll` has no running observers: `ctx.events` is empty unless `--events JSON` (a JSON array)
  injects some. `keepwatch observe <watch> [<name>]` runs a watch's observers in the foreground and
  prints their events (for development; exit with Ctrl-C).
- Records: `observer.started`, `observer.event` (DEBUG), `observer.output`, `observer.stopped`
  (`exit_code`, `reason`), `observer.restarting` (`delay`).

## 3. The remote watcher

`keepwatch/remote_watcher.py` — one file, standard library only, **compatible with Python 3.6+**
("assembly style": no dataclasses, walrus, `capture_output`, `match`, or 3.7+ library functions). It is
never installed on the remote host: the observer runs

```
ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 [opts] user@host <python> -u -
```

and writes the watcher's source to ssh's stdin, with its options prepended as one Python assignment
(`KEEPWATCH_REMOTE_ARGS = "<json>"`) instead of a command-line argument, so nothing passes through the
remote login shell's quoting (EDA hosts often use csh/tcsh). keepwatch also passes `-T`,
`-o ConnectTimeout=15`, `-o ControlMaster=no -o ControlPath=none` (no connection sharing) and `--` before the host; the watcher's `hello` becomes an `observer.connected`
record, and stdout lines other than file events (login-script banners) are logged, not delivered. `remote_python = "auto"` (default) runs
`sh -c 'if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 "$@"; else exec python3 "$@"; fi' sh -u - <args>`,
preferring the system interpreter over whatever is first on PATH.

Behaviour:

1. Prints `{"event": "hello", "version", "python", "inotify": bool}`.
2. Scans `dir` (not recursive) every `interval` (default 2 s) — and immediately when inotify (used
   through `ctypes` if libc provides it) reports a change in `dir`.
3. Considers regular files matching `pattern` (fnmatch, default `*`) and not matching `ignore` (default
   `.*`, `*.tmp`, `*.part`, `*~`).
4. A file is **settled** when its (size, mtime) has been unchanged for `settle` seconds (default 10).
   Settled files not yet reported in this run are reported — including everything already present at
   startup, so a reconnect catches up.
5. With `checksum = true` (default) it computes the sha256 (streamed) before reporting.
6. Emits `{"event": "file", "path": "<absolute>", "name", "size", "mtime", "sha256"?}` and flushes.
7. Emits `{"event": "heartbeat"}` when nothing was printed for `heartbeat` seconds (default 30).
8. Exits quietly when stdout is closed.

## 4. Transfers

`keepwatch.transfer` (also `ctx.transfer`, and `keepwatch kit` for command hooks) uses the system's
OpenSSH `scp`.

- Endpoints: local paths, `user@host:path`, or `scp://user@host[:port]/path`. Two remote endpoints
  use `scp -3` (data flows through this machine) **in SFTP mode only**: in classic mode (`-3 -O`) scp
  exits 0 when the second host fails (login refused, unknown host key, host down; spike on OpenSSH
  10.5p1, 2026-10-02), so success could not be told from failure. keepwatch therefore refuses
  `protocol = "scp"` with two remote endpoints, and refuses a password for them (keys only).
- Options always set: `-o StrictHostKeyChecking=yes` (host keys must already be known: `known_hosts`
  or a `known_hosts` setting), `-o ConnectTimeout=15`, `-o ServerAliveInterval=15`,
  `-o ServerAliveCountMax=3`; `-o BatchMode=yes` unless a password is used, and then
  `-o NumberOfPasswordPrompts=1` (a wrong password fails once instead of three times, which could
  trip lockouts such as fail2ban).
- `protocol = "scp"` (default) uses the classic scp protocol, which scp-only servers accept: `-O` is
  added when the local OpenSSH is 9.0 or newer (version from `ssh -V`, cached). `protocol = "sftp"`
  requires OpenSSH ≥ 9.0.
- **Passwords:** `password_env = "VAR"` names an environment variable holding the password. keepwatch
  writes a small askpass launcher into the run directory (`sh` script on POSIX, `.cmd` on Windows) that
  runs `python -m keepwatch.askpass` with the variable's name; the Python helper prints the value (no
  shell quoting of the secret), and scp gets `SSH_ASKPASS`, `SSH_ASKPASS_REQUIRE=force` (verified on
  Windows OpenSSH 9.5 on 2026-10-01; on OpenSSH 10.5p1 on 2026-10-02 with a password containing spaces,
  quotes and `$`, and for keyboard-interactive logins too).
- **Remote paths are backslash-escaped**: every character outside `[A-Za-z0-9_./:@%+,=-]` gets a `\`
  (a leading `~/` is kept for tilde expansion). Spike against OpenSSH sshd, 2026-10-02: a space breaks an
  unquoted path in classic mode, single quotes fail in both modes ("filename does not match request" /
  "No such file"), backslash escapes work in both.
- **Remote login scripts must be silent**: anything a login shell prints on stdout for a non-interactive
  session (an `echo` in `.cshrc`/`.bashrc`) breaks scp in both modes ("Received message too long";
  spike 2026-10-02). The remote watcher tolerates it; scp cannot. The docs say how to guard the echo
  (`if ($?prompt)` in csh, `[ -t 1 ]` or `case $- in *i*)` in sh).
- Success is exit status 0, one file per transfer; failures raise `CommandFailed` with scp's stderr.
- `pull(remote, local_dir, *, size=None, sha256=None, on_conflict="skip-identical")` copies to
  `local_dir/.name.part`, verifies size and sha256 when given, then renames to the final name. If the
  final name exists with the same sha256 the pull is a no-op; otherwise an existing name is an error
  (`on_conflict = "rename"` or `"overwrite"` to opt in).
- `push(path, remote_dir, *, marker="sha256")` uploads the file, then (default) a `name.sha256` file in
  `sha256sum` format as a completion marker, so consumers on an upload-only host can wait for the marker
  and run `sha256sum -c`.
- `tcp_open(host, port=22, timeout=5) -> bool` for reachability checks.

## 5. Recipes

A watch may set `recipe = "<name>"` instead of providing hooks: keepwatch supplies built-in hooks and
observers, configured by `[settings]` (unknown settings are errors). `watch.py` and `[hooks]` are then
not allowed.

### 5.1 `pull`

Settings: `remote`, `remote_dir`, `pattern` (`*`), `ignore`, `settle` (`10s`), `checksum` (true),
`remote_python` (`auto`), `port`, `identity`, `known_hosts`, `ssh_options`, `ssh_command`, and `local_dir`
(staging). No `password_env` (the observer needs key authentication anyway) and no `dest` (direct `scp -3`
needs SFTP and keys on both hosts; a watch.py with `ctx.transfer.copy` can do it). The observer gets
`skip_ledger = "pulled"`, so reconnects do not re-hash files already pulled.

- Observer: a `remote_files` observer named `remote`, built from the settings.
- check: file events not in the ledger `pulled` (key `path|size|mtime`) → TRUE with them as payload;
  otherwise FALSE.
- on_true: for each file: `pull` (or `scp -3` to `dest`), verifying size and sha256; add to `pulled`.
  A failure stops the action; files already done stay recorded.

### 5.2 `push`

Settings: `local_dir`, `pattern` (`*`), `dest` (remote endpoint), `password_env`, `marker`
(`sha256`|`none`), `after` (`delete`|`archive`|`keep`), `archive_dir`, `keep_for` (`7d`), `settle`
(`10s`), `reachable_port` (22), `reachable_timeout` (`5s`), `reachable_host` (defaults to the host in
`dest`; set it when `dest` is a Host alias).

- Observer: a `files` observer on `local_dir` (wakes the watch when a file settles).
- check: if the destination host's `reachable_port` does not accept a TCP connection → **unknown**
  ("destination unreachable") — no failure, no backoff, files wait. Otherwise TRUE with the settled
  files in `local_dir` (rescanned directly; the observer only wakes) that are not in the ledger
  `pushed`, else FALSE.
- on_true: for each file: `push` (+ marker); then `delete`, move to `archive_dir` (pruning files older
  than `keep_for`), or add to `pushed` (`keep`).

## 6. The relay example

`examples/relay-pull` and `examples/relay-push` (placeholders `linux1.example`, `linux2.example`) and a
docs topic `relay`, covering the topology, key setup for linux1, the password variable for linux2
(`setx RELAY_PASSWORD …` on Windows), the staging folder as the queue, the `.sha256` marker, and the
`scp -3` alternative with its limits (SFTP on both hosts, keys only; classic `-3` cannot report a
failed destination).

## 7. Decisions

| Decision | Reason | Rejected |
|---|---|---|
| Observers in the service, events delivered with polls | keeps one decision maker and the existing state machine, retries and logging | hooks that block on events (a hook would hold a process forever) |
| At-least-once delivery, ack on successful poll | a failed action never loses an event; recipes deduplicate | at-most-once (silent loss) |
| Remote watcher sent over ssh stdin | nothing to install on locked-down hosts | copying a script to the remote |
| Python 3.6 floor for the remote watcher | EL8 `/usr/bin/python3`; EDA hosts lag | requiring the user's newer Python |
| Two watches chained by a staging folder | independent retries; queues while linux2 is away; inspectable | one combined relay watch |
| Reachability check → unknown | linux2 is often disconnected; that is not a failure | counting every disconnected period as failures (offline + alert storms) |
| `.sha256` uploaded after the data | upload-only destination cannot rename; consumers need a completion signal | temporary-name upload (needs rename rights) |
| System scp, askpass for passwords | present on Linux and Windows; honours `~/.ssh/config`; spike-verified | paramiko (heavier; classic scp needs exec channel anyway) |
| `watchdog` for local directories | portable native notifications | per-OS code |

## 8. Testing

- Remote watcher: tests run it as a local subprocess against temporary directories; a CI job runs its
  tests under Python 3.6 (`python:3.6-slim` container) and the suite compiles it with Python 3.6.
- `remote_files` observer: `ssh_command` points at a test helper that drops ssh options and the host and
  runs the rest locally, so the full observer path runs without an ssh server, on Linux and Windows.
- Transfers: an `asyncssh` scp server on localhost (test dependency) with password and key auth and a
  pinned host key; the system `scp` is exercised for push, pull, askpass and wrong-password cases in
  classic mode. asyncssh's SFTP server sends no exit status (scp then exits 1 after a complete copy),
  so SFTP mode and `-3` are tested at the command-line level only; the hand test uses a real sshd in a
  container.
- Observers: command observers built from Python one-liners (events, garbage lines, exit and restart,
  heartbeat timeout); files observer on temporary directories.
- Recipes and an end-to-end relay test combine the fake ssh, the asyncssh server and temporary
  directories.

## 9. Install guards (Plan 6d)

`keepwatch install` keeps registering the interpreter that runs it (`pythonw.exe` beside `sys.executable` on
Windows; the same keepwatch on Linux), never one found on PATH, through `py` or `uv python find` (those can be
the Microsoft Store placeholder or an interpreter without keepwatch). Two guards, on every OS:

- **No transient environments.** Refuse when the interpreter's prefix is inside uv's cache (`UV_CACHE_DIR`, or
  `uv cache dir` when uv is on PATH, or the default cache location) or the temp directory: `uvx` and
  `uv run --with` environments are pruned later and the logon task would break silently. The message says to
  `uv tool install keepwatch` and run `keepwatch install` from there; `--force` overrides.
- **Verify before registering.** Run `<interpreter> -m keepwatch --version` (console `python.exe` beside
  `pythonw.exe` on Windows) with the service's environment and require this keepwatch's version; otherwise fail
  with the command, its exit code and output (the Store placeholder exits 9009).

The user's Windows hub runs keepwatch from `uv tool install keepwatch`, which passes both guards.
