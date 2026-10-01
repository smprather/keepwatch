# keepwatch Plan 3b (Reference Documentation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship keepwatch's online reference (`keepwatch docs`), tuned for AI agents that write watches: narrative topics, topics generated from the code, tested example watches, `AGENTS.md`/`CLAUDE.md` written by `init`, the help-text pointer, and a README.

**Architecture:** Narrative topics are Markdown files in `src/keepwatch/reference/`; the `config`, `ctx`, `cli` and `examples` topics append generated sections (from the config key definitions, `Ctx`/`Ledger` docstrings and signatures, the rich-click command tree, and the example watch directories in `src/keepwatch/examples/`). `keepwatch docs` prints raw Markdown when stdout is not a terminal and renders it with Rich on a terminal. Tests enforce that every key, member, command and option is documented and that every example watch validates and polls.

**Tech Stack:** Python ≥ 3.12, rich-click (rich's Markdown renderer), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md` (section 16). Builds on Plans 1, 1b, 2 and 3a (implemented).

## Global Constraints

- All Global Constraints of Plans 1, 2 and 3a apply.
- The Markdown and example files below are product text: copy them exactly (they are written to match the implemented behaviour). If you find a statement that contradicts the code, stop and report it rather than editing either.
- Markdown files use 2-space indentation where indentation is needed; never tabs.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- Piped `keepwatch docs <topic>` must be raw Markdown (no ANSI codes, no re-wrapping). Test: Task 1 `test_docs_topic_is_raw_markdown_when_piped`.
- An unknown topic must suggest the closest one. Test: Task 1 `test_unknown_topic_suggests`.
- Every config key, `Ctx`/`Ledger` member, command and option must be documented. Tests: Task 3 coverage tests.
- Every example watch must pass `validate` and a poll. Test: Task 4 `test_examples_validate_and_poll`.
- `init` must never overwrite an existing `AGENTS.md`. Test: Task 5 `test_init_writes_agent_files_once`.

---

### Task 1: The `docs` command and topic machinery

**Files:**
- Create: `src/keepwatch/reference.py`, `src/keepwatch/reference/` (Markdown topics; Task 1 adds `agent.md` and `overview.md`)
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_docs.py`

**Interfaces:**
- Produces:
  - `reference.TOPICS: tuple[tuple[str, str], ...]` — (name, one-line summary) in reading order
  - `narrative(name: str) -> str` (reads `reference/<name>.md` from the package)
  - `render_topic(name: str) -> str` (raises `KeyError` for unknown names); `topic_index() -> str`; `render_all() -> str`
  - `GENERATED: dict[str, Callable[[], str]]` — topics that append generated sections (filled in by Task 3; Task 1 starts it empty)
  - `keepwatch docs [TOPIC] [--all]` in a new "Reference" help panel

- [ ] **Step 1: Write the failing tests** — `tests/test_docs.py`

```python
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.reference import TOPICS, render_all, render_topic, topic_index


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_every_topic_renders():
    for name, summary in TOPICS:
        text = render_topic(name)
        assert text.startswith("# "), name
        assert summary
    assert all(f"`{name}`" in topic_index() for name, _ in TOPICS)


def test_render_all_contains_every_topic_in_order():
    text = render_all()
    positions = [text.index("\n" + render_topic(name).splitlines()[0]) for name, _ in TOPICS]
    assert positions == sorted(positions)


def test_docs_without_topic_lists_topics(xdg):
    result = run("docs")
    assert result.exit_code == 0
    assert "keepwatch docs agent" in result.output


def test_docs_topic_is_raw_markdown_when_piped(xdg):
    result = run("docs", "agent")
    assert result.exit_code == 0
    assert result.output == render_topic("agent") + "\n"
    assert "\x1b[" not in result.output


def test_unknown_topic_suggests(xdg):
    result = run("docs", "agnet")
    assert result.exit_code == 1
    assert "did you mean 'agent'" in result.output


def test_help_points_agents_at_the_docs(xdg):
    assert "keepwatch docs agent" in run("--help").output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.reference'`

- [ ] **Step 3: Implement.** Create `src/keepwatch/reference.py`:

```python
"""keepwatch's online reference: narrative Markdown topics plus sections generated from the code."""

from __future__ import annotations

import difflib
from collections.abc import Callable
from importlib import resources

TOPICS: tuple[tuple[str, str], ...] = (
    ("agent", "Start here: the whole contract, the development loop, a done checklist, common mistakes"),
    ("overview", "What keepwatch does, its vocabulary and how the pieces fit"),
    ("config", "Every key of the global and watch config files"),
    ("python", "watch.py: hooks, return values, exceptions, imports"),
    ("ctx", "Every Ctx and Ledger member, from the code"),
    ("executables", "Command hooks: exit codes, environment variables, payload files"),
    ("states", "Outcomes, the state table, startup, retries and ordering"),
    ("failures", "Failed polls, backoff, offline, enable, retry_after and alert_command"),
    ("storage", "Directories, persistent and run-only data, ledgers, renaming"),
    ("logging", "The log file, every event, ctx.log and keepwatch logs"),
    ("environment", "What hooks run inside: cwd, stdin, variables, PATH, ssh-agent, timeouts"),
    ("dependencies", "python_dependencies and uv"),
    ("reload", "How config changes are picked up while the service runs"),
    ("cli", "Every command and option"),
    ("examples", "Complete example watches (tested)"),
)

GENERATED: dict[str, Callable[[], str]] = {}


class UnknownTopic(KeyError):
    """No topic with that name; str() suggests the closest one."""

    def __str__(self) -> str:
        name = self.args[0]
        names = [topic for topic, _ in TOPICS]
        close = difflib.get_close_matches(name, names, n=1)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        return f"no docs topic '{name}'{hint}; topics: {', '.join(names)}"


def narrative(name: str) -> str:
    return resources.files("keepwatch").joinpath("reference", f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def render_topic(name: str) -> str:
    if name not in dict(TOPICS):
        raise UnknownTopic(name)
    if name in GENERATED:
        return GENERATED[name]()
    return narrative(name)


def topic_index() -> str:
    lines = [
        "# keepwatch reference",
        "",
        "Read a topic with `keepwatch docs <topic>`; `keepwatch docs --all` prints every topic in this order.",
        "New to keepwatch, or writing a watch? Start with `keepwatch docs agent`.",
        "",
        "| Topic | Contents |",
        "|---|---|",
    ]
    lines += [f"| `{name}` | {summary} |" for name, summary in TOPICS]
    return "\n".join(lines)


def render_all() -> str:
    return "\n\n".join([topic_index(), *(render_topic(name) for name, _ in TOPICS)])
```

Create `src/keepwatch/reference/agent.md`:

````markdown
# Writing a keepwatch watch (start here)

keepwatch runs *watches*. A watch is a directory holding a `config.toml` and some code. On every poll keepwatch runs the watch's **check**, which answers TRUE or FALSE, and then runs the **actions** that the state rules call for. You write the check and the actions; keepwatch does the scheduling, state keeping, timeouts, retries, failure limits and logging.

## The contract

1. **Location.** A watch is a subdirectory of a watches directory: `$XDG_CONFIG_HOME/keepwatch/watches/<name>/` by default (`~/.config/keepwatch/watches/<name>/`), or any directory listed in `watch_dirs` of the global config. The directory name is the watch name: letters, digits, `_`, `.` and `-`, starting with a letter or digit. Directories starting with `.` or `_` are ignored.
2. **config.toml** is required (it may be empty). Unknown keys are errors. Put the watch's own parameters in `[settings]`; Python reads them as `ctx.settings`, commands as `KEEPWATCH_SETTING_<KEY>`. See `keepwatch docs config`.
3. **Hooks.** `check` is required; `on_rise`, `on_fall`, `on_true` and `on_false` are optional. Each hook is either a top-level `def <hook>(ctx):` in `watch.py` (see `keepwatch docs python`) or a command in `[hooks]` of config.toml (see `keepwatch docs executables`), never both. Hooks written any other way (imported, assigned, `async def`, nested) never run; `keepwatch validate` reports them.
4. **The check MUST only look, never change anything.** It returns `True` or `False`, `(answer, payload)` to hand JSON data to the actions, or `None` for "cannot tell right now" (or raises `keepwatch.Unknown("reason")`). Any other return value or exception is an error.
5. **Actions** succeed by returning (exit status 0) and fail by raising (nonzero exit).
6. **When actions run** (full rules: `keepwatch docs states`):
   - `on_true` runs on every poll while the condition is TRUE; `on_false` on every poll while it is FALSE.
   - `on_rise` runs once when the condition becomes TRUE, `on_fall` once when it becomes FALSE. A failed `on_rise`/`on_fall` is retried on later polls until it succeeds or the condition flips back.
   - Every watch starts FALSE (`initial_condition = false`), so a condition that is already TRUE when keepwatch starts fires `on_rise`. Write checks so that TRUE is the thing to act on.
   - Actions can run more than once. Make them safe to repeat, and record finished work in a ledger (`ctx.ledger`).
7. **Every hook runs in a fresh process** with stdin `/dev/null`, the watch directory as working directory, and a deadline (`check_timeout`, `action_timeout`, default 60s). Nothing may prompt for input. See `keepwatch docs environment`.
8. **State** belongs in `ctx.data_dir` and ledgers (persistent) or `ctx.run_dir` (until keepwatch exits), never in the watch directory. See `keepwatch docs storage`.

## Development loop

```sh
keepwatch new <name>                                  # python template; --template shell or expect
keepwatch validate <name>                             # every config and code problem, with line numbers
keepwatch poll <name> --fake true,true,false --dry-run   # the state logic only: which actions would run
keepwatch poll <name> --dry-run                       # the real check; actions are reported, not run
keepwatch poll <name>                                 # one real poll, everything printed as it happens
keepwatch logs --poll <id> -v                         # everything a poll did, with captured output
```

`validate`, `poll`, `status` and `logs` accept `--json`. Exit status is 0 for success, 1 for problems found or a failed poll, 2 for wrong usage.

## Done checklist

- [ ] `keepwatch validate <name>` exits 0.
- [ ] `keepwatch poll <name> --dry-run` shows the check answering correctly for the current state of the world.
- [ ] `keepwatch poll <name> --fake true,true,false --dry-run` plans exactly the actions you intend.
- [ ] A real `keepwatch poll <name>` succeeds, and running it again does not repeat work already done.
- [ ] Anything that can hang (network, ssh, scp) is non-interactive (`ssh -o BatchMode=yes`) and fits within the timeouts.
- [ ] `description` in config.toml says in one line what the watch does.

## Common mistakes

- Returning a list or a string from `check`. Return `bool(items), items`.
- Changing things in `check` (sending, deleting, adding to a ledger). Checks run for real during `--dry-run`, and ledgers are read-only in `check`.
- Doing per-item work in `on_rise`. It runs once per FALSE→TRUE change, so items that arrive while the condition stays TRUE are missed. Use `on_true` plus a ledger.
- Running commands that prompt: ssh passwords, unknown host keys, `sudo`. stdin is `/dev/null`, so they fail or hang until the timeout.
- Assuming your login shell's environment. The service runs under systemd with the PATH captured by `keepwatch install` and without your shell's ssh-agent. See `keepwatch docs environment`.
- Setting a ledger `expire` for things that stay visible. When an entry expires, the thing it stood for is processed again.
- Renaming a watch directory with `mv`. Its state stays behind under the old name, and the watch starts with empty ledgers. Use `keepwatch rename`.
- Storing data in the watch directory. Use `ctx.data_dir`.

## Reference topics

Run `keepwatch docs` for the list of topics, or `keepwatch docs --all` for everything at once.
````

Create `src/keepwatch/reference/overview.md`:

````markdown
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
````

In `src/keepwatch/cli.py`: add `{"name": "Reference", "commands": ["docs"]}` as the last panel of `COMMAND_GROUPS`; add `from rich.markdown import Markdown` and `from keepwatch.reference import UnknownTopic, render_all, render_topic, topic_index` to the imports; replace the group docstring (the `cli` function's docstring) with:

```python
    """Poll conditions and run actions.

    Each watch is a directory holding a config.toml plus the code for its check and actions.

    Writing or fixing a watch? Read `keepwatch docs agent` first, or `keepwatch docs --all` for the complete reference.
    """
```

and add before `def main()`:

```python
@cli.command()
@click.argument("topic", required=False)
@click.option("--all", "show_all", is_flag=True, help="Print every topic, in reading order.")
def docs(topic: str | None, show_all: bool) -> None:
    """Print the reference as Markdown. With no TOPIC, list the topics.

    When stdout is not a terminal (agents, pipes), the raw Markdown is printed unchanged; on a terminal it is
    rendered. Start with `keepwatch docs agent`.

    Exit status: 0, or 1 for an unknown topic.
    """
    if show_all:
        text = render_all()
    elif topic is None:
        text = topic_index()
    else:
        try:
            text = render_topic(topic)
        except UnknownTopic as exc:
            _fail(str(exc))
    if plain_output():
        click.echo(text)
    else:
        make_console().print(Markdown(text))
```

Create the remaining topic files as empty placeholders **only for the duration of this task** so `render_topic` works for every name: for each of `config python ctx executables states failures storage logging environment dependencies reload cli examples`, create `src/keepwatch/reference/<name>.md` containing exactly `# <name>` (Tasks 2–4 replace every one of them).

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/reference.py src/keepwatch/reference src/keepwatch/cli.py tests/test_docs.py
git commit -m "Add keepwatch docs with the agent and overview topics"
```

---

### Task 2: Narrative topics

**Files:**
- Replace: `src/keepwatch/reference/{python,executables,states,failures,storage,logging,environment,dependencies,reload}.md`
- Test: `tests/test_docs.py`

- [ ] **Step 1: Write the failing test.** Add `import pytest` to the imports at the top of `tests/test_docs.py` (imports must stay at the top; ruff rejects them anywhere else), then append:

```python
KEY_FACTS = {
    "python": ["keepwatch.Unknown", "(True, payload)", "1 MiB", "keepwatch_watch", "top-level"],
    "executables": ["KEEPWATCH_PAYLOAD_OUT", "KEEPWATCH_PAYLOAD_FILE", "KEEPWATCH_SETTINGS_FILE", "[check_exit_codes]", "/bin/sh -c"],
    "states": ["| `true` |", "initial_condition", "stop at the first failure", "never overlap"],
    "failures": ["max_failures", "retry_after", "alert_command", "KEEPWATCH_ALERT_EVENT", "offline.json"],
    "storage": ["ctx.data_dir", "ctx.run_dir", "file_key", "LedgerCorrupt", "keepwatch rename"],
    "logging": ["keepwatch.jsonl", "poll_id", "hook.end", "check.outcome", "extra="],
    "environment": ["/dev/null", "SIGTERM", "SSH_AUTH_SOCK", "BatchMode=yes", "[environment]"],
    "dependencies": ["python_dependencies", "uv run", "--offline", "keepwatch validate"],
    "reload": ["reload_interval", "last valid", "watch.removed", "offline.json"],
}


@pytest.mark.parametrize("topic", sorted(KEY_FACTS))
def test_narrative_topics_cover_their_key_facts(topic):
    text = render_topic(topic)
    missing = [fact for fact in KEY_FACTS[topic] if fact not in text]
    assert not missing, f"{topic} is missing {missing}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_docs.py -q`
Expected: 9 FAIL (placeholder topics)

- [ ] **Step 3: Write the topics** exactly as below.

`src/keepwatch/reference/python.md`:

````markdown
# Python hooks (watch.py)

A watch's `watch.py` may define these module-level functions. Each one receives a `Ctx` (see `keepwatch docs ctx`).

```python
from keepwatch import Ctx

def check(ctx: Ctx): ...            # required (unless [hooks] has check)
def on_rise(ctx: Ctx) -> None: ...  # the condition became TRUE
def on_fall(ctx: Ctx) -> None: ...  # the condition became FALSE
def on_true(ctx: Ctx) -> None: ...  # every poll while TRUE
def on_false(ctx: Ctx) -> None: ... # every poll while FALSE
```

Only **top-level `def` statements** count as hooks. A hook that is imported (`from helpers import on_rise`), assigned (`on_rise = make()`), written as `async def` or defined inside an `if` never runs. `keepwatch validate` reports such hooks.

## What check returns

| Return value | Outcome |
|---|---|
| `True` / `False` | `true` / `false` |
| `(True, payload)` / `(False, payload)` | the answer, plus a payload for the actions |
| `None`, or `(None, payload)` | `unknown`: the check ran fine but cannot tell right now |
| `raise keepwatch.Unknown("reason")` | `unknown`, with the reason in the log |
| anything else (a list, a string, a number) | `error` |
| any other exception | `error`, with the traceback in the log |

The payload must be JSON-serializable and at most 1 MiB once encoded. `pathlib.Path` values become strings and tuples become lists; anything else that JSON cannot represent (sets, objects) makes the outcome `error`. The actions receive the payload as `ctx.payload`, and it is logged in full.

## Actions

An action succeeds when it returns and fails when it raises. Its return value is ignored. `ctx.run` raises `keepwatch.CommandFailed` for a nonzero exit, so a failed command fails the action unless you catch it.

## Rules

- **check MUST only observe.** It must not change anything outside `ctx.run_dir`: `keepwatch poll --dry-run` runs real checks, and ledgers are read-only inside `check` (writing raises `keepwatch.LedgerReadOnly`).
- `keepwatch.Unknown` only has a meaning in `check`; raising it in an action is an error.
- Each hook call is a **fresh process**: module-level variables do not survive between calls or from check to actions. Pass data from check to actions through the payload; keep anything longer-lived in `ctx.data_dir` or a ledger.
- watch.py is imported as the module `keepwatch_watch`. The watch directory is first on `sys.path`, so `watch.py` can import sibling modules (`helpers.py`) and packages in the watch directory. The working directory is the watch directory.
- `print()` and anything written to stdout or stderr (including by programs you start without `ctx.run`) is captured into the hook's log record (the first and last 32 KiB with the default `capture_bytes`).
- Logging: use `ctx.log` (a standard `logging.Logger`); see `keepwatch docs logging`.
- Third-party packages: list them in `python_dependencies`; see `keepwatch docs dependencies`. Without it, watch.py can import the standard library and `keepwatch`.
- `sys.exit()` in a hook is treated like any other exception: the hook fails.
````

`src/keepwatch/reference/executables.md`:

````markdown
# Command hooks (shell, Expect, any language)

Any hook can be a command instead of a Python function. Define it in `[hooks]` of config.toml:

```toml
[hooks]
check   = "test -e /run/vpn.up"            # a string runs through /bin/sh -c
on_true = ["expect", "./send.exp"]          # a list runs directly (argv)
on_fall = ["./notify.sh", "VPN went down"]
```

A hook defined both in `[hooks]` and in watch.py is an error. A program named with a `/` (`./notify.sh`) is relative to the watch directory and must be executable; a bare name (`expect`) is looked up on PATH. `keepwatch validate` checks list-form programs; strings are only checked when they run.

## What the exit code means

For the **check**, `[check_exit_codes]` maps exit codes to outcomes:

```toml
[check_exit_codes]
true = [0]        # default
false = [1]       # default
unknown = []      # default: no code means "unknown"
```

Any exit code that is not listed is an `error`; a check killed by a signal is an `error`; one that runs past `check_timeout` is a `timeout`. No code maps to `unknown` by default because many programs use low codes for real errors (curl exits 3 for a malformed URL). List the codes that mean "cannot tell right now", for example curl's `unknown = [6, 7, 28]` (cannot resolve host, cannot connect, timed out). A code may appear in only one list.

An **action** succeeds with exit status 0 and fails otherwise.

## What the command receives

- Working directory: the watch directory. stdin: `/dev/null`.
- Environment: the service's environment, then the global `[environment]`, then the watch's `[environment]`, then these variables. Inherited `KEEPWATCH_*` variables are removed first (except `KEEPWATCH_CONFIG`).

| Variable | Meaning |
|---|---|
| `KEEPWATCH_WATCH` | The watch name. |
| `KEEPWATCH_HOOK` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `KEEPWATCH_POLL_ID` | This poll's ID; it is on every log record of the poll. |
| `KEEPWATCH_CONDITION` | `true` or `false`: the condition before the answer in the check, after it in actions. |
| `KEEPWATCH_WATCH_DIR` | The watch directory. |
| `KEEPWATCH_DATA_DIR` | Persistent storage for this watch (created before the call). |
| `KEEPWATCH_RUN_DIR` | Run-only scratch space (created before the call). |
| `KEEPWATCH_SETTINGS_FILE` | A JSON file holding the whole `[settings]` table. |
| `KEEPWATCH_SETTING_<KEY>` | Each top-level scalar setting; the key is upper-cased and anything other than letters and digits becomes `_` (`dest-host` → `KEEPWATCH_SETTING_DEST_HOST`). Booleans are `true`/`false`. |
| `KEEPWATCH_PAYLOAD_OUT` | Check only: write JSON here to hand a payload to the actions. |
| `KEEPWATCH_PAYLOAD_FILE` | Actions only, and only when there is a payload: the JSON file holding it. |

Python hooks get the same variables, so programs they start can use them too.

## Passing a payload from a command check

```sh
#!/bin/sh
files=$(ls ~/incoming/*.tar.gz 2>/dev/null) || exit 1   # nothing there: FALSE
printf '%s\n' "$files" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().split()))' > "$KEEPWATCH_PAYLOAD_OUT"
exit 0
```

An empty or missing payload file means no payload. A file that is not valid JSON, or is larger than 1 MiB, makes the outcome `error`. The payload and settings files are deleted after the call.

## Output

stdout and stderr are captured into the hook's log record (the first and last 32 KiB of each with the default `capture_bytes`). Non-UTF-8 bytes are replaced, not fatal. A command that leaves a background process holding its stdout open is not waited for: once the command exits, keepwatch waits two seconds and then kills its process group.

## Expect

Expect spawns programs on their own terminal, so it can drive interactive logins even though the hook's stdin is `/dev/null`. Always set `timeout` and handle it (`timeout { exit 1 }`), so a missing prompt fails the action instead of waiting for `action_timeout`. Read settings as `$env(KEEPWATCH_SETTING_HOST)`.
````

`src/keepwatch/reference/states.md`:

````markdown
# Outcomes and the state rules

## Outcomes

| Outcome | Meaning |
|---|---|
| `true` / `false` | An answer. |
| `unknown` | The check ran correctly but cannot tell right now (Python `None` or `keepwatch.Unknown`; a command's `unknown` exit codes). |
| `timeout` | keepwatch killed the check at `check_timeout`. |
| `error` | The check broke: an exception, an unlisted exit code, a signal, a bad return value or payload, or it could not start. |

## What one poll does

| Check says | Condition was FALSE | Condition was TRUE |
|---|---|---|
| `true` | becomes TRUE; run `on_rise`, then `on_true` | run `on_true` |
| `false` | run `on_false` | becomes FALSE; run `on_fall`, then `on_false` |
| `unknown` | nothing runs; condition unchanged | nothing runs; condition unchanged |
| `timeout` / `error` | nothing runs; condition unchanged; the poll fails | same |

## Rules

1. **Start.** When the service starts, and when a watch appears while it runs, the condition is `initial_condition` (default `false`). A condition that is already TRUE then fires `on_rise`; one that is FALSE fires nothing. Write the check so that TRUE is the thing to act on.
2. **Edges are retried until they succeed.** A FALSE→TRUE change leaves a pending `on_rise` that is cleared only when `on_rise` succeeds; until then it runs again on every poll that answers `true`. If the condition flips back first, the pending edge is replaced by the opposite one. So actions run *at least once*: make them safe to repeat.
3. **Actions in one poll run in order and stop at the first failure.** If `on_rise` fails, `on_true` does not run in that poll.
4. **Polls of one watch never overlap.** `interval` is measured from the end of a poll to the start of the next (longer after failures; see `keepwatch docs failures`). Different watches run independently and in parallel.
5. **Config changes keep the state.** A changed config applies from the next poll; the condition, pending edge and failure count are kept.
6. **Missing hooks are skipped.** A pending edge whose hook does not exist is dropped at once.
7. **The condition is not saved across restarts.** Restarting the service starts every watch from `initial_condition` again; durable facts belong in ledgers.

## Testing the rules

`keepwatch poll NAME --fake true,true,false,timeout --dry-run` feeds made-up outcomes through these rules without running the check, and shows which actions each poll would run. Dry runs assume every action succeeds. Without `--dry-run`, the actions run for real with the payload from `--payload`. `--initial-condition true` starts from TRUE. A manual poll starts from `initial_condition` and does not share the running service's condition or failure count.
````

`src/keepwatch/reference/failures.md`:

````markdown
# Failures, backoff and going offline

## What fails a poll

- An action fails (nonzero exit, exception, or `action_timeout`).
- The check times out or errors.

An `unknown` outcome neither fails nor succeeds a poll. A poll in which the check answered and every action that ran succeeded resets the failure count to 0. Check failures count on purpose: a broken check never runs an action, so an action-only limit would let a broken watch do nothing forever without anyone being told.

## Backoff

After the k-th consecutive failed poll, the wait before the next poll is

```
min(max(interval, 60s) * 2^(k-1), max(interval, 1h))
```

With the default `max_failures = 5` the waits are 1m, 2m, 4m and 8m, so a watch goes offline after about 15 minutes of continuous failure whatever its interval. The slow retry also keeps a failing `scp` from producing a burst of failed ssh logins, which tools like fail2ban punish with an IP ban.

## Going offline

When the failure count reaches `max_failures` (default 5; `0` means never):

- the watch stops polling;
- `offline.json` is written to its state directory (`$XDG_STATE_HOME/keepwatch/watches/<name>/offline.json`) with the reason, the time and a summary of the last failure, so restarting the service does not quietly resume it;
- a `watch.offline` record is logged at CRITICAL;
- `alert_command` runs.

## Coming back online

- `keepwatch enable NAME` deletes `offline.json`. A running service notices within a second, resets the failure count and polls at once.
- With `retry_after` set (for example `retry_after = "1h"`), an offline watch makes one **trial poll** every `retry_after`. A trial poll that succeeds (the check answers `true` or `false` and every action that runs succeeds) brings the watch back online. Any other trial poll, including one answering `unknown`, leaves it offline until the next trial. Set `retry_after` on watches that fail for outside reasons, like a laptop being off the network.
- `keepwatch disable NAME` takes a watch offline by hand (reason "disabled by user"); such a watch makes no trial polls and stays offline until `keepwatch enable`.

A poll cut short because the service is stopping never takes a watch offline.

## alert_command

Set `alert_command` in the global config to be told when a watch goes offline or comes back online:

```toml
alert_command = 'notify-send "keepwatch: $KEEPWATCH_WATCH $KEEPWATCH_ALERT_EVENT" "$KEEPWATCH_ALERT_REASON"'
```

It receives `KEEPWATCH_ALERT_EVENT` (`offline` or `online`), `KEEPWATCH_WATCH` and `KEEPWATCH_ALERT_REASON`, plus the global `[environment]`. A string runs through `/bin/sh -c`; a list runs directly. Its timeout is `action_timeout` from `[defaults]` (default 60s). Each run is logged as an `alert.end` record; its failures never count against any watch.
````

`src/keepwatch/reference/storage.md`:

````markdown
# Directories and storage

## Layout

| Path | Contents |
|---|---|
| `$XDG_CONFIG_HOME/keepwatch/config.toml` | The global config (optional). |
| `$XDG_CONFIG_HOME/keepwatch/watches/<name>/` | A watch: `config.toml` plus code. |
| `$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl` | The log, rotated to `keepwatch.jsonl.1`, `.2`, … |
| `$XDG_STATE_HOME/keepwatch/status.json` | Written by the service every master tick. |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/offline.json` | Present while the watch is offline. |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/data/` | The watch's persistent storage (`ctx.data_dir`). |
| `$XDG_STATE_HOME/keepwatch/watches/<name>/data/ledgers/<ledger>.json` | Ledgers. |
| `$XDG_RUNTIME_DIR/keepwatch/<pid>/<name>/` | Run-only scratch space for one keepwatch process (`ctx.run_dir`). |
| `$XDG_RUNTIME_DIR/keepwatch/locks/<name>.lock` | Held while the watch is being polled. |

`XDG_CONFIG_HOME` defaults to `~/.config` and `XDG_STATE_HOME` to `~/.local/state`. `XDG_RUNTIME_DIR` is normally `/run/user/<uid>`: private, in memory, and deleted at logout. Without it keepwatch uses `$TMPDIR/keepwatch-<uid>` (or `/tmp/keepwatch-<uid>`), created with mode 0700, and refuses to use it if anyone else could read it.

## Persistent and run-only data

- `ctx.data_dir` (`KEEPWATCH_DATA_DIR`) survives restarts and reboots. The watch owns its contents and must keep them tidy.
- `ctx.run_dir` (`KEEPWATCH_RUN_DIR`) lasts as long as the keepwatch process: the service, or one manual `keepwatch poll`. Each process has its own; it is deleted when the process exits, and leftovers of crashed processes are removed at the next start.
- Never store data in the watch directory: it holds code and config, and may be a git repository.

## Ledgers

`ctx.ledger(name)` opens a persistent set of string keys, each stamped with the time it was added. Use it to remember finished work ("this file was already sent").

```python
sent = ctx.ledger("sent")                  # read-only inside check
if ctx.file_key(path) not in sent:
    ...
sent.add(ctx.file_key(path))               # saved at once, atomically
```

- Every `add` and `discard` rewrites the file atomically, so a crash in the middle of a loop keeps everything recorded so far.
- `ctx.file_key(path)` is `"<absolute path>|<size>|<mtime_ns>"`: a new file that reuses an old name gets a new key, so it is processed.
- `ctx.ledger(name, expire="90d")` forgets entries older than that. **Only expire keys for things that go away.** If the thing a key stands for can still be seen after its entry expires, the watch processes it again.
- A ledger file that cannot be read raises `keepwatch.LedgerCorrupt`, whose message names the file. Fix it, or delete it to start with an empty ledger.
- The format is JSON: `{"version": 1, "entries": {"<key>": <unix time added>}}`.

## Renaming and deleting watches

State is kept under the watch's name. Rename a watch with `keepwatch rename OLD NEW`, which moves the watch directory and its state together; renaming the directory with `mv` leaves the state behind, and the watch starts with empty ledgers (a watch that sends files would send them all again). Deleting a watch directory leaves its state in place; `keepwatch status` lists such orphaned state directories, and you may delete them by hand.
````

`src/keepwatch/reference/logging.md`:

````markdown
# Logging

## The log file

Everything keepwatch does is written to `$XDG_STATE_HOME/keepwatch/logs/keepwatch.jsonl`, one JSON object per line. When the file would grow past `max_bytes` (default 10 MB) it is rotated to `keepwatch.jsonl.1`, `.2`, … keeping `backups` (default 10) old files. Rotation settings are read when the service starts.

Every record has `ts` (local time, RFC 3339 with offset), `level` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`), `event` and `pid`, and, where they apply, `watch`, `poll_id` and `hook`. All records of one poll share its `poll_id`.

## Events

| Event | Fields |
|---|---|
| `service.start` / `service.stop` | `version`, `config`, `watch_dirs`, `only` |
| `config.loaded` / `config.error` | `path`; `error` (with file and line); `watch` and `running_previous` for a watch's config |
| `watch.added` / `watch.changed` / `watch.removed` | `watch` |
| `poll.start` | `condition`, `faked`, `dry_run`, `trial` |
| `command` | from `ctx.run`: `argv`, `shell`, `exit_code`, `timed_out`, `duration`, `stdout`, `stderr`, `*_truncated` |
| `plugin.log` | from `ctx.log`: `level`, `logger`, `message`, `fields`, `traceback` |
| `hook.end` | `hook`, `kind`, `target`, `status`, `reason`, `payload`, `exit_code`, `signal`, `exception` (type, message, traceback), `stdout`, `stderr`, `*_truncated`, `duration` |
| `check.outcome` | `outcome`, `reason`, `payload`, `condition_before`, `condition_after`, `pending_edge`, `actions`, `faked` |
| `poll.end` | `failed`, `failures`, `condition`, `pending_edge`, `dry_run` |
| `watch.offline` / `watch.online` | `reason`, `by_user`, `last_failure` |
| `watch.crash` | `error`, `traceback` (a bug in keepwatch itself; the watch retries after a minute) |
| `alert.end` | `alert_event`, `command`, `status`, `exit_code`, `reason`, `stdout`, `stderr`, `duration` |

For `hook.end`, `status` is the outcome for a check (`true`, `false`, `unknown`, `timeout`, `error`) and `ok`, `failed` or `timeout` for an action. Captured output keeps the first and last half of `capture_bytes` (default 64 KiB) per stream.

## Logging from a watch

`ctx.log` is a standard `logging.Logger`. Its records become `plugin.log` records tagged with the watch, hook and poll ID, and they appear while the hook runs. Keys passed with `extra=` become JSON fields you can search for:

```python
ctx.log.info("sent %s", path, extra={"file": path, "bytes": size})
ctx.log.exception("upload failed")     # inside an except block: includes the traceback
```

Command hooks log by writing to stdout or stderr; that output lands in the `hook.end` record.

## Reading the log

```sh
keepwatch logs                         # the last 200 records
keepwatch logs psg-export --failed     # failures of one watch
keepwatch logs --poll 3fa9 -v          # one poll, with all captured output
keepwatch logs --since 2h --event hook # every hook.* record of the last two hours
keepwatch logs -f                      # follow new records
keepwatch logs --json                  # the raw records, one JSON object per line
```

`--since`/`--until` take a duration ago (`1h`, `2d`) or a time (`2026-09-30`, `2026-09-30T14:00`). `--failed` selects ERROR-or-worse records, failed hooks, failed polls and failed alerts. `-n` limits the output to the last N matching records (`0` for all).

`keepwatch run` also prints records to its terminal: everything from INFO up on a terminal, only WARNING and up when stdout is not a terminal (for example under systemd, where it goes to the journal), every record with `-v`.
````

`src/keepwatch/reference/environment.md`:

````markdown
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
````

`src/keepwatch/reference/dependencies.md`:

````markdown
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
````

`src/keepwatch/reference/reload.md`:

````markdown
# Live configuration

The running service picks up changes without a restart. Every master tick (`reload_interval`, default 5s) it:

- re-reads the global config if its modification time or size changed. New `[defaults]` reach every watch;
- rescans every directory in `watch_dirs`. New watches start (from `initial_condition`); watches whose directory disappeared stop after their current poll (logged as `watch.removed`); a changed `config.toml` applies from that watch's next poll, keeping its condition and failure count (`watch.changed`).

**A broken edit never stops a working watch.** If a config file no longer parses or validates, the service keeps running the last valid version, logs one `config.error` record per distinct broken version (with file, line and the fix), and `keepwatch status` shows the error. A brand-new watch whose config is invalid is not started until it is fixed. A broken global config at service start is fatal: `keepwatch run` exits 1 with the error.

**Code needs no reload.** Every hook call starts a fresh process that imports watch.py and runs scripts from disk, so a code edit applies at that watch's next hook call, even sooner than a config edit.

**Control files are read every second.** `keepwatch enable` and `keepwatch disable` work by deleting or writing the watch's `offline.json`; the watch's scheduler checks it about once a second.

Not reloaded while running: the `[log]` rotation settings (read at start) and `--watch` limits given to `keepwatch run`.

Duplicate watch names across `watch_dirs`: the first directory listed wins; the others are reported as problems and skipped.
````

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/reference tests/test_docs.py
git commit -m "Write the narrative reference topics"
```

---

### Task 3: Generated topics and documentation coverage

**Files:**
- Modify: `src/keepwatch/reference.py`
- Replace: `src/keepwatch/reference/{config,ctx,cli}.md`
- Test: `tests/test_docs.py`

**Interfaces:**
- Produces: `config_topic()`, `ctx_topic()`, `cli_topic()` registered in `GENERATED`; each returns `narrative(name)` followed by generated sections.

- [ ] **Step 1: Write the failing tests.** Add these imports at the top of `tests/test_docs.py`: `import inspect`, `import click`, `from keepwatch import Ctx, Ledger` and `from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, WATCH_KEYS, WATCH_TABLES` (then `uv run ruff check --fix tests/test_docs.py` sorts them). Append:

```python
def test_every_config_key_is_documented():
    text = render_topic("config")
    for key in (*WATCH_KEYS, *GLOBAL_KEYS, *LOG_KEYS):
        assert key.doc.strip(), key.name
        assert f"`{key.name}`" in text, key.name
    for table, doc in (*WATCH_TABLES.items(), *GLOBAL_TABLES.items()):
        assert doc.strip() and f"`[{table}]`" in text, table


def test_every_public_ctx_and_ledger_member_is_documented():
    text = render_topic("ctx")
    for owner, prefix in ((Ctx, "ctx."), (Ledger, "ledger.")):
        for name, member in inspect.getmembers(owner):
            if name.startswith("_"):
                continue
            assert inspect.getdoc(member), f"{owner.__name__}.{name} has no docstring"
            assert f"{prefix}{name}" in text, name


def test_every_command_and_option_is_documented():
    from keepwatch.cli import cli

    text = render_topic("cli")
    ctx = click.Context(cli, info_name="keepwatch")
    for name in cli.list_commands(ctx):
        command = cli.get_command(ctx, name)
        assert command.help and command.help.strip(), name
        assert f"## keepwatch {name}" in text
        for param in command.params:
            if isinstance(param, click.Option):
                assert param.help, f"{name} {param.opts}"
                assert param.opts[-1] in text
    assert "UNSET" not in text and "Sentinel" not in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_docs.py -q`
Expected: 3 FAIL (placeholder topics)

- [ ] **Step 3: Implement.** Replace `src/keepwatch/reference/config.md` with:

````markdown
# Configuration files

keepwatch reads two kinds of TOML file:

- the **global config**, `$XDG_CONFIG_HOME/keepwatch/config.toml` (or `--config PATH` / `KEEPWATCH_CONFIG`), which is optional; `keepwatch init` writes one with every key commented out;
- each watch's **config.toml**, which is required (it may be empty).

General rules:

- **Durations** are a number of seconds or a string of integer+unit pairs with the units `s`, `m`, `h`, `d`: `"30s"`, `"15m"`, `"1h30m"`, `"90d"`. `nan` and infinity are rejected.
- **Unknown keys are errors** everywhere except inside `[settings]` and `[environment]`, so a typo cannot silently do nothing. Errors give the file, the line, the problem and the nearest valid key.
- `~` and `$VARIABLES` are expanded in `watch_dirs` (relative entries are relative to the config file's directory); `[settings]` is passed to hooks as written, except that TOML dates and times become ISO 8601 strings.
- Changes apply while the service runs; see `keepwatch docs reload`.
- `keepwatch validate` checks every file and reports every problem at once.
````

Replace `src/keepwatch/reference/ctx.md` with:

````markdown
# The Ctx object

Every Python hook receives one argument, `ctx`, a `keepwatch.Ctx`. The package ships type hints (`py.typed`), so editors can check watch.py against it.

## Attributes

| Attribute | Type | Meaning |
|---|---|---|
| `ctx.watch` | `str` | The watch name. |
| `ctx.hook` | `str` | `check`, `on_rise`, `on_fall`, `on_true` or `on_false`. |
| `ctx.poll_id` | `str` | This poll's ID; it is on every log record of the poll. |
| `ctx.condition` | `bool` | The condition as this hook sees it: before the answer in `check`, after it in actions. |
| `ctx.payload` | JSON value or `None` | What `check` returned with its answer. Actions only. |
| `ctx.settings` | mapping | The watch's `[settings]` table. |
| `ctx.watch_dir` | `Path` | The watch directory, which is also the working directory. |
| `ctx.log` | `logging.Logger` | Logs into keepwatch's log; see `keepwatch docs logging`. |

## Exceptions

`keepwatch.Unknown(reason)` (raise in `check` for "cannot tell right now"), `keepwatch.CommandFailed` (raised by `ctx.run`), `keepwatch.LedgerReadOnly` (a ledger change inside `check`) and `keepwatch.LedgerCorrupt` (an unreadable ledger file) can be imported from `keepwatch`.
````

Replace `src/keepwatch/reference/cli.md` with:

````markdown
# Command-line reference

Global options come before the command: `keepwatch [--config PATH] COMMAND ...`. Every command accepts `-h`/`--help`. Exit status: 0 for success, 1 for problems found or a failed poll, 2 for wrong usage. When stdout is not a terminal, output is plain text with no colour or box drawing; `validate`, `poll`, `status` and `logs` also accept `--json`.
````

Append to `src/keepwatch/reference.py` (add `import inspect` and `from typing import Any` to its imports):

```python
_KIND_NAMES = {
    "str": "string",
    "bool": "boolean",
    "int": "integer ≥ 0",
    "duration": "duration",
    "interval": "duration ≥ 1s",
    "str_list": "list of strings",
    "path_list": "list of paths",
    "command": "command (string or list)",
}


def _default_text(key: Any) -> str:
    from keepwatch.durations import format_duration

    value = key.default
    if value is None:
        return "none"
    if key.kind in ("duration", "interval"):
        return f'`"{format_duration(value)}"`'
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, tuple):
        return "`[]`"
    if value == "":
        return '`""`'
    return f"`{value}`"


def _key_table(keys: Any, *, defaultable_column: bool) -> list[str]:
    header = "| Key | Type | Default | Meaning |"
    rule = "|---|---|---|---|"
    if defaultable_column:
        header = "| Key | Type | Default | In `[defaults]` | Meaning |"
        rule = "|---|---|---|---|---|"
    rows = [header, rule]
    for key in keys:
        cells = [f"`{key.name}`", _KIND_NAMES[key.kind], _default_text(key)]
        if defaultable_column:
            cells.append("yes" if key.defaultable else "no")
        cells.append(key.doc)
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def config_topic() -> str:
    from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, WATCH_KEYS, WATCH_TABLES

    lines = [narrative("config"), "", "## Watch config.toml", ""]
    lines += _key_table(WATCH_KEYS, defaultable_column=True)
    lines += ["", "Tables:", ""]
    lines += [f"- `[{name}]`: {doc}" for name, doc in WATCH_TABLES.items()]
    lines += ["", "## Global config.toml", ""]
    lines += _key_table(
        [key for key in GLOBAL_KEYS],
        defaultable_column=False,
    )
    lines += ["", "Tables:", ""]
    lines += [f"- `[{name}]`: {doc}" for name, doc in GLOBAL_TABLES.items()]
    lines += ["", "### `[log]`", ""]
    lines += _key_table(LOG_KEYS, defaultable_column=False)
    return "\n".join(lines)


def _signature(member: Any) -> str:
    signature = inspect.signature(member)
    parameters = list(signature.parameters.values())[1:]
    return str(signature.replace(parameters=parameters)).replace("'", "")


def _members(owner: type, prefix: str) -> list[str]:
    lines = []
    for name, member in inspect.getmembers(owner):
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            lines += [f"### `{prefix}{name}`", "", inspect.getdoc(member) or "", ""]
        elif inspect.isfunction(member):
            lines += [f"### `{prefix}{name}{_signature(member)}`", "", inspect.getdoc(member) or "", ""]
    return lines


def ctx_topic() -> str:
    from keepwatch.ctx import Ctx, Ledger

    lines = [narrative("ctx"), "", "## Methods and properties", ""]
    lines += _members(Ctx, "ctx.")
    lines += [
        "## Ledger",
        "",
        inspect.getdoc(Ledger) or "",
        "",
        "Membership and size: `key in ledger`, `len(ledger)`, and iteration (`for key in ledger`, sorted).",
        "",
    ]
    lines += _members(Ledger, "ledger.")
    return "\n".join(lines).rstrip()


def cli_topic() -> str:
    import click

    from keepwatch.cli import cli

    root = click.Context(cli, info_name="keepwatch")
    lines = [narrative("cli"), "", "## keepwatch (global options)", ""]
    lines += _options(cli)
    for name in cli.list_commands(root):
        command = cli.get_command(root, name)
        sub = click.Context(command, info_name=name, parent=root)
        usage = " ".join(command.collect_usage_pieces(sub))
        lines += [
            f"## keepwatch {name}",
            "",
            f"`keepwatch {name} {usage}`",
            "",
            inspect.cleandoc(command.help or ""),
            "",
        ]
        lines += _options(command)
    return "\n".join(lines).rstrip()


def _options(command: Any) -> list[str]:
    import click

    rows = []
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        names = ", ".join(f"`{opt}`" for opt in (*param.opts, *param.secondary_opts))
        if param.is_flag:
            kind = "flag"
        else:
            kind = param.type.name
            if param.multiple:
                kind += ", repeatable"
        unset = getattr(click.core, "UNSET", None)  # click >= 8.2 marks "no default" with a sentinel
        no_default = param.default in (None, False, ()) or (unset is not None and param.default is unset)
        default = "" if no_default else f"`{param.default}`"
        rows.append(f"| {names} | {kind} | {default} | {param.help or ''} |")
    if not rows:
        return []
    return ["| Option | Type | Default | Meaning |", "|---|---|---|---|", *rows, ""]


GENERATED.update({"config": config_topic, "ctx": ctx_topic, "cli": cli_topic})
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS. Also read `uv run keepwatch docs ctx | head -60` and `uv run keepwatch docs cli | head -60` once to see that the output reads well; report anything malformed.

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/reference.py src/keepwatch/reference tests/test_docs.py
git commit -m "Generate the config, ctx and cli reference topics from the code"
```

---

### Task 4: Example watches

**Files:**
- Create: `src/keepwatch/examples/psg-export/{config.toml,watch.py}`, `src/keepwatch/examples/site-down/{config.toml,notify.sh}`, `src/keepwatch/examples/greet-once/{config.toml,due.sh,greet.exp}`
- Replace: `src/keepwatch/reference/examples.md`
- Modify: `src/keepwatch/reference.py`
- Test: `tests/test_examples.py`

**Interfaces:**
- Produces: `reference.example_dirs() -> list[Path]`; `examples_topic()` registered in `GENERATED` (narrative plus every file of every example in a fenced block).

- [ ] **Step 1: Write the failing test** — `tests/test_examples.py`

```python
import json
import shutil

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.reference import example_dirs, render_topic


def run(*args):
    return CliRunner().invoke(cli, list(args))


def install(xdg, name):
    source = next(d for d in example_dirs() if d.name == name)
    target = xdg.default_watches_dir / name
    shutil.copytree(source, target)
    return target


def test_examples_are_in_the_docs():
    text = render_topic("examples")
    assert [d.name for d in example_dirs()] == ["greet-once", "psg-export", "site-down"]
    for directory in example_dirs():
        for path in directory.iterdir():
            if path.is_file():
                assert f"{directory.name}/{path.name}" in text


def test_examples_validate_and_poll(xdg):
    install(xdg, "psg-export")
    install(xdg, "site-down")
    assert run("validate", "psg-export", "site-down").exit_code == 0
    data = json.loads(run("poll", "psg-export", "--dry-run", "--json").output)
    assert data["polls"][0]["outcome"] == "false"
    data = json.loads(run("poll", "site-down", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]


@pytest.mark.skipif(shutil.which("expect") is None, reason="expect is not installed")
def test_expect_example_runs_once(xdg):
    install(xdg, "greet-once")
    assert run("validate", "greet-once").exit_code == 0
    first = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert first["outcome"] == "true" and first["results"][0]["status"] == "ok"
    second = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert second["outcome"] == "false"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_examples.py -q`
Expected: FAIL with `ImportError: cannot import name 'example_dirs'`

- [ ] **Step 3: Create the examples.**

`src/keepwatch/examples/psg-export/config.toml`:

```toml
# Send each finished PSG export tarball to bar.com, once.
description = "Send new PSG export tarballs to the incoming directory on bar.com"
interval = "30s"
action_timeout = "15m"   # big tarballs over a slow link
retry_after = "1h"       # once offline (say, the laptop is off the network), try again hourly

[settings]
pattern = "~/incoming/psg-export-*.tar.gz"
dest = "foo@bar.com:/home/foo/incoming/"
quiet = "30s"            # a file must be unchanged this long before it counts as finished
```

`src/keepwatch/examples/psg-export/watch.py`:

```python
"""Send each finished PSG export to bar.com once.

The condition is "there are finished exports that have not been sent". It stays TRUE while sends fail
and when new files arrive, so the work happens in on_true (every poll while TRUE), not on_rise.
"""

from keepwatch import Ctx


def check(ctx: Ctx):
    sent = ctx.ledger("sent")
    pending = [
        str(path)
        for path in ctx.glob(ctx.settings["pattern"])
        if ctx.unchanged_for(path, ctx.settings["quiet"]) and ctx.file_key(path) not in sent
    ]
    return bool(pending), pending


def on_true(ctx: Ctx) -> None:
    # No expire: the tarballs stay in ~/incoming, so a forgotten entry would be sent again.
    sent = ctx.ledger("sent")
    for path in ctx.payload:
        # BatchMode makes scp fail instead of prompting for a password or a host key.
        ctx.run(["scp", "-q", "-o", "BatchMode=yes", path, ctx.settings["dest"]])
        sent.add(ctx.file_key(path))  # only reached when scp succeeded
        ctx.log.info("sent %s", path, extra={"file": path})
```

`src/keepwatch/examples/site-down/config.toml`:

```toml
# Say when a web site stops answering, and when it is back. TRUE means "down".
description = "Tell me when https://example.com/ is down and when it recovers"
interval = "60s"
check_timeout = "20s"

[hooks]
check = 'curl --fail --silent --show-error --max-time 15 --output /dev/null "$KEEPWATCH_SETTING_URL"'
on_rise = ["./notify.sh", "is DOWN"]
on_fall = ["./notify.sh", "is back up"]

[check_exit_codes]
true = [22]           # curl --fail: the server answered with an HTTP error status
false = [0]           # the page loaded
unknown = [6, 7, 28]  # cannot resolve, cannot connect, timed out: maybe our own network is down

[settings]
url = "https://example.com/"
```

`src/keepwatch/examples/site-down/notify.sh`:

```sh
#!/bin/sh
# Report a change. Replace the echo with notify-send, mail or a chat webhook; output goes to the log.
set -eu
echo "$KEEPWATCH_SETTING_URL $1 (watch $KEEPWATCH_WATCH, poll $KEEPWATCH_POLL_ID)"
```

`src/keepwatch/examples/greet-once/config.toml`:

```toml
# A one-time task done through an interactive program, driven by Expect.
description = "Answer an interactive prompt once, then remember that it was done"
interval = "1h"
action_timeout = "1m"

[hooks]
check = ["./due.sh"]
on_true = ["expect", "./greet.exp"]

[settings]
name = "keepwatch"
```

`src/keepwatch/examples/greet-once/due.sh`:

```sh
#!/bin/sh
# TRUE (exit 0) until the greeting has been done; the action leaves a marker in the data directory.
if [ -e "$KEEPWATCH_DATA_DIR/greeted" ]; then
    exit 1
fi
exit 0
```

`src/keepwatch/examples/greet-once/greet.exp`:

```tcl
#!/usr/bin/env expect
# Expect runs the program on its own terminal, so prompts work although the hook's stdin is /dev/null.
set timeout 20
set name $env(KEEPWATCH_SETTING_NAME)
spawn sh -c {printf "name? "; read answer; echo "hello, $answer"}
expect {
    "name? " { send "$name\r" }
    timeout { exit 1 }
}
expect {
    "hello, $name" { }
    timeout { exit 1 }
}
expect eof
exec touch "$env(KEEPWATCH_DATA_DIR)/greeted"
```

Make `notify.sh`, `due.sh` and `greet.exp` executable (`chmod 755`).

Replace `src/keepwatch/reference/examples.md` with:

````markdown
# Example watches

Complete, tested watches. Every file of each example is printed in full below: recreate the files in a new directory under your watches directory (or start from `keepwatch new`), then edit `[settings]`.

- **psg-export** (Python): send every finished file matching a pattern to a server with scp, exactly once, using a ledger. Shows payloads, `ctx.run`, `unchanged_for`, `file_key` and why the work is in `on_true`.
- **site-down** (shell): watch a web site with curl. Shows `[check_exit_codes]` with `unknown` codes, TRUE meaning "something is wrong", and `on_rise`/`on_fall` notifications.
- **greet-once** (Expect): a one-time task through an interactive program. Shows Expect handling prompts, and a marker in `KEEPWATCH_DATA_DIR` that turns the condition FALSE once the work is done.
````

Append to `src/keepwatch/reference.py` (add `from pathlib import Path` to its imports):

```python
_LANGUAGES = {".toml": "toml", ".py": "python", ".sh": "sh", ".exp": "tcl"}


def example_dirs() -> list[Path]:
    root = Path(str(resources.files("keepwatch").joinpath("examples")))
    return sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(("_", ".")))


def examples_topic() -> str:
    lines = [narrative("examples")]
    for directory in example_dirs():
        lines += ["", f"## {directory.name}"]
        for path in sorted(directory.iterdir(), key=lambda p: (p.name != "config.toml", p.name)):
            if not path.is_file():
                continue
            language = _LANGUAGES.get(path.suffix, "")
            text = path.read_text(encoding="utf-8").rstrip("\n")
            lines += ["", f"### {directory.name}/{path.name}", "", f"```{language}", text, "```"]
    return "\n".join(lines)


GENERATED["examples"] = examples_topic
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/examples src/keepwatch/reference.py src/keepwatch/reference/examples.md tests/test_examples.py
git commit -m "Add tested example watches to the reference"
```

---

### Task 5: `AGENTS.md` and `CLAUDE.md` from `init`, and the README

**Files:**
- Modify: `src/keepwatch/templates.py`, `src/keepwatch/cli.py`, `README.md`
- Test: `tests/test_cli_init.py`

**Interfaces:**
- Produces: `templates.AGENTS_MD`, `templates.CLAUDE_MD = "@AGENTS.md\n"`; `keepwatch init` also writes `AGENTS.md` and `CLAUDE.md` into the default watches directory, never overwriting.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_cli_init.py`:

```python
def test_init_writes_agent_files_once(xdg):
    assert run("init").exit_code == 0
    agents = xdg.default_watches_dir / "AGENTS.md"
    claude = xdg.default_watches_dir / "CLAUDE.md"
    assert "keepwatch docs agent" in agents.read_text()
    assert claude.read_text() == "@AGENTS.md\n"
    agents.write_text("# mine\n")
    result = run("init")
    assert f"exists  {agents}" in result.output
    assert agents.read_text() == "# mine\n"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -q`
Expected: FAIL (`AGENTS.md` does not exist)

- [ ] **Step 3: Implement.** Append to `src/keepwatch/templates.py`:

```python
AGENTS_MD = """# keepwatch watches

Every subdirectory here is a keepwatch watch: a `config.toml` plus the code for its check and actions.

Before you create or change a watch, read the contract:

    keepwatch docs agent

`keepwatch docs --all` prints the complete reference.

Development loop: `keepwatch new <name>` → edit → `keepwatch validate <name>` →
`keepwatch poll <name> --fake true,false --dry-run` → `keepwatch poll <name> --dry-run` →
`keepwatch poll <name>` → `keepwatch logs --poll <id> -v`.
"""

CLAUDE_MD = "@AGENTS.md\n"
```

In `src/keepwatch/cli.py`, change the templates import to include `AGENTS_MD` and `CLAUDE_MD`, and in `init` replace the block that creates the watches directory with:

```python
    watches = app.paths.default_watches_dir
    if watches.is_dir():
        click.echo(f"exists  {watches}")
    else:
        watches.mkdir(parents=True)
        click.echo(f"created {watches}")
    for filename, content in (("AGENTS.md", AGENTS_MD), ("CLAUDE.md", CLAUDE_MD)):
        path = watches / filename
        if path.exists():
            click.echo(f"exists  {path}")
        else:
            path.write_text(content, encoding="utf-8")
            click.echo(f"created {path}")
```

and update its docstring's first line to: `"""Create the global config file, the default watches directory, and AGENTS.md/CLAUDE.md for agents.`

Replace `README.md` with:

````markdown
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
````

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/templates.py src/keepwatch/cli.py tests/test_cli_init.py README.md
git commit -m "init writes AGENTS.md and CLAUDE.md; rewrite the README"
```
