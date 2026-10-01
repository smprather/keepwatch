# keepwatch

Poll conditions and run actions, from login onward.

keepwatch runs *watches*: small directories holding a `config.toml` plus a check and some actions, written in Python, shell, Expect or anything executable. It polls each check on its own interval, remembers whether the condition is TRUE or FALSE, runs actions while it is TRUE or FALSE and when it changes, retries with backoff, takes failing watches offline, and logs everything as JSON lines, so any failure can be explained afterwards. The command line and its reference are written for AI agents as much as for people.

## Install

```sh
uv tool install git+https://github.com/smprather/keepwatch
keepwatch init                 # config, watches directory, AGENTS.md
keepwatch new hello            # a commented Python watch
keepwatch validate hello
keepwatch poll hello --dry-run
keepwatch install              # start the service at login (systemd user service)
```

Linux with systemd is supported today.

## Learn more

```sh
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
