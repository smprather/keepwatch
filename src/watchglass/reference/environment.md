# The environment hooks run in

## Processes

- Every hook call is a fresh process in its own session and process group: Python hooks as `python -m watchglass.worker` (through `uv run` when the watch has `python_dependencies`), commands directly.
- The working directory is the watch directory, and stdin is `/dev/null`. Programs that prompt for input fail or hang until the deadline. Use non-interactive options (`ssh -o BatchMode=yes`, `scp -o BatchMode=yes`, `sudo -n`) or drive real prompts with Expect.
- **Deadlines.** At `check_timeout` or `action_timeout` (default 60s each) the whole process group gets SIGTERM, then SIGKILL 5 seconds later, so programs the hook started (an `scp`, say) die with it. `ctx.run` limits each command to the time the hook has left.
- When the service stops (SIGTERM or Ctrl-C), running hooks get SIGTERM and the service exits after their polls end.
- A manual `watchglass poll` and the service never poll the same watch at the same time: the second one waits for the first.

## Environment variables

A hook's environment is built in this order, later entries winning:

1. the environment of the watchglass process (the service, or your shell for `watchglass poll`), minus any inherited `WATCHGLASS_*` variables;
2. **on Windows**, the user and machine environment as the registry has it now;
3. the global `environment_file`, if set (read now);
4. the global config's `[environment]`;
5. the watch's `[environment]`;
6. watchglass's own variables (see `watchglass docs executables`).

### Rotating a password or other variable without a restart

A process cannot see another process change its environment: `setx` and systemd's `Environment=` do not reach
the running service by themselves. watchglass therefore re-reads a hot source before **every hook**:

- **Windows:** `HKCU\Environment` and the machine environment. `setx RELAY_PASSWORD "…"` is visible to the next
  hook, with no new terminal, `watchglass stop` or logon. `setx /M` writes the machine environment (user values
  win). PATH is combined with the service's own PATH, so entries added by the service environment survive;
  deleting a variable (or a PATH entry) from the registry leaves the old process value until a restart, so set a
  new value instead of deleting it.
- **Linux:** put `environment_file = "~/.config/watchglass/env"` in the global config and write
  `RELAY_PASSWORD=…` in that file. It is re-read before every hook. Keep `password_env = "RELAY_PASSWORD"`; the
  variable name does not change. Run `chmod 600` on the file: watchglass refuses a group- or world-readable one.
  Do not rely on a systemd drop-in's `Environment=` or `systemctl --user set-environment` for a hot value: the
  running unit's environment is not re-read, and a login shell would run rc files in the service.

`environment_file` also works on Windows (read after the registry); protect it with the file's ACL instead of a
POSIX mode. The format is one `KEY=VALUE` per line; `export` is optional, full-line `#` comments are ignored,
an unquoted value may carry an inline comment after whitespace, and single or double quotes protect spaces and
`#`. Values are literal: no variable expansion. Write the file atomically (temporary file, then rename) if a
hook may read it while you edit.

A missing, unreadable, malformed or insecure file fails the hook with a clear reason rather than silently using
a stale password. A change applies to the next hook; a transfer already running keeps the password it started
with.

## Under systemd (the login service)

`watchglass install` runs the service as a systemd user service. Its environment is not your login shell's:

- **PATH** is the PATH of the shell that ran `watchglass install`, written into the unit. If you install new programs elsewhere, re-run `watchglass install` or set `PATH` in `[environment]`.
- **ssh-agent.** The service does not see the agent of your terminal session (`SSH_AUTH_SOCK` is not set there). For `ssh`/`scp` in hooks, either use a dedicated key without a passphrase (`ssh -i ~/.ssh/watchglass_ed25519 -o BatchMode=yes …`, or an `IdentityFile` entry in `~/.ssh/config`), or run an agent with a fixed socket path (for example a systemd user `ssh-agent` service) and set `SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"` in the global `[environment]`.
- **Desktop notifications** (`notify-send`) usually work, because the systemd user manager has the session bus address.
- The service's own output goes to the journal (`journalctl --user -u watchglass`); the watchglass log has everything.

## On Windows

- Every hook runs in its own **Job Object**, started suspended and assigned to the job before it runs, so nothing it starts can escape. A timeout, `watchglass stop`, or the service ending terminates the whole job **at once**: Windows has no graceful stop signal for windowless processes, so hooks must not rely on cleanup when they are terminated. Hooks also die if the service process itself is killed.
- String hooks run in Windows PowerShell (see `watchglass docs executables`).
- **Before blaming a hook's output for `The system cannot find the path specified.`**: cmd.exe reads `HKCU\Software\Microsoft\Command Processor\AutoRun` (and the machine-wide equivalent) before every `/c` it runs, and Clink installs itself there; a stale entry makes every cmd.exe the system starts print that line to stderr, which lands in a hook's captured output and in scp's stderr. watchglass's own script spawns are immune — a `.cmd` or `.bat` hook (or `ctx.run` of one) is run as `cmd.exe /d /c`, which skips AutoRun, and a string hook is PowerShell. The one place it can still appear is the askpass launcher: **ssh** starts that `askpass.cmd` itself, and Windows runs it through cmd.exe without `/d`. That line is noise, not an askpass failure.
- `watchglass install` registers a Task Scheduler task "watchglass" that runs `pythonw.exe -m watchglass run` at logon (no console window, restarted on failure); if policy forbids user tasks, it creates a Startup-folder shortcut instead. The service runs with your normal user environment, so there is no PATH capture.
- Stop the service with **`watchglass stop`** (it also works on Linux). Without a console, the service writes only to its log file.
- ssh and scp: the Windows OpenSSH `ssh-agent` service is shared by all your sessions, so keys added with `ssh-add` work for hooks too. For a password, see *Passwords* in `watchglass docs transfers` (SSH_ASKPASS, and the `conpty` mode for servers that refuse it).
