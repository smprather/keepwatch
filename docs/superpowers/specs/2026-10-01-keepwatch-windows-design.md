# keepwatch on Windows — design

Date: 2026-10-01
Status: approved by delegation (the owner asked Claude to decide; decisions and reasons are recorded here)
Release target: 2026.10.2

## 1. Purpose and scope

keepwatch 2026.10.1 runs on Linux only. Its owner's first real use case is on Windows, so the first
step after the initial release is making every existing feature work on Windows 10/11 with Python
≥ 3.12, without changing behaviour on Linux.

In scope: everything in the 2026.10.1 spec (`2026-09-29-keepwatch-design.md`) — the CLI, watches with
Python and command hooks, the service, status/logs/control commands, `install`, docs — plus:

- a `keepwatch stop` command (needed on Windows, useful everywhere);
- a PowerShell watch template and PowerShell-aware command hooks;
- continuous integration on Linux and Windows.

Out of scope here (later phases, see section 10): the kit (`files`, `transfer`, `sweep`), the SQLite
store, `watchdog` observers, macOS-specific work.

## 2. Findings that shaped the design

- **Spike (2026-10-01, GitHub Actions `windows-latest`, OpenSSH_for_Windows_9.5p2):** the built-in
  `C:\Windows\System32\OpenSSH\scp.exe` accepts a password through `SSH_ASKPASS` +
  `SSH_ASKPASS_REQUIRE=force` with a `.cmd` helper, in both classic (`-O`) and SFTP modes; a wrong
  password fails within 0.2 s instead of hanging. Git for Windows' `scp` misparses `C:\` source paths
  in classic mode. (This matters for phase 2; recorded here so the spike is not repeated.)
- `os.kill(pid, 0)` on Windows **terminates** the process (Python maps any signal other than the
  console control events to `TerminateProcess`). `pid_alive` must not use it there.
- `subprocess` cannot pass extra file descriptors (`pass_fds`) on Windows; it can pass specific
  inheritable handles through `STARTUPINFO.lpAttributeList["handle_list"]`.
