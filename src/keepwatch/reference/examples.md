# Example watches

Complete, tested watches. Every file of each example is printed in full below: recreate the files in a new directory under your watches directory (or start from `keepwatch new`), then edit `[settings]`.

- **psg-export** (Python): send every finished file matching a pattern to a server with scp, exactly once, using a ledger. Shows payloads, `ctx.run`, `unchanged_for`, `file_key` and why the work is in `on_true`.
- **site-down** (shell): watch a web site with curl. Shows `[check_exit_codes]` with `unknown` codes, TRUE meaning "something is wrong", and `on_rise`/`on_fall` notifications.
- **greet-once** (Expect): a one-time task through an interactive program. Shows Expect handling prompts, and a marker in `KEEPWATCH_DATA_DIR` that turns the condition FALSE once the work is done.
- **disk-space-ps** (PowerShell, Windows): warn when a drive runs low on space. Shows `.ps1` hooks, settings as environment variables, and exit codes from PowerShell.
- **relay-pull**, **relay-push** and **relay-receive** (recipes): move files from one host through this
  machine to another, unattended. Shows the `pull` and `push` recipes end to end, `delete_remote`, the
  `.sha256` marker and a receiving side that waits for it - see `keepwatch docs relay`.
