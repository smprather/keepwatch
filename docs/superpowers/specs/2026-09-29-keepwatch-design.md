# keepwatch — design

Date: 2026-09-29
Status: draft for review

## 1. Purpose

keepwatch is a command-line tool that starts at login and runs forever in the
background. It runs a set of **watches**. Each watch periodically runs a
**check** that answers a yes/no question about the world, remembers the
answer, and runs **actions** when the answer is, or becomes, true or false.

Goals, in the user's words and as agreed in design discussion:

- No GUI. Everything happens in the terminal.
- Starts automatically at login and is always running.
- Each watch has its own polling interval, check, rules for what counts as true
  and false, and actions for "while true", "while false", false→true and
  true→false.
- Watches are plugins: a directory each, holding arbitrarily large and varied
  code (Python, shell, Expect, anything executable).
- Complete after-the-fact visibility: when an action such as `scp` fails at 3am,
  the log must say exactly what ran, what it printed and why it failed.
- Live output in the terminal during development.
- Plugins are written mostly by AI agents, so the tool's online help must leave
  out no detail and be tuned for agents to read.
- Linux first. Nothing in the design should rule out macOS or Windows later.

Success means: the first real watch (section 14) runs unattended from login,
sends every finished `psg-export-*.tar.gz`, never resends one it has recorded
as sent, and any failure is explainable from `keepwatch logs` alone.

### Non-goals for v1

- macOS and Windows support (paths and service installation are isolated so
  they can be added).
- Sub-second polling. Intervals of 5–10s are the expected minimum; 1s is the
  enforced floor.
- Debouncing ("N identical answers before the condition flips").
- A control socket. The CLI talks to the running service through files only.
- Notifications beyond the single `alert_command` hook.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| watch | One directory under a watches directory: `config.toml` plus code. |
| poll | One run of a watch: the check, then whichever actions the rules call for. |
| check | The hook that looks at the world and produces an outcome. It never changes anything. |
| outcome | What one check produced: `true`, `false`, `unknown`, `timeout` or `error`. |
| condition | What the watch currently believes: TRUE or FALSE. Changed only by `true`/`false` outcomes. |
| action | Any hook other than the check: `on_rise`, `on_fall`, `on_true`, `on_false`. |
| payload | JSON data the check returns with its answer; handed to the actions and logged. |
| service | The long-running `keepwatch run` process started at login. |
| master tick | The service's periodic pass (default every 5s) that reloads config and reads control files. |
| offline | A watch stopped by the failure limit or by `keepwatch disable`. |
| ledger | A persistent, named set of string keys a plugin uses to remember what it has done. |

## 3. Architecture

```
keepwatch run (service, one process)
├── master tick thread: reload config, rescan watch dirs, read control files, write status
├── one scheduler thread per watch: poll → state machine → actions → sleep
│     └── per hook call, a fresh child process:
│           python hooks  → python -m keepwatch.worker  (imports watch.py, calls one function)
│           executable hooks → the command itself
└── log writer thread: the only writer of the JSONL log
```

Decisions and the reasons for them:

- **Plugins run in a fresh child process per hook call (approach C).** Timeouts
  are real: killing the child's process group also kills anything it started,
  such as `scp`. A crashing, hanging or leaking plugin cannot affect other
  watches. Code edits take effect at the next call with no reload mechanism.
  The cost (tens of ms per call) is irrelevant at 5s+ intervals.
- **The check and each action are separate calls, not one "kernel" call.** The
  service must see the check's answer on its own to detect state changes, feed
  fake outcomes during testing, tell check failures from action failures, give
  each phase its own timeout, and log every poll the same way. A watch whose
  check always returns true and whose `on_true` does everything is the
  single-call model, so nothing is lost.
- **The service is the only decision maker.** A worker runs exactly one
  function and reports exactly one result. There is no back-and-forth protocol.
- **Threads, not asyncio.** Each watch's scheduler blocks on its child process;
  a thread per watch is the simplest correct model for tens of watches.

## 4. File layout

All paths follow the XDG base directory spec. `<w>` is a watch name (its
directory name).

