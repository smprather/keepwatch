# Third-party Python packages

watch.py can import the standard library and `keepwatch`. To use anything else, list it in the watch's config.toml:

```toml
python_dependencies = ["requests>=2.32", "paramiko==3.5.0"]
```

Entries are PEP 508 requirements, exactly as you would give them to pip. They need [uv](https://docs.astral.sh/uv/) on PATH.

## How it works

- A watch with `python_dependencies` runs its Python hooks as `uv run --no-project --with <each dependency> python -m keepwatch.worker`. uv builds one environment per distinct set of dependencies and caches it, so after the first build a hook starts in a few tens of milliseconds more than without dependencies.
- The worker imports the running keepwatch (not a copy from PyPI) through a small `PYTHONPATH` directory, so the hook API always matches the service.
- Polls first try `uv run --offline`, which uses only the cache. Only if that cannot start the worker (the environment has not been built yet) is the hook retried once with network access, within the same deadline.
- `keepwatch validate` builds the environment (with network access) before importing watch.py, so a watch you have validated works at login without a network.
- Watches without `python_dependencies` do not use uv at all.

## Advice

- Pin versions (`==`) for watches that must keep working unattended; an unpinned dependency can change when uv's cache is refreshed.
- Run `keepwatch validate NAME` after changing `python_dependencies`.
- If uv's cache is cleaned (`uv cache clean`), the next poll needs the network once, or run `keepwatch validate` again.
- Errors: "python_dependencies needs uv on PATH" (install uv, or make it visible to the service's PATH; see `keepwatch docs environment`) and "uv could not build the environment" (a requirement that does not resolve; the message ends with uv's own error).
