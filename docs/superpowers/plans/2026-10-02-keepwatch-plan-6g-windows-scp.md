# keepwatch Plan 6g (Windows scp, Final Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make key-authenticated transfers and file names with spaces work on Windows, and fix the last review findings before release 2026.10.3.

**Architecture:** Remote paths are written for scp per protocol, direction and the hub's OS (`transfer.remote_path_arg`); `Endpoint.scp_arg()` becomes the plain display form; `scp://` endpoints become `user@host:path` plus `-P`. scp gets SIGTERM before SIGKILL on a timeout (scp then stops its own ssh). `pull` in rename mode reuses an identical numbered copy. Small cleanups.

**Tech Stack:** as Plan 6f.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 4). Builds on Plans 6a-6f (implemented, up to `8ec0f4b`).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests unless marked, stop and report on plan defects, attribution trailer).
- Facts from Windows CI (OpenSSH_for_Windows 9.5p2, 2026-10-02):
  - a private key with an explicit `OWNER RIGHTS` (S-1-3-4) entry is rejected ("bad permissions"); pytest's temp dirs give files that entry;
  - Windows scp turns `\` in remote paths into `/`, so backslash escaping breaks there;
  - classic protocol (`-O`): a **pull** works only with `?` in place of special characters (quotes fail scp's own "filename does not match request" check); a **push** works with the remote name in single or double quotes;
  - SFTP: the plain name works for pull and push, and must not be escaped or quoted.
- On Linux (OpenSSH 10.5p1 against a real sshd): classic protocol works with backslash escaping, SFTP with the plain name.

## Review Focus

- A file name with a space must pull and push from a Windows hub. Tests: Task 2 `test_remote_path_arg`, and the existing `test_pull_a_name_with_a_space` / `test_push_a_name_with_a_space` on Windows CI.
- A timed-out scp must not hang on its ssh child. Test: Task 2 `test_a_timeout_stops_scp_and_its_ssh_child`.
- Retrying a renamed pull must not create a second copy. Test: Task 2 `test_a_retried_rename_reuses_the_identical_copy`.

---

### Task 1: The Windows test key, really

**Files:**
- Modify: `tests/scp_server.py`

- [ ] **Step 1: Fix it** — in `tests/scp_server.py`, replace

```python
            subprocess.run(
                ["icacls", str(self.client_key), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                check=True,
                capture_output=True,
            )
```

with

```python
            # pytest's temp dirs also give files an explicit OWNER RIGHTS entry, which OpenSSH rejects too.
            for change in (["/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"], ["/remove:g", "*S-1-3-4"]):
                subprocess.run(["icacls", str(self.client_key), *change], check=True, capture_output=True)
```

- [ ] **Step 2: Full suite, lint, commit** (the Windows effect is checked on CI by the supervisor)

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the one file. Then:

```bash
git add tests/scp_server.py
git commit -m "Test scp server: remove the OWNER RIGHTS entry Windows OpenSSH rejects"
```

---

### Task 2: Transfers

**Files:**
- Modify: `src/keepwatch/transfer.py`
- Modify: `tests/test_transfer.py`, `tests/test_transfer_scp.py`

**Interfaces:**
- Produces:
  - `transfer.remote_path_arg(path: str, *, classic: bool, source: bool, windows: bool = platform.IS_WINDOWS) -> str`
  - `Endpoint.scp_arg()` returns the plain form (`user@host:path`, `scp://…`, local path), used in messages and results, never escaped; the scp operands are built by `scp_argv`
  - `scp_argv` writes `scp://` endpoints as `user@host:path` with `-P port`; two remote endpoints on different ports raise `TransferFailed`
  - `_free_name(directory, name, same)` returns an existing numbered copy for which `same(path)` is true, else the first free name; `pull` compares sizes before hashing
  - on a timeout scp gets SIGTERM, then SIGKILL after 10s

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_transfer.py`: add `remote_path_arg` to the `keepwatch.transfer` import (alphabetical order). Replace the test `test_local_names_with_a_colon_get_dot_slash` (scp_arg is now the plain form) with:

```python
def test_local_names_with_a_colon_get_dot_slash():
    def last(local):
        return scp_argv(parse_endpoint("me@h:a.gz"), Endpoint(path=local), ScpOptions(), (10, 5))[-1]

    assert last(".run-12:00.txt.part") == "./.run-12:00.txt.part"
    assert last("C:/x/a.gz") == "C:/x/a.gz"
    assert last("/abs/a:b") == "/abs/a:b"
```

Replace the two tests `test_remote_paths_are_escaped_in_scp_arguments` and `test_uri_paths_are_escaped` with:

```python
def test_scp_arg_is_the_plain_form():
    assert parse_endpoint("me@h:/out/a b.gz").scp_arg() == "me@h:/out/a b.gz"
    assert parse_endpoint("/local/a b.gz").scp_arg() == "/local/a b.gz"
    assert parse_endpoint("me@h:in/").child("a b.gz").scp_arg() == "me@h:in/a b.gz"
    assert parse_endpoint("scp://h:2222/in/a b.gz").scp_arg() == "scp://h:2222/in/a b.gz"


def test_remote_path_arg():
    # SFTP: the plain name, on every OS.
    assert remote_path_arg("/out/a b.gz", classic=False, source=True, windows=False) == "/out/a b.gz"
    assert remote_path_arg("/out/a b.gz", classic=False, source=False, windows=True) == "/out/a b.gz"
    # Classic protocol on POSIX: backslash escapes, both directions.
    assert remote_path_arg("/out/a b.gz", classic=True, source=True, windows=False) == "/out/a\\ b.gz"
    assert remote_path_arg("~/in/c d.txt", classic=True, source=False, windows=False) == "~/in/c\\ d.txt"
    # Classic protocol on Windows: '?' for a pull, quotes for a push.
    assert remote_path_arg("/out/a b.gz", classic=True, source=True, windows=True) == "/out/a?b.gz"
    assert remote_path_arg("~/in/c d.txt", classic=True, source=False, windows=True) == "~/'in/c d.txt'"
    assert remote_path_arg("in/it's.txt", classic=True, source=False, windows=True) == '"in/it\'s.txt"'
    assert remote_path_arg("in/plain.txt", classic=True, source=False, windows=True) == "in/plain.txt"
    with pytest.raises(TransferFailed, match="Windows"):
        remote_path_arg("in/it's \"x\".txt", classic=True, source=False, windows=True)


def test_uri_endpoints_become_dash_p():
    argv = argv_for("scp://me@h:2222/in/a.gz", "a.gz")
    assert argv[-2:] == ["me@h:in/a.gz", "a.gz"] and argv[argv.index("-P") + 1] == "2222"
    with pytest.raises(TransferFailed, match="different ports"):
        argv_for("scp://a:2222/x", "scp://b:2223/y", protocol="sftp")


@pytest.mark.posix_only
def test_a_timeout_stops_scp_and_its_ssh_child(tmp_path):
    fake = tmp_path / "scp"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])  # holds our stderr\n"
        "signal.signal(signal.SIGTERM, lambda *args: (child.kill(), sys.exit(1)))  # what scp does for its ssh\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(TransferFailed, match="timed out"):
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(scp_command=[str(fake)], timeout=1))
    assert time.monotonic() - started < 20