```
$XDG_CONFIG_HOME/keepwatch/config.toml             global config (default ~/.config/keepwatch/config.toml)
$XDG_CONFIG_HOME/keepwatch/watches/                default watches directory
$XDG_CONFIG_HOME/keepwatch/watches/AGENTS.md       written by `keepwatch init`; points agents at `keepwatch docs agent`
$XDG_CONFIG_HOME/keepwatch/watches/CLAUDE.md       written by `keepwatch init`; contains `@AGENTS.md`
<watches dir>/<w>/config.toml                      the watch's config (required)
<watches dir>/<w>/watch.py                         Python hooks (optional)
<watches dir>/<w>/...                              anything else the watch needs

$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl     the log (rotated: keepwatch.jsonl.1, .2, ...)
$XDG_STATE_HOME/keepwatch/status.json              written by the service every master tick and after every poll
$XDG_STATE_HOME/keepwatch/watches/<w>/offline.json present while the watch is offline (reason, time, last error)
$XDG_STATE_HOME/keepwatch/watches/<w>/data/        the plugin's persistent storage (ctx.data_dir)
$XDG_STATE_HOME/keepwatch/watches/<w>/data/ledgers/<name>.json   ledgers

$XDG_RUNTIME_DIR/keepwatch/service.lock            single-instance lock (flock)
$XDG_RUNTIME_DIR/keepwatch/locks/<w>.lock          per-watch poll lock (flock)
$XDG_RUNTIME_DIR/keepwatch/<pid>/<w>/              run-only scratch for one process (ctx.run_dir)
```

- `XDG_STATE_HOME` defaults to `~/.local/state`. XDG places logs and
  restart-surviving state there.
- `XDG_RUNTIME_DIR` (normally `/run/user/<uid>`) is used for run-only data
  instead of `$TMPDIR`: it is private (mode 0700), in RAM, and deleted at
  logout, so even a crash leaves nothing behind. If it is unset, the fallback
  is `$TMPDIR/keepwatch-<uid>/` (or `/tmp/keepwatch-<uid>/`), created mode
  0700; keepwatch refuses to use it if it exists and is not owned by the user
  with mode 0700.
- Each process (the service, or a manual `keepwatch poll`) deletes its own
  `<pid>` directory on exit, and on start removes `<pid>` directories whose
  process no longer exists.
- Tool-owned files sit next to `data/`, never inside it, so a plugin cleaning
  its own storage cannot damage them.
- Persistent data is keyed by watch name. Renaming a watch directory therefore
  starts it with empty data (the scp watch would resend everything).
  `keepwatch rename OLD NEW` moves the directory and its state together, and
  `keepwatch status` lists state directories that have no matching watch.

## 5. Configuration

### 5.1 Formats

- TOML, read with the standard library's `tomllib`.
- **Durations** are either a number (seconds) or a string: an integer followed
  by `s`, `m`, `h` or `d` (`"30s"`, `"15m"`, `"1h"`, `"90d"`). Compound forms
  (`"1h30m"`) are accepted.
- `~` and `$VARS` are expanded in path-valued keys (`watch_dirs`) but not in
  `[settings]`, which is passed to plugins verbatim.
- Unknown keys are errors everywhere except inside `[settings]` and
  `[environment]`, so a typo cannot silently do nothing. Errors name the file,
  line, key and the nearest valid key.

### 5.2 Global config: `$XDG_CONFIG_HOME/keepwatch/config.toml`

Optional; every key has a default. `keepwatch init` writes a commented copy.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `watch_dirs` | list of paths | `["$XDG_CONFIG_HOME/keepwatch/watches"]` | Directories whose subdirectories are watches. Earlier entries win name clashes. |
| `reload_interval` | duration | `"5s"` | Master tick period. |
| `alert_command` | string or list | none | Run when a watch goes offline or back online (section 8.4). |
| `[defaults]` | table | — | Default values for any watch key in section 5.3 marked "defaultable". |
| `[environment]` | table of strings | `{}` | Extra environment variables for every hook, e.g. `SSH_AUTH_SOCK`. |
| `[log]` `max_bytes` | int | `10_000_000` | Rotate the log at this size. |
| `[log]` `backups` | int | `10` | Rotated files kept. |
| `[log]` `capture_bytes` | int | `65_536` | Per stream (stdout, stderr) captured into a log record; longer output keeps the head and the tail and is marked truncated. |

### 5.3 Watch config: `<watches dir>/<w>/config.toml`

