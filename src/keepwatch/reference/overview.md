# Overview

keepwatch is a command-line tool that starts at login and runs in the background. It runs a set of **watches**. Each watch periodically runs a **check** that answers a yes/no question about the world, remembers the answer, and runs **actions** when the answer is, or becomes, TRUE or FALSE. Everything that happens is written to a JSON-lines log, so any failure can be explained after the fact.

## Vocabulary

| Term | Meaning |
|---|---|
| watch | A directory with a `config.toml` and code. Its name is the directory name. |
| poll | One run of a watch: the check, then the actions the state rules call for. |
| check | The hook that looks at the world and produces an outcome. It never changes anything. |
| outcome | What one check produced: `true`, `false`, `unknown`, `timeout` or `error`. |
| condition | What the watch currently believes: TRUE or FALSE. Only `true`/`false` outcomes change it. |
| action | `on_rise`, `on_fall`, `on_true` or `on_false`. |
| payload | JSON data the check returns with its answer; handed to the actions and logged. |
| service | The long-running `keepwatch run` process (started at login by `keepwatch install`). |
| master tick | The service's periodic pass (every `reload_interval`, default 5s) that picks up config changes. |
| offline | A watch stopped by the failure limit or by `keepwatch disable`. |
| ledger | A persistent, named set of string keys a watch uses to remember what it has done. |

## How it fits together

- `keepwatch run` (the service) runs one scheduler thread per watch and a master tick that reloads changed config files and writes `status.json`.
- Every hook call runs in a fresh child process in its own process group: Python hooks through `python -m keepwatch.worker`, command hooks directly. A timeout kills the whole group, including anything the hook started.
- The service decides everything: which hook to run, what the outcome means, when to back off, when a watch goes offline. Hooks only report.
- The other commands work without talking to the service: they read the log, `status.json` and `offline.json`, and `enable`/`disable` write `offline.json`, which the service notices within a second.

## Commands

| Group | Commands |
|---|---|
| Run | `run` |
| Develop | `new`, `validate`, `poll` |
| Inspect | `status`, `logs` |
| Control | `enable`, `disable`, `rename` |
| Setup | `init`, `install`, `uninstall` |
| Reference | `docs` |

Details: `keepwatch docs cli`, or `keepwatch <command> --help`.
