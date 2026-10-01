# Live configuration

The running service picks up changes without a restart. Every master tick (`reload_interval`, default 5s) it:

- re-reads the global config if its modification time or size changed. New `[defaults]` reach every watch;
- rescans every directory in `watch_dirs`. New watches start (from `initial_condition`); watches whose directory disappeared stop after their current poll (logged as `watch.removed`); a changed `config.toml` applies from that watch's next poll, keeping its condition and failure count (`watch.changed`).

**A broken edit never stops a working watch.** If a config file no longer parses or validates, the service keeps running the last valid version, logs one `config.error` record per distinct broken version (with file, line and the fix), and `keepwatch status` shows the error. A brand-new watch whose config is invalid is not started until it is fixed. A broken global config at service start is fatal: `keepwatch run` exits 1 with the error.

**Code needs no reload.** Every hook call starts a fresh process that imports watch.py and runs scripts from disk, so a code edit applies at that watch's next hook call, even sooner than a config edit.

**Control files are read every second.** `keepwatch enable` and `keepwatch disable` work by deleting or writing the watch's `offline.json`; the watch's scheduler checks it about once a second.

Not reloaded while running: the `[log]` rotation settings (read at start) and `--watch` limits given to `keepwatch run`.

Duplicate watch names across `watch_dirs`: the first directory listed wins; the others are reported as problems and skipped.