| Key | Type | Default | Defaultable | Meaning |
|---|---|---|---|---|
| `description` | string | `""` | no | One line shown in `status` and logs. |
| `enabled` | bool | `true` | no | `false` parks the watch: loaded and validated, never polled. |
| `interval` | duration | `"60s"` | yes | Wait between the end of one poll and the start of the next. Minimum 1s. |
| `initial_condition` | bool | `false` | yes | The condition before the first answer after the service starts. |
| `check_timeout` | duration | `"60s"` | yes | Deadline for the check. |
| `action_timeout` | duration | `"60s"` | yes | Deadline for each action. |
| `max_failures` | int | `5` | yes | Consecutive failed polls before the watch goes offline. `0` disables the limit. |
| `retry_after` | duration | none | yes | While offline, make one trial poll this often (section 8.3). |
| `python_dependencies` | list of strings | `[]` | no | PEP 508 requirements for `watch.py` (section 10). |
| `[hooks]` | table | `{}` | no | Hooks implemented as commands (section 9.3). |
| `[check_exit_codes]` | table | `true=[0]`, `false=[1]`, `unknown=[]` | no | Exit-code mapping for a command check. Unlisted codes are errors. |
| `[environment]` | table of strings | `{}` | no | Extra environment variables for this watch's hooks (added over the global ones). |
| `[settings]` | table | `{}` | no | Free-form plugin parameters: `ctx.settings` in Python, `KEEPWATCH_SETTING_*` for commands. |

A watch is valid when its config parses, it has a check (a `check` function in
`watch.py` or `hooks.check`), every hook is defined at most once, and every
command hook's executable resolves.

## 6. Check outcomes

An outcome gets its own name only if the service or a person reacts to it
differently. All other distinctions are detail on the log record (exit code,
signal, exception type and traceback, captured output, duration).

| Outcome | Meaning | Python check | Command check |
|---|---|---|---|
| `true` | An answer. | `return True` or `return True, payload` | Exit code in `check_exit_codes.true` (default `[0]`) |
| `false` | An answer. | `return False` or `return False, payload` | Exit code in `check_exit_codes.false` (default `[1]`) |
| `unknown` | Ran correctly but cannot tell right now. | `return None`, or `raise keepwatch.Unknown("reason")` | Exit code in `check_exit_codes.unknown` (default none) |
| `timeout` | Killed at `check_timeout`. | — | — |
| `error` | The check broke. | Any other return value or exception; import failure | Any unlisted exit code; killed by a signal; could not start |

No exit code maps to `unknown` by default, because many tools use low codes
for real errors (curl exits 3 for a malformed URL). A watch that wants network
failures treated as "can't tell" lists them, e.g. `unknown = [6, 7, 28]` for
curl.

## 7. State machine

Per watch, the service keeps in memory: `condition` (TRUE/FALSE), `pending_edge`
(none, rise or fall), and `failures` (consecutive failed polls).

### 7.1 One poll

| Check outcome | Condition was FALSE | Condition was TRUE |
|---|---|---|
| `true` | becomes TRUE, `pending_edge = rise`; run `on_rise`, then `on_true` | run `on_true` |
| `false` | run `on_false` | becomes FALSE, `pending_edge = fall`; run `on_fall`, then `on_false` |
| `unknown` | nothing runs; condition unchanged | same |
| `timeout` / `error` | nothing runs; condition unchanged; the poll fails | same |

Rules:

1. **Start.** When the service starts (or a watch is added while it runs) the
   condition is `initial_condition` (default FALSE). So at login a condition that
   is already true fires `on_rise`, and one that is false fires nothing. Checks
   should be written so that TRUE is the thing to act on.
2. **Edges are retried until they succeed.** A pending edge is cleared only when
   its action succeeds. If `on_rise` fails, it runs again at the next poll that
   answers `true`. If the condition flips first, the pending edge is replaced
   by the opposite one. Consequence: actions run at least once and must be
   safe to repeat.
3. **Actions in one poll run in order and stop at the first failure.** If
   `on_rise` fails, `on_true` does not run in that poll.
4. **A watch's polls never overlap.** `interval` is measured from the end of the
   previous poll (plus backoff, section 8). Different watches are independent.
5. **Config reloads keep state.** A changed config applies from the next poll;
   the condition, pending edge and failure count are kept. Re-enabling an
   offline watch keeps the condition and resets the failure count.
6. Hooks that are not defined are skipped; a skipped hook neither succeeds nor
   fails. A pending edge with no hook is cleared immediately.

The state machine is a pure function, `step(state, outcome) -> (state, plan)`,
with a second function recording action results. It has no I/O, which makes it
exhaustively unit-testable and lets `poll --fake` drive it directly.

## 8. Failures, backoff and offline

### 8.1 What fails a poll

