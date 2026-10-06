# The environment hooks run in

## Processes

- Every hook call is a fresh process in its own session and process group: Python hooks as `python -m keepwatch.worker` (through `uv run` when the watch has `python_dependencies`), commands directly.
- The working directory is the watch directory, and stdin is `/dev/null`. Programs that prompt for input fail or hang until the deadline. Use non-interactive options (`ssh -o BatchMode=yes`, `scp -o BatchMode=yes`, `sudo -n`) or drive real prompts with Expect.
- **Deadlines.** At `check_timeout` or `action_timeout` (default 60s each) the whole process group gets SIGTERM, then SIGKILL 5 seconds later, so programs the hook started (an `scp`, say) die with it. `ctx.run` limits each command to the time the hook has left.
- When the service stops (SIGTERM or Ctrl-C), running hooks get SIGTERM and the service exits after their polls end.
- A manual `keepwatch poll` and the service never poll the same watch at the same time: the second one waits for the first.

## Environment variables

A hook's environment is built in this order, later entries winning:

1. the environment of the keepwatch process (the service, or your shell for `keepwatch poll`), minus any inherited `KEEPWATCH_*` variables;
2. the global config's `[environment]`;
3. the watch's `[environment]`;
4. keepwatch's own variables (see `keepwatch docs executables`).

## Under systemd (the login service)

`keepwatch install` runs the service as a systemd user service. Its environment is not your login shell's:

- **PATH** is the PATH of the shell that ran `keepwatch install`, written into the unit. If you install new programs elsewhere, re-run `keepwatch install` or set `PATH` in `[environment]`.
- **ssh-agent.** The service does not see the agent of your terminal session (`SSH_AUTH_SOCK` is not set there). For `ssh`/`scp` in hooks, either use a dedicated key without a passphrase (`ssh -i ~/.ssh/keepwatch_ed25519 -o BatchMode=yes …`, or an `IdentityFile` entry in `~/.ssh/config`), or run an agent with a fixed socket path (for example a systemd user `ssh-agent` service) and set `SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"` in the global `[environment]`.
- **Desktop notifications** (`notify-send`) usually work, because the systemd user manager has the session bus address.
- The service's own output goes to the journal (`journalctl --user -u keepwatch`); the keepwatch log has everything.

## On Windows

- Every hook runs in its own **Job Object**, started suspended and assigned to the job before it runs, so nothing it starts can escape. A timeout, `keepwatch stop`, or the service ending terminates the whole job **at once**: Windows has no graceful stop signal for windowless processes, so hooks must not rely on cleanup when they are terminated. Hooks also die if the service process itself is killed.
- String hooks run in Windows PowerShell (see `keepwatch docs executables`).
- **Before blaming a hook's output for `The system cannot find the path specified.`**: cmd.exe reads `HKCU\Software\Microsoft\Command Processor\AutoRun` (and the machine-wide equivalent) before every `/c` it runs, and Clink installs itself there; a stale entry makes every cmd.exe the system starts print that line to stderr, which lands in a hook's captured output and in scp's stderr. keepwatch's own script spawns are immune — a `.cmd` or `.bat` hook (or `ctx.run` of one) is run as `cmd.exe /d /c`, which skips AutoRun, and a string hook is PowerShell. The one place it can still appear is the askpass launcher: **ssh** starts that `askpass.cmd` itself, and Windows runs it through cmd.exe without `/d`. That line is noise, not an askpass failure.
- `keepwatch install` registers a Task Scheduler task "keepwatch" that runs `pythonw.exe -m keepwatch run` at logon (no console window, restarted on failure); if policy forbids user tasks, it creates a Startup-folder shortcut instead. The service runs with your normal user environment, so there is no PATH capture.
- Stop the service with **`keepwatch stop`** (it also works on Linux). Without a console, the service writes only to its log file.
- ssh and scp: the Windows OpenSSH `ssh-agent` service is shared by all your sessions, so keys added with `ssh-add` work for hooks too. For a password, see the askpass support planned with the transfer tools.
