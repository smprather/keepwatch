# keepwatch

Poll conditions and run actions, from login onward.

keepwatch runs *watches*: small directories holding a `config.toml` plus a check and some actions, written in Python, shell, Expect or anything executable. It polls each check on its own interval, remembers whether the condition is TRUE or FALSE, runs actions while it is TRUE or FALSE and when it changes, retries with backoff, takes failing watches offline, and logs everything as JSON lines, so any failure can be explained afterwards. The command line and its reference are written for AI agents as much as for people.

Watches can also react to events instead of polling: observers keep commands, local directories or remote directories (over one ssh connection, with nothing installed remotely) under watch. Built-in recipes move files between machines with verified scp transfers; `keepwatch docs relay` walks through relaying files host A → this machine → host B.

`keepwatch tui` watches all of it live in one screen: every watch and its state, the countdown to its next poll, and the actions it has taken with their results. It is read-only, so it is safe to leave open beside the running service.

## Install

```sh
uv tool install git+https://github.com/smprather/keepwatch
# Windows only, for servers that refuse SSH_ASKPASS passwords (see: keepwatch docs transfers):
# uv tool install 'keepwatch[conpty]'
keepwatch init                 # config, watches directory, AGENTS.md
keepwatch new hello            # a commented Python watch
keepwatch validate hello
keepwatch poll hello --dry-run
keepwatch install              # start the service at login (systemd user service / Windows logon task)
```

Supported: Linux with systemd, and Windows 10/11 (Task Scheduler; hooks in PowerShell, Python or any program).
On Windows, `keepwatch new <name> --template powershell` starts a PowerShell watch, and `keepwatch stop` stops the service.

## Learn more

```sh
keepwatch tui            # a live view of every watch (read-only)
keepwatch docs           # list the reference topics
keepwatch docs agent     # the contract for writing a watch (start here)
keepwatch docs --all     # everything
```

## Development

```sh
uv sync
uv run pytest -q
uv run ruff check src tests
```

The design is in `docs/superpowers/specs/`; the implementation plans are in `docs/superpowers/plans/`.

## License

MIT