- An action fails: nonzero exit, exception, or timeout.
- The check times out or errors.
- `unknown` neither fails nor succeeds a poll, and does not reset the count.
- A poll in which the check answered and every action that ran succeeded resets
  `failures` to 0.

Check failures count on purpose: a broken check never runs an action, so an
action-only limit would let a broken watch do nothing forever without anyone
being told.

### 8.2 Backoff

After the k-th consecutive failure, the wait before the next poll is:

```
wait = min(max(interval, 60s) * 2^(k-1), max(interval, 1h))
```

With defaults (`max_failures = 5`) the waits are 1m, 2m, 4m, 8m, so a watch goes
offline after about 15 minutes of continuous failure whatever its interval. This
also keeps a failing `scp` from producing a burst of failed ssh logins, which
tools like fail2ban punish with an IP ban.

### 8.3 Offline

When `failures` reaches `max_failures`:

- The watch stops polling.
- `offline.json` is written to its state directory with the reason, time and the
  last failure's summary. Because it is on disk, restarting the service does not
  quietly resume the watch.
- A `watch.offline` record is logged at CRITICAL.
- `alert_command` runs (section 8.4).

A watch comes back online when:

- `keepwatch enable <w>` deletes `offline.json`; the next master tick sees it.
- Or, if `retry_after` is set, the service makes one trial poll every
  `retry_after`. A trial poll that succeeds (the check answers and every action
  that runs succeeds, per 8.1) deletes `offline.json`, resets `failures`, logs
  `watch.online` and runs `alert_command`. Any other trial poll, including one
  answering `unknown`, leaves the watch offline until the next trial.

`keepwatch disable <w>` writes `offline.json` with reason "disabled by user";
such a watch has no trial polls.

### 8.4 alert_command

Run (with `action_timeout` from `[defaults]`) when a watch goes offline or comes
back online. It receives `KEEPWATCH_ALERT_EVENT` (`offline` or `online`),
`KEEPWATCH_WATCH`, and `KEEPWATCH_ALERT_REASON`. A string runs through
`/bin/sh -c`; a list runs directly. Example:
`alert_command = 'notify-send "keepwatch: $KEEPWATCH_WATCH $KEEPWATCH_ALERT_EVENT" "$KEEPWATCH_ALERT_REASON"'`.
Its failures are logged but never counted against any watch.

## 9. Plugin API

### 9.1 Python hooks (`watch.py`)

A watch's `watch.py` may define any of these module-level functions:

```python
def check(ctx: keepwatch.Ctx) -> bool | None | tuple[bool | None, object]: ...
def on_rise(ctx: keepwatch.Ctx) -> None: ...
def on_fall(ctx: keepwatch.Ctx) -> None: ...
def on_true(ctx: keepwatch.Ctx) -> None: ...
def on_false(ctx: keepwatch.Ctx) -> None: ...
```

- `check` returns per section 6. A payload must be JSON-serializable and at most
  1 MiB once encoded; otherwise the outcome is `error`.
- A check MUST only observe. It must not change anything outside `ctx.run_dir`,
  because `poll --dry-run` runs real checks.
- An action succeeds by returning and fails by raising. Its return value is
  ignored.
- `watch.py` may import sibling modules in the watch directory; the watch
  directory is first on `sys.path`.
- `print()` and anything written to stdout/stderr (including by child processes
  not run through `ctx.run`) is captured into the hook's log record.

### 9.2 `Ctx`

`keepwatch.Ctx` is fully type-hinted (the package ships `py.typed`) and
documented member by member; its docstrings are the source of `keepwatch docs ctx`.

| Member | Type | Meaning |
|---|---|---|
| `watch` | `str` | Watch name. |
| `hook` | `str` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `poll_id` | `str` | This poll's ID; appears on every log record of the poll. |
| `condition` | `bool` | The condition as this hook sees it: before the answer in `check`, after it in actions. |
| `payload` | JSON value or `None` | What `check` returned with its answer. Actions only. |
| `settings` | `dict` | The `[settings]` table, verbatim. |
| `log` | `logging.Logger` | Records are sent to the service and written to the log tagged with watch, hook and poll ID. `extra={...}` keys become JSON fields. |
| `watch_dir` | `Path` | The watch's directory. Also the working directory of every hook. |
| `data_dir` | `Path` | Persistent storage; created on first access. |
| `run_dir` | `Path` | Run-only scratch; created on first access. |
| `run(argv, *, timeout=None, check=True, input=None, env=None, cwd=None)` | `CompletedProcess` | Runs a command with stdin `/dev/null` (unless `input` is given), captures stdout/stderr, and logs the command, exit code, duration and output as a `command` record. `argv` is a list (run directly) or a string (run via `/bin/sh -c`). `timeout` defaults to the hook's remaining time. Raises `keepwatch.CommandFailed` (with exit code and stderr tail) on nonzero exit when `check=True`. |
| `ledger(name, expire=None)` | `Ledger` | A persistent set of string keys, each stamped with the time it was added. |
| `file_key(path)` | `str` | `"<absolute path>\|<size>\|<mtime_ns>"`, for ledger keys: a new file reusing a name gets a new key. |
| `glob(pattern)` | `list[Path]` | Expands `~`, returns sorted matches. |
| `unchanged_for(path, duration)` | `bool` | True when the file's mtime is at least `duration` old, i.e. nothing has written to it for that long. |