- Windows has no process groups or signals for windowless processes. Job Objects are the standard way
  to own and kill a process tree (used by e.g. Chromium, Windows' own task management).
- Files open in another process cannot be renamed or replaced on Windows (sharing violations), which
  affects log rotation and atomic replacement.
- Under `pythonw.exe` (no console window) `sys.stdout` and `sys.stderr` are `None`.

## 3. Platform layer

All OS differences live in one module, `keepwatch/platform.py`, so the rest of the code stays
OS-neutral. It exposes:

| Name | Linux | Windows |
|---|---|---|
| `IS_WINDOWS` | `False` | `True` |
| `default_dirs(env)` → (config, state, runtime) | XDG as today | `%APPDATA%\keepwatch`, `%LOCALAPPDATA%\keepwatch`, `%LOCALAPPDATA%\keepwatch\run` |
| `secure_dir(path)` | create 0700, check owner and mode (as today) | create; no mode/owner check (the user profile's ACL is already private) |
| `pid_alive(pid)` | `os.kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess == STILL_ACTIVE` (ctypes) |
| `FileLock` (used by `hold_lock`) | `fcntl.flock` | `msvcrt.locking(LK_NBLCK)` on byte 0, polled every 100 ms when blocking |
| `ProcessTree` | new session; `killpg` SIGTERM/SIGKILL | Job Object with `KILL_ON_JOB_CLOSE`; the process is created suspended, assigned, then resumed (`NtResumeProcess`), so nothing escapes the job |
| `inheritable_pipe()` + child argument | `pass_fds` | `handle_list` in `STARTUPINFO`; the worker reopens handles with `msvcrt.open_osfhandle` |
| `default_shell()` for string hooks | `["/bin/sh", "-c"]` | `["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]` |
| `script_runner(path)` for list hooks | none (exec bit and shebang) | `.ps1` → PowerShell `-File`; `.py` → keepwatch's Python; `.cmd`/`.bat` → `cmd.exe /c`; `.exe` and others → direct |

Environment variables `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` still override the
defaults on Windows (useful for tests and for people who want one layout everywhere).

### Termination on Windows

There is no graceful "please stop" for windowless processes, so on Windows a timeout or
`keepwatch stop` terminates the hook's whole Job Object immediately (no 5-second grace). Hooks must
therefore not rely on cleanup at termination; this is documented in `docs environment`. On Linux
nothing changes (SIGTERM, then SIGKILL 5 s later). Because every job has `KILL_ON_JOB_CLOSE`, hooks
also die if the service process itself is killed (Task Manager, logoff) — no orphans.

## 4. Command hooks

- **String hooks** run through `platform.default_shell()`. A new optional key `shell` (watch config
  and `[defaults]`) overrides it, e.g. `shell = ["pwsh", "-NoProfile", "-Command"]` or
  `shell = ["cmd.exe", "/d", "/c"]`. PowerShell is the Windows default because it is always present,
  handles quoting sanely, and `exit N` sets the exit code.
- **List hooks** resolve a relative program (one containing `/` or `\`) against the watch directory
  before starting it, on every OS (Windows does not resolve relative programs against `cwd`). On
  Windows, `platform.script_runner` picks the interpreter by extension.
- `validate` on Windows checks that the program exists and has a runnable extension (or is on PATH);
  the executable-bit check stays POSIX-only.
- Output is decoded as UTF-8 with replacement (unchanged). Python workers get `PYTHONUTF8=1`.

## 5. The worker protocol

Unchanged messages; only the transport differs. `runner` asks the platform layer for an inheritable
pipe pair and the argument that names it to the child: `--request-fd/--result-fd` on POSIX,
`--request-handle/--result-handle` on Windows. The worker accepts both forms. The same mechanism works
through `uv run` for `python_dependencies` watches (uv inherits inheritable handles); this is verified
by the network test on Windows CI.

## 6. Service and CLI

- **`keepwatch stop`** (new, all OSes): writes `stop.request` in the runtime directory; the service
  checks for it every second, removes it and shuts down cleanly (same path as SIGTERM). Exit 0 if a
  running service acknowledged within 30 s (the service lock is released), 1 otherwise. On Linux,
  SIGTERM and Ctrl-C keep working; on Windows Ctrl-C works in a console.
- **No console:** when `sys.stdout` is `None` (pythonw), `run` writes only to the log file.
- **`install` on Windows:** registers a Task Scheduler task "keepwatch" for the current user, trigger
  "at logon", action `pythonw.exe -m keepwatch run` (the `pythonw.exe` next to the running
  interpreter; no console window), settings: restart on failure every minute up to 3 times, no
  execution time limit, start when available. Done through `powershell.exe Register-ScheduledTask`.
  If registration is refused (some policies forbid user tasks), it falls back to a shortcut in the
  user's Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\keepwatch.lnk`,
  created through `WScript.Shell`), and says which method it used. `--dry-run` prints the PowerShell
  it would run. A `--config` given to `install` is passed on, as on Linux. `uninstall` removes
  whichever exists. `KEEPWATCH_POWERSHELL` replaces `powershell.exe` (for tests), like
  `KEEPWATCH_SYSTEMCTL`.
- **Log rotation:** if renaming the log fails because another process holds it open (e.g. `logs -f`
  on Windows), rotation is skipped and retried on a later write; records are never lost.
- **Atomic replace** (`write_json_atomic`, ledgers): `os.replace` is retried for up to 1 s on
  `PermissionError` (a reader holding the file briefly).

## 7. Templates, examples, docs

- `keepwatch new --template powershell`: `config.toml`, `check.ps1`, `on_true.ps1` (list hooks, so
  the script-runner rule applies). On Windows the default template stays `python`; `shell` and
  `expect` templates print a warning that they need a POSIX shell / Expect.
- Examples: the existing three stay (marked POSIX in their descriptions); add
  `examples/disk-space-ps` — a PowerShell check of free space with an `on_rise` notification — so
  Windows users have a runnable example. (The scp sweep example arrives with the kit in phase 2.)
- Docs: `environment` (Windows processes, Job Objects, no grace period, PowerShell default, Task
  Scheduler), `executables` (shell selection, script runners, relative programs), `storage` (Windows
  directory table), `cli` (stop), `config` (`shell` key; TOML paths: use single-quoted literal
  strings or forward slashes for Windows paths, since `\` is an escape in double-quoted TOML).

## 8. Testing and CI

- **GitHub Actions** workflow `ci.yml` on push and pull request: matrix `ubuntu-latest`,
  `windows-latest` × Python 3.12 and 3.14; steps: `astral-sh/setup-uv`, `uv sync`, `uv run pytest -q`;
  ruff on Ubuntu only. The network test (uv dependencies) runs on both.
- **Test portability:** tests use OS-neutral commands built from Python (`[sys.executable, "-c", …]`
  list hooks) instead of `sh` one-liners. A small helper module `tests/portable.py` provides them
  (exit with a code, sleep, write a file, print, ignore termination). Tests of inherently POSIX
  behaviour (signals, `sh` scripts, Expect, systemd) are marked `posix_only`; Windows-only behaviour
  (Job Objects, handle passing, Task Scheduler commands) is marked `windows_only`, with the real
  `powershell.exe` replaced by `KEEPWATCH_POWERSHELL` for install tests.
- Every task's tests must pass on Linux locally; Windows results come from CI. A plan task touching
  Windows-only code is not done until CI is green on `windows-latest`.

## 9. Decisions and reasons (summary)

| Decision | Reason | Alternatives rejected |
|---|---|---|
| Job Objects, suspended start | the only reliable way to own a whole process tree on Windows | `CREATE_NEW_PROCESS_GROUP` + Ctrl-Break (needs a shared console); `taskkill /T` (races, misses re-parented children) |
| Immediate termination on Windows | no graceful signal exists for windowless processes | sending WM_CLOSE (only GUI apps) |
| PowerShell as default shell | present on every Windows, sane quoting, real exit codes | `cmd.exe` (quoting and `%` expansion pitfalls); requiring pwsh 7 (not installed by default) |
| Task Scheduler, Startup-folder fallback | restart-on-failure and no console; the fallback works without permissions | a Windows service (needs admin, runs outside the user session); VBScript launchers (deprecated) |
| `handle_list` for worker pipes | the documented Windows equivalent of `pass_fds`; no change to plugin stdout/stderr capture | protocol over stdin/stdout (plugin output would mix with protocol) |
| `keepwatch stop` via a request file | works without signals, from any process, on every OS | named pipes / sockets (a control channel the design deliberately avoided) |
| GitHub Actions for Windows testing | real Windows, free for public repos, runs on every push | Wine (unfaithful for Job Objects and handles); a local Windows VM (kept for manual checks only) |

## 10. Later phases (not designed here)

2. Kit and store: per-watch SQLite store (ledgers, tags, key–value state), `files` helpers (pending
   with settle, tag, move/copy/archive/delete), `transfer.upload` over the system `scp`/`sftp` with
   askpass passwords (helper reads the variable in Python, never through `cmd` echo), the `sweep`
   recipe, `keepwatch kit` for command hooks.
3. Observers with `watchdog` on the service tick, `wake`.
4. More kit groups (`net`, `proc`, `sys`, `state`, `when`, `notify`) and `requires`/`condition_of`.
