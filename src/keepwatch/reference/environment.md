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