`Ledger`:

- `key in ledger`, `ledger.add(key)`, `ledger.discard(key)`, `ledger.added_at(key) -> datetime | None`, `len(ledger)`, iteration.
- Stored as JSON in `data_dir/ledgers/<name>.json`, written atomically (temp file
  + rename) on every `add`/`discard`, so a crash mid-loop keeps everything
  recorded so far.
- `expire` (a duration) forgets entries older than that, pruned on write.
  Documentation must state the trap: if the thing a key stands for can still be
  seen after its entry expires, the watch will act on it again. The scp example
  therefore uses no expiry.
- Keys must be strings. A check may read a ledger; only actions may change one
  (writes from a check raise).

Exceptions exported by `keepwatch`: `Unknown`, `CommandFailed`.

### 9.3 Command hooks

Any hook can be a command instead of a function:

```toml
[hooks]
check   = "./check.sh"                  # string: run via /bin/sh -c
on_true = ["expect", "./send.exp"]      # list: run directly
```

- Working directory: the watch directory. stdin: `/dev/null`.
- The check's outcome comes from its exit code via `[check_exit_codes]`.
  An action succeeds on exit 0 and fails otherwise.
- stdout and stderr are captured into the log record.
- Environment: the service's environment, then global `[environment]`, then the
  watch's `[environment]`, then:

| Variable | Meaning |
|---|---|
| `KEEPWATCH_WATCH`, `KEEPWATCH_HOOK`, `KEEPWATCH_POLL_ID` | As in `Ctx`. |
| `KEEPWATCH_CONDITION` | `true` or `false`, as `ctx.condition`. |
| `KEEPWATCH_WATCH_DIR`, `KEEPWATCH_DATA_DIR`, `KEEPWATCH_RUN_DIR` | As in `Ctx` (directories already created). |
| `KEEPWATCH_SETTINGS_FILE` | Path of a JSON file holding `[settings]`. |
| `KEEPWATCH_SETTING_<KEY>` | Each top-level scalar setting; key upper-cased, non-alphanumerics replaced by `_`. |
| `KEEPWATCH_PAYLOAD_OUT` | Check only: write JSON here to pass a payload to the actions. |
| `KEEPWATCH_PAYLOAD_FILE` | Actions only: path of the payload JSON (absent if there is none). |

- A hook defined both in `watch.py` and in `[hooks]` is a validation error.

### 9.4 Timeouts and process groups

Every hook runs in its own session/process group. At its deadline the whole
group gets SIGTERM, then SIGKILL 5 seconds later. The outcome is `timeout` (check)
or a failed action.

### 9.5 Worker protocol (internal)

- Python hooks run as `python -m keepwatch.worker`. The request (watch dir,
  hook, condition, payload, settings, paths, deadline) arrives on a dedicated
  pipe, not stdin; results and log records go back as JSON lines on a second
  dedicated pipe. stdin is `/dev/null`; stdout and stderr are captured as plain
  output.
- The worker's first message carries its keepwatch version and protocol version.
  A mismatch with the service fails the hook with a clear error.
- `keepwatch.worker`, `keepwatch.Ctx` and everything they import use only the
  standard library, so plugin environments never need rich-click.

## 10. Third-party Python packages (option B)

- A watch declares `python_dependencies = ["requests>=2.32"]`.
- With no dependencies, the worker runs with keepwatch's own interpreter.
- With dependencies, the worker starts through
  `uv run --no-project --with <each dependency> python -m keepwatch.worker`.
  uv caches one environment per dependency set.
