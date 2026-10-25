# watchglass

Poll conditions and run actions, from login onward.

watchglass runs *watches*: small directories holding a `config.toml` plus a check and some actions, written in Python, shell, Expect or anything executable. It polls each check on its own interval, remembers whether the condition is TRUE or FALSE, runs actions while it is TRUE or FALSE and when it changes, retries with backoff, takes failing watches offline, and logs everything as JSON lines, so any failure can be explained afterwards. The command line and its reference are written for AI agents as much as for people.

Watches can also react to events instead of polling: observers keep commands, local directories or remote directories (over one ssh connection, with nothing installed remotely) under watch. Built-in recipes move files between machines with verified scp transfers, and `recipe = "mirror"` keeps a whole tree in sync in place, the way rsync would; `watchglass docs relay` walks through relaying files host A → this machine → host B.

`watchglass tui` watches all of it live in one screen: every watch and its state, the countdown to its next poll, and the actions it has taken with their results. It is read-only, so it is safe to leave open beside the running service.

## Install

```sh
uv tool install watchglass
# (Windows installs get pywinpty automatically; `watchglass[conpty]` still works as an alias.)
watchglass init                 # config, watches directory, AGENTS.md
watchglass new hello            # a commented Python watch
watchglass validate hello
watchglass poll hello --dry-run
watchglass install              # start the service at login (systemd user service / Windows logon task)
```

Supported: Linux with systemd, and Windows 10/11 (Task Scheduler; hooks in PowerShell, Python or any program).
On Windows, `watchglass new <name> --template powershell` starts a PowerShell watch, and `watchglass stop` stops the service.

## Upgrading from keepwatch

`watchglass` is the new name for this project; the PyPI name `keepwatch` was taken. Stop the old service before
installing the new one, or the first command will refuse to move its configuration:

```sh
keepwatch stop
systemctl --user disable --now keepwatch.service   # Linux; Windows: remove the old logon task/shortcut
uv tool uninstall keepwatch
uv tool install watchglass
```

The first `watchglass` command moves `~/.config/keepwatch` and `~/.local/state/keepwatch` (or their Windows
equivalents) to the `watchglass` paths once, then `watchglass install` registers the new service. Run directories
are not moved. Environment variables are renamed too: `KEEPWATCH_*` becomes `WATCHGLASS_*` in scripts and
service environments. See `watchglass docs storage` for the paths.

## Learn more

```sh
watchglass tui            # a live view of every watch (read-only)
watchglass docs           # list the reference topics
watchglass docs agent     # the contract for writing a watch (start here)
watchglass docs --all     # everything
```

## Development

```sh
uv sync
uv run pytest -q
uv run ruff check src tests
uv run ty check src     # types; the tests are typed only as far as ruff and pytest require
```

The design is in `docs/superpowers/specs/`; the implementation plans are in `docs/superpowers/plans/`.

## License

MIT