```

(b) `tests/test_transfer_scp.py`: in `test_push_a_name_with_a_space` change the expected return value to `"u@127.0.0.1:c d.txt"`, and append:

```python
def test_a_retried_rename_reuses_the_identical_copy(tmp_path, server, ssh_config):
    (server.root / "b.tar.gz").write_bytes(b"new")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "b.tar.gz").write_bytes(b"old")
    options = key_options(server, ssh_config)
    first = pull(REMOTE + "b.tar.gz", stage, size=3, sha256=sha(b"new"), on_conflict="rename", options=options)
    again = pull(REMOTE + "b.tar.gz", stage, size=3, sha256=sha(b"new"), on_conflict="rename", options=options)
    assert first == again == stage / "b-1.tar.gz"
    assert sorted(path.name for path in stage.iterdir()) == ["b-1.tar.gz", "b.tar.gz"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py -q`
Expected: FAIL — `cannot import name 'remote_path_arg'`.

- [ ] **Step 3: Implement** — in `src/keepwatch/transfer.py`:

- Remove `import math`.
- Replace `Endpoint.scp_arg` with:

```python
    def scp_arg(self) -> str:
        """The endpoint as people write it (in messages and results). scp_argv builds scp's own operands."""
        if not self.remote:
            return self.path
        who = f"{self.user}@{self.host}" if self.user else str(self.host)
        if self.uri:
            port = f":{self.port}" if self.port else ""
            return f"scp://{who}{port}/{self.path}"
        return f"{who}:{self.path}"
```

- Add after `escape_remote_path`:

```python
def remote_path_arg(path: str, *, classic: bool, source: bool, windows: bool = platform.IS_WINDOWS) -> str:
    """A remote path as scp needs it on the command line.

    SFTP takes the plain path. The classic protocol hands it to the remote shell: on POSIX backslash escapes
    work both ways; Windows' scp turns backslashes into slashes, so a pull uses `?` for each special character
    (scp's own name check accepts glob matches; size and sha256 checks catch a wrong match) and a push quotes.
    """
    if not classic:
        return path
    if not windows:
        return escape_remote_path(path)
    prefix = "~/" if path.startswith("~/") else ""
    rest = path[len(prefix) :]
    if all(_SAFE.fullmatch(char) for char in rest):
        return path
    if source:
        return prefix + "".join(char if _SAFE.fullmatch(char) else "?" for char in rest)
    if "'" not in rest:
        return f"{prefix}'{rest}'"
    if not any(char in rest for char in '"$`\\'):
        return f'{prefix}"{rest}"'
    raise TransferFailed(
        f"cannot name {path!r} for scp's classic protocol on Windows (it holds both kinds of quotes or $ ` \\); "
        'rename the file or use protocol = "sftp"'
    )


def _local_arg(path: str) -> str:
    head = re.split(r"[\\/]", path, maxsplit=1)[0]
    if ":" in head and not _DRIVE.match(path):
        return "./" + path  # scp would read "name:with:colons" as host "name"
    return path


def _operand(end: Endpoint, *, classic: bool, source: bool) -> str:
    if not end.remote:
        return _local_arg(end.path)
    who = f"{end.user}@{end.host}" if end.user else str(end.host)
    return f"{who}:{remote_path_arg(end.path, classic=classic, source=source)}"
```

- In `scp_argv`, replace

```python
    port = options.port
    if port is None:
        port = next((end.port for end in (source, destination) if end.remote and not end.uri and end.port), None)
    if port is not None:
        argv += ["-P", str(port)]
```

with

```python
    port = options.port
    if port is None:
        ports = {end.port for end in (source, destination) if end.remote and end.port}
        if len(ports) > 1:
            raise TransferFailed(f"the two hosts use different ports ({', '.join(map(str, sorted(ports)))}); scp takes one -P")
        port = ports.pop() if ports else None
    if port is not None:
        argv += ["-P", str(port)]
```

and replace the last line `return [*argv, "--", source.scp_arg(), destination.scp_arg()]` with:

```python
    classic = options.protocol == "scp"
    return [*argv, "--", _operand(source, classic=classic, source=True), _operand(destination, classic=classic, source=False)]
```

- In `_run`: in the Windows branch change `popen, stop, close = process.popen, process.kill, process.close` to `popen, terminate, stop, close = process.popen, process.kill, process.kill, process.close`; in the POSIX branch change `stop, close = popen.kill, (lambda: None)` to `terminate, stop, close = popen.terminate, popen.kill, (lambda: None)`; and replace

```python
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            out, err = popen.communicate()
```

with

```python
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate()  # scp stops its ssh child on SIGTERM; a SIGKILL would leave ssh holding our pipes
            try:
                out, err = popen.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stop()
                try:
                    out, err = popen.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    out, err = b"", b""
```

- In `copy`, replace `shown = format_duration(limit if limit is not None and not math.isinf(limit) else 0)` with `shown = format_duration(limit or 0)`.
- Replace `_free_name` with:

```python
def _free_name(directory: Path, name: str, same: Callable[[Path], bool]) -> Path:
    """name-1.ext, name-2.ext, …: an existing one for which same() holds, else the first free one (a.tar.gz -> a-1.tar.gz)."""
    head, dot, tail = name.partition(".") if not name.startswith(".") else (name, "", "")
    for number in itertools.count(1):
        candidate = directory / (f"{head}-{number}.{tail}" if dot else f"{head}-{number}")
        if not candidate.exists() or same(candidate):
            return candidate
    raise AssertionError("unreachable")


def _identical(path: Path, size: int | None, sha256: str | None) -> bool:
    """Whether path already holds the file with this size and sha256 (sizes first: no hashing when they differ)."""
    if sha256 is None:
        return False
    try:
        if size is not None and path.stat().st_size != size:
            return False
        return sha256_file(path) == sha256.lower()
    except OSError:
        return False
```

- In `pull`, replace

```python
    if final.exists():
        if sha256 is not None and sha256_file(final) == sha256.lower():
            return final  # an earlier pull of this exact file finished
```

with

```python
    same = functools.partial(_identical, size=size, sha256=sha256)
    if final.exists():
        if same(final):
            return final  # an earlier pull of this exact file finished
```

and `final = _free_name(directory, name)` with:

```python
            final = _free_name(directory, name, same)
            if final.exists():
                return final  # a numbered copy of this exact file is already there
```

and add `import functools` back to the imports (alphabetical order).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_ctx_transfer.py tests/test_cli_kit.py tests/test_recipe_pull.py tests/test_recipe_push.py tests/test_relay_e2e.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/transfer.py tests/test_transfer.py tests/test_transfer_scp.py
git commit -m "Transfers: remote paths per protocol and OS, scp:// as -P, gentle timeouts, no duplicate renamed copies"
```

---

### Task 3: Cleanups

**Files:**
- Modify: `src/keepwatch/ctx.py`, `src/keepwatch/config.py`, `src/keepwatch/cli.py`, `src/keepwatch/installcheck.py`
- Modify: `tests/test_config_recipe.py`, `tests/test_installcheck.py`

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_config_recipe.py`, append:

```python
def test_recipe_ssh_options_must_start_with_an_option(make_watch):
    [problem] = problems(make_watch, PUSH + 'ssh_options = ["ProxyJump=bastion"]\n')
    assert "'ssh_options' are scp arguments" in problem and "['-o', 'ProxyJump=bastion']" in problem
```

(b) `tests/test_installcheck.py`, in `test_install_checks_the_registered_program_too`, add after the existing assertion:

```python
    assert "the program the login service would run" in result.output
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config_recipe.py tests/test_installcheck.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/ctx.py`: replace the two lines

```python
_LEDGER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
LEDGER_NAME = _LEDGER_NAME  # public: config.py validates ledger names with it
```

with `LEDGER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")`, and change `_LEDGER_NAME.fullmatch(name)` in `Ctx.ledger` to `LEDGER_NAME.fullmatch(name)`.

(b) `src/keepwatch/config.py`: delete the line `_LEDGER = LEDGER_NAME` and change `_LEDGER.fullmatch(converted)` to `LEDGER_NAME.fullmatch(converted)`. In `_recipe_settings`, right before `values[name] = converted` (the last statement of the settings loop), add:

```python
        if name == "ssh_options" and converted and not converted[0].startswith("-"):
            collector.add(
                f"'ssh_options' are scp arguments and must start with an option, e.g. ['-o', {converted[0]!r}]",
                key=name,
                table="settings",
                topic="recipes",
            )
            continue
```

(c) `src/keepwatch/installcheck.py`: change `transient_reason`'s signature to `def transient_reason(prefix: Path, *, cache_dirs: Sequence[Path], temp_dir: Path, subject: str = "this keepwatch") -> str | None:` and its two messages to `f"{subject} runs from uv's cache ({prefix}): uvx and \`uv run --with\` environments are pruned later"` and `f"{subject} runs from the temporary directory ({prefix})"`.

(d) `src/keepwatch/cli.py`:
- `_kit_run`: change the signature to `def _kit_run(action) -> None:` and its first line to `if os.environ.get("KEEPWATCH_HOOK") == "check":`.
- Replace the body of `_install_problem` with:

```python
    argv = installcheck.service_check_argv()
    if not force:
        caches, temp = installcheck.uv_cache_dirs(os.environ), Path(tempfile.gettempdir())
        reason = installcheck.transient_reason(Path(sys.prefix), cache_dirs=caches, temp_dir=temp)
        reason = reason or installcheck.transient_reason(
            Path(argv[0]), cache_dirs=caches, temp_dir=temp, subject="the program the login service would run"
        )
        if reason is not None:
            return (
                f"{reason}, and the login service would stop working when it disappears. Install keepwatch for good "
                "with `uv tool install keepwatch` and run `keepwatch install` from there (or pass --force)."
            )
    problem = installcheck.verify_command(argv, os.environ)
    if problem is not None:
        return f"the login service would run a command that does not work: {problem}"
    return None
```

(keep its docstring line).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_recipe.py tests/test_installcheck.py tests/test_cli_install.py tests/test_cli_kit.py tests/test_ctx.py tests/test_config_observe.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the six files. Then:

```bash
git add src/keepwatch/ctx.py src/keepwatch/config.py src/keepwatch/cli.py src/keepwatch/installcheck.py tests/test_config_recipe.py tests/test_installcheck.py
git commit -m "Validate recipe ssh_options; one ledger-name regex; clearer install-check messages"
```

---

### Task 4: Docs and spec

**Files:**
- Modify: `src/keepwatch/reference/transfers.md`, `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md`
- Modify: `tests/test_docs.py`

- [ ] **Step 1: Write the failing test** — append `"OWNER RIGHTS"` and `"Windows' scp"` to `KEY_FACTS["transfers"]` in `tests/test_docs.py`.

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL.

- [ ] **Step 2: Edit**

(a) `transfers.md`: in the Options table's `identity` row, after the `icacls` command add: ``If ssh then says "Try removing permissions for user: OWNER RIGHTS", also run `icacls KEY /remove:g *S-1-3-4`.``

In the "Errors" section, replace the sentence ``File names with spaces or shell characters are fine: keepwatch backslash-escapes remote paths.`` with:

```markdown
File names with spaces or shell characters work: keepwatch writes remote paths the way each scp needs them (backslash escapes with the classic protocol on Linux; on Windows, where Windows' scp turns backslashes into slashes, `?` wildcards for pulls and quotes for pushes; plain names with SFTP). A name holding both kinds of quotes cannot be pushed with the classic protocol from Windows; use `protocol = "sftp"` or rename it.
```

(b) Spec, section 4: replace the bullet beginning ``- **Remote paths are backslash-escaped**`` (through `backslash escapes work in both.`) with:

```
- **Remote paths are written per protocol, direction and OS** (spikes and Windows CI, 2026-10-02): SFTP takes
  the plain path. Classic protocol on POSIX: backslash escapes (single quotes fail scp's "filename does not
  match request" check on pulls). Classic protocol on Windows, whose scp turns `\` into `/`: `?` for special
  characters in pulls (scp's name check accepts glob matches; size/sha256 verification catches a wrong
  match) and quotes in pushes. `scp://` endpoints are passed as `user@host:path` with `-P` (scp URL-decodes
  URI paths, and percent-encoding fails with `-O`).
```

- [ ] **Step 3: Run the docs tests**

Run: `uv run pytest tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/reference/transfers.md docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md tests/test_docs.py
git commit -m "Docs: Windows key permissions and remote file names"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