- The worker must see exactly the running keepwatch's code. The service puts a
  directory containing only a symlink to its own `keepwatch` package on
  `PYTHONPATH`, under `$XDG_RUNTIME_DIR/keepwatch/<pid>/lib/`. It does not add
  its whole site-packages. The version handshake (9.5) catches any mistake.
- Polls try `uv run --offline` first and fall back to online resolution only if
  the environment is not cached, so a poll at login does not need the network
  once the environment has been built. `keepwatch validate` builds it.
- `uv` must be on `PATH` only for watches that declare dependencies;
  `validate` reports it missing.

## 11. Live configuration

Every master tick (`reload_interval`):

- The global config is re-read if its mtime or size changed.
- Each watch directory is rescanned. Subdirectories starting with `.` or `_` are
  ignored (so a watches directory can be a git repo). Each watch's `config.toml`
  is re-read if changed.
- New valid watches start. Removed watches stop after their current poll
  (in-flight hooks are not killed). Changed watches use the new config from
  their next poll.
- **A broken edit never stops a working watch.** The last valid config keeps
  running; the error is logged once per distinct bad version and shown in
  `status`. A new watch whose config is invalid is not started.
- A broken global config likewise keeps the last valid one. At service startup a
  broken global config is fatal (exit 1 with the error).
- Duplicate watch names across `watch_dirs`: the first directory wins; others
  are reported and skipped.
- `offline.json` appearing or disappearing (via `enable`/`disable`) is picked
  up.
- Code needs no reload: every hook call starts a fresh process that imports
  `watch.py` and runs scripts from disk.

## 12. Logging

### 12.1 The log file

- JSON Lines at `$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl`, written only by
  the service's log-writer thread (and by a manual `poll`'s own writer, which
  takes the same `flock` around each append). Rotated by size.
- Every record: `ts` (RFC 3339 with offset), `level`, `event`, `pid`, and when
  applicable `watch`, `poll_id`, `hook`, plus event-specific fields.
- Events:

| Event | Key fields |
|---|---|
| `service.start` / `service.stop` | version, config path, watch dirs |
| `config.loaded` / `config.error` | path, error with line |
| `watch.added` / `watch.removed` / `watch.changed` | watch |
| `poll.start` | watch, poll_id, condition, trial (bool), faked (bool) |
| `hook.end` | hook, argv or function, exit code / signal / exception + traceback, duration, stdout, stderr (captured, truncation flags), result |
| `check.outcome` | outcome, reason, payload, condition before/after, pending edge |
| `command` | from `ctx.run`: argv, exit code, duration, stdout, stderr |
| `plugin.log` | from `ctx.log`: message, logger level, extra fields |
| `poll.end` | failed (bool), failures, next poll time, backoff |
| `watch.offline` / `watch.online` | reason, last failure summary |
| `alert.end` | as `hook.end` |

Every poll's records share its `poll_id`, so `keepwatch logs --poll <id>` shows
one poll from start to finish.

### 12.2 Terminal output

- `keepwatch run` on a terminal prints one human-readable line per event, colored
  with Rich. `-v` adds captured output; `-q` shows warnings and above only.
- When stdout is not a terminal (e.g. under systemd, where it goes to the
  journal) it prints plain lines at WARNING and above, since the log file is the
  full record.

## 13. CLI

Built with rich-click. Global options: `--config PATH` (env
`KEEPWATCH_CONFIG`), `--version`.

| Group | Command | Purpose |
|---|---|---|
| Run | `run [--watch NAME ...] [-v/-q]` | Run the service in the foreground (what the login service runs). `--watch` limits it to some watches during development. Refuses to start if another service holds the lock. |
| Develop | `new NAME [--template python\|shell\|expect] [--dir DIR]` | Create a watch from a commented template in the first (or given) watches dir. |
| Develop | `validate [NAME ...] [--json]` | Check config, hooks, executables and imports (in a worker), build dependency environments. Reports every problem at once. |
| Develop | `poll NAME [--dry-run] [--fake OUTCOMES] [--payload JSON] [--initial-condition BOOL] [--json]` | Run one real poll now, printing everything live. `--dry-run`: run the check, report actions without running them. `--fake true,false,timeout,...`: skip the check, feed these outcomes one poll each, print a step trace; actions run for real unless `--dry-run`. Takes the per-watch lock; shares persistent data but not the service's condition or failure count. Exit 0 if no poll failed, 1 otherwise. |
| Inspect | `status [NAME] [--json]` | Service running or not; per watch: enabled/parked/offline (with reason), condition, last outcome and time, last action result, failures, next poll, config errors; orphaned state dirs. |
| Inspect | `logs [NAME] [--since DUR\|TIME] [--until ...] [--level L] [--event E] [--failed] [--poll ID] [-f] [-v] [--json]` | Query the log, including rotated files. `-f` follows. `--json` prints raw records. |
| Control | `enable NAME` / `disable NAME` | Delete / write `offline.json`. |
| Control | `rename OLD NEW` | Rename a watch directory and its state directory together. Refuses while the service is polling it. |
| Setup | `init` | Write the commented global config, the watches directory, `AGENTS.md` and `CLAUDE.md`. Never overwrites. |
| Setup | `install` / `uninstall` | Install/remove the systemd user unit (section 15). |
| Reference | `docs [TOPIC] [--all]` | Print the reference (section 16). No topic lists topics. |

Exit codes for every command: 0 success, 1 problems found or an operation failed,
2 usage error.

Output: on a terminal, Rich formatting (panels, tables, colors). Not on a
terminal (how agents run commands), plain text: no color, no box-drawing
characters. `NO_COLOR` is honored. `--json` is available where noted.

Error messages give file:line where there is one, the problem, the fix, and the
doc topic, e.g.
`psg-export/config.toml:3: unknown key 'intervall' (did you mean 'interval'?); see: keepwatch docs config`.

## 14. First watch: psg-export

```toml
# ~/.config/keepwatch/watches/psg-export/config.toml
description    = "Send new PSG exports to bar.com"
interval       = "30s"
action_timeout = "15m"
retry_after    = "1h"

[settings]
pattern = "~/incoming/psg-export-*.tar.gz"
dest    = "foo@bar.com:/home/foo/incoming/"
```

```python
# ~/.config/keepwatch/watches/psg-export/watch.py
from keepwatch import Ctx

def check(ctx: Ctx):
    sent = ctx.ledger("sent")
    pending = [str(f) for f in ctx.glob(ctx.settings["pattern"])
               if ctx.unchanged_for(f, "30s") and ctx.file_key(f) not in sent]
    return bool(pending), pending

def on_true(ctx: Ctx):
    sent = ctx.ledger("sent")
    for f in ctx.payload:
        ctx.run(["scp", "-o", "BatchMode=yes", f, ctx.settings["dest"]])
        sent.add(ctx.file_key(f))
```

Why it is shaped this way: the condition "unsent files exist" stays true while
sends fail and when new files arrive, so the work is in `on_true` (level), not
`on_rise` (edge). Failed sends retry on the next poll. `unchanged_for` skips
files still being written. The ledger key includes size and mtime, so a new
export reusing a name is sent. The ledger does not expire because the tarballs
stay in `~/incoming`. `BatchMode=yes` makes ssh fail instead of prompting.

This watch also ships (with a fake destination) as a tested example.

## 15. Login service (Linux)

`keepwatch install`:

- Writes `~/.config/systemd/user/keepwatch.service` with
  `ExecStart=<absolute path of keepwatch> run`, `Restart=on-failure`,
  `RestartSec=10`, and `Environment=PATH=<PATH at install time>` so hooks find
  the same commands as the shell `install` ran from.
- Runs `systemctl --user daemon-reload` and `systemctl --user enable --now keepwatch`.
- Prints what it did and how to check it (`systemctl --user status keepwatch`,
  `keepwatch status`).
- Warns when `SSH_AUTH_SOCK` is set in the current shell: the service will not
  see that agent unless its socket path is stable and set in `[environment]`,
  or the watch uses a dedicated key. `docs environment` explains both.

`uninstall` disables, stops and removes the unit.

## 16. Online help (tuned for agents)

- `keepwatch --help` opens with: "Writing or fixing a watch? Read
  `keepwatch docs agent` first, or `keepwatch docs --all` for everything."
- Every command's `--help` lists every option with type, default and an example,
  and ends with its doc topic.
- `keepwatch docs <topic>` prints Markdown: rendered with Rich on a terminal,
  raw and never re-wrapped otherwise, so tables and code blocks survive.
  Topics:

| Topic | Contents |
|---|---|
| `agent` | Start here: the whole contract in brief, the development loop, a done checklist, common mistakes. |
| `overview` | What keepwatch does, vocabulary, architecture. |
| `config` | Every key of both config files: type, default, units, example. |
| `python` | `watch.py` hooks, return values, exceptions, imports. |
| `ctx` | Every `Ctx` and `Ledger` member: signature, returns, raises, example. |
| `executables` | Command hooks: exit-code mapping, environment variables, payload files. |
| `states` | Outcomes, the state table, start, retries, ordering. |
| `failures` | Failed polls, backoff formula, offline, `enable`, `retry_after`, `alert_command`. |
| `storage` | Directory layout, persistent vs run-only data, ledgers and expiry, renaming. |
| `logging` | The log file, every event and field, `ctx.log`, querying with `logs`. |
| `environment` | What hooks run inside: working directory, stdin, environment variables, PATH and ssh-agent under systemd, what timeouts kill. |
| `dependencies` | `python_dependencies`, uv, offline behaviour. |
| `reload` | Live-config rules. |
| `cli` | Every command and option. |
| `examples` | Complete example watches: psg-export (Python), a curl check with `unknown` codes (shell), an Expect action. |

- The development loop `docs agent` prescribes: `new` → edit → `validate` →
  `poll --fake … --dry-run` → `poll --dry-run` → `poll` → `logs --poll <id>`.
- Common mistakes it lists include: returning a list instead of a bool or
  tuple; changing things in a check; commands that prompt (stdin is
  `/dev/null`); assuming the login shell's PATH or ssh-agent; ledger expiry on
  things that are still visible; renaming a watch directory with `mv`.
- Generated from code wherever possible, so it cannot drift: config tables from
  the config schema, `ctx` from docstrings and type hints, `cli` from the
  rich-click command tree. Narrative topics are Markdown files shipped in the
  package; examples are real watch directories included verbatim.
- `init` writes `AGENTS.md` (a short pointer to `keepwatch docs agent` plus the
  development loop) and `CLAUDE.md` containing `@AGENTS.md` in the watches
  directory, so an agent working there finds the docs unprompted.

## 17. Project layout and packaging

- Package `keepwatch`, Python ≥ 3.12, managed with uv, `src/` layout. Runtime
  dependency: `rich-click` (which brings `click` and `rich`). Everything else is
  standard library.
- Installed for use with `uv tool install` (editable during development).
- Modules (each with one job):

| Module | Job |
|---|---|
| `durations` | Parse and format durations. |
| `paths` | XDG paths, runtime dir fallback and permission checks. |
| `config` | Schema, parsing, validation with line numbers, defaults. Source for `docs config`. |
| `state` | The pure state machine (section 7) and backoff arithmetic (section 8.2). |
| `runner` | Start hooks (worker or command), deadlines, process groups, output capture, payload files, uv invocation. |
| `worker` | Child side: import `watch.py`, build `Ctx`, call one function, report. Stdlib only. |
| `ctx` | `Ctx`, `Ledger`, helpers, exceptions. Stdlib only. |
| `protocol` | Message types and version for the worker pipes. Stdlib only. |
| `logstore` | JSONL writer, rotation, query engine behind `logs`. |
| `service` | Scheduler threads, master tick, reload, offline markers, status file, locks, alerts. |
| `install` | systemd unit generation and systemctl calls. |
| `cli` | rich-click commands and plain/Rich/JSON output switching. |
| `docs` | Topic files, generators, `docs` command backend. |

- The repository is `github.com/smprather/keepwatch` (renamed from
  `ms-windows-do-if-loop`); the README is rewritten.

## 18. Testing

Development is test-first.

- `state`: table-driven tests covering every cell of section 7.1, every rule,
  edge retries, backoff values and offline transitions.
- `config`: valid and invalid files, line-numbered errors, suggestions,
  defaults, `[defaults]` inheritance.
- `runner` + `worker`: real child processes against temporary watch directories:
  each outcome, payload passing both ways, timeouts killing grandchildren,
  captured output and truncation, version mismatch, command hooks and their
  environment.
- `ctx`: ledger atomicity and expiry, `file_key`, `unchanged_for`, `run` logging and
  `CommandFailed`, write-from-check refusal.
- `service`: time-controlled tests of scheduling, reload (add, remove, change,
  broken edit), offline and `retry_after`, locks.
- `cli`: CliRunner tests for every command, plain vs JSON output, exit codes.
- Dependencies (section 10): one integration test with a small real package,
  marked so it can be skipped offline.
- Docs: every config key, `Ctx` member and CLI option is documented (test fails
  otherwise); every example watch passes `validate` and a `poll --fake` run.
- The service is exercised end to end at least once under `systemd-run --user`
  (manual verification step, documented in the plan).
