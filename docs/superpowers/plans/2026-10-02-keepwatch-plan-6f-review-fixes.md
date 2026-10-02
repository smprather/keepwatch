# keepwatch Plan 6f (Transfer, Recipe and Relay Review Fixes) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what Windows CI and three code reviews (Plans 6c, 6d, 6e) found before release 2026.10.3: the test key that Windows OpenSSH ignores, scp outliving a timed-out hook, transfers overrunning the hook deadline, checks transferring through `keepwatch kit`, endpoint parsing gaps, a Windows askpass launcher that breaks on non-ASCII user names, relay examples that never chain, a pull recipe that blocks on a same-named staged file, and smaller gaps.

**Architecture:** Mostly local edits. `transfer.py`: scp runs in the caller's process group on POSIX, `ScpOptions.deadline` caps every scp run, endpoints handle URIs, domain users, ports and colon names, the version cache keeps only successes, the askpass launcher lives in the hook's run dir when there is one and is written in the OEM code page on Windows. `cli.py`: a `Duration` parameter type; `kit` refuses checks and reports OSErrors cleanly. Recipes: shared staging in the examples, `on_conflict` for pull (default `rename`), config and validation fixes. `installcheck`: also checks the registered executable's own path, and matches the version exactly.

**Tech Stack:** as Plan 6e.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md`. Builds on Plans 6a-6e (implemented, up to `3e9b2d5`).

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check --no-cache src tests` without pipes before every commit, `git diff --stat` shows only the task's files, no formatter, do not push, portable tests unless marked, stop and report on plan defects, attribution trailer).
- `transfer.py`, `askpass.py` and `recipes/*.py` stay stdlib-only.
- Facts behind the fixes: Windows OpenSSH rejects a private key other accounts can read ("bad permissions", CI 2026-10-02); backslash escaping works in `scp://` URI paths in both protocols, percent-encoding fails with `-O` (spike 2026-10-02); POSIX exit statuses are 8-bit.

## Review Focus

- A hook killed at its deadline must not leave scp running. Test: Task 2 `test_scp_runs_in_the_callers_process_group`.
- The `.sha256` marker upload must not get more time than the hook has left. Test: Task 2 `test_a_past_deadline_stops_a_transfer_before_scp`.
- `keepwatch kit push` from a command check must be refused like `ctx.transfer.push`. Test: Task 3 `test_kit_refuses_to_transfer_in_a_check`.
- The two relay examples, copied as the guide says, must use one staging folder. Test: Task 4 `test_relay_examples_share_the_staging_folder`.
- A newer file with the name of one still staged must not block the pull recipe. Test: Task 4 `test_pull_recipe_renames_on_a_name_clash`.

---

### Task 1: Windows can use the test key

**Files:**
- Modify: `tests/scp_server.py`

- [ ] **Step 1: Fix the key's permissions** — in `tests/scp_server.py`, add `import getpass` and `import subprocess` to the imports and replace

```python
        if os.name != "nt":
            self.client_key.chmod(0o600)
```

with

```python
        if os.name == "nt":
            # Windows OpenSSH ignores a private key that other accounts can read ("bad permissions").
            subprocess.run(
                ["icacls", str(self.client_key), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                check=True,
                capture_output=True,
            )
        else:
            self.client_key.chmod(0o600)
```

- [ ] **Step 2: Run the tests** (Linux cannot exercise the Windows branch; the supervisor checks CI)

Run: `uv run pytest tests/test_transfer_scp.py tests/test_recipe_pull.py tests/test_recipe_push.py -q`
Expected: all pass.

- [ ] **Step 3: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the one file. Then:

```bash
git add tests/scp_server.py
git commit -m "Test scp server: a client key Windows OpenSSH accepts"
```

---

### Task 2: Transfers

**Files:**
- Modify: `src/keepwatch/transfer.py`, `src/keepwatch/ctx.py` (`LEDGER_NAME`, `ledger_file`)
- Modify: `tests/test_transfer.py`

**Interfaces:**
- Produces:
  - `ScpOptions.deadline: float | None = None` (epoch seconds); every scp run's time limit is `min(timeout, deadline - now)`; a deadline already past raises `TransferFailed("no time left …")` before scp starts. `Transfer` sets `deadline` from the hook's remaining time at each call (so `push`'s marker copy gets only what is left). `Transfer.tcp_open` caps `timeout` at the remaining time.
  - On POSIX scp runs in the caller's process group (no new session), so killing a hook's process group stops it.
  - `ScpOptions` rejects `ssh_options` given as a string or not starting with an option (`"-…"`).
  - `Endpoint.scp_arg()`: `scp://` paths are backslash-escaped too; a relative local path whose first component holds a `:` gets `./`; `parse_endpoint` accepts `DOMAIN\user@host:path`; `scp_argv` adds `-P` for a non-URI endpoint built with a `port` (when `options.port` is not set).
  - `openssh_version` caches only versions it could read (a failed `ssh -V` is retried next time).
  - `pull` raises `ValueError` for a `sha256` that is not 64 hex characters; on Windows `TransferFailed` for a name Windows cannot store (`<>:"|?*`); an existing identical file (by the given `sha256`) is a no-op for every `on_conflict`.
  - the askpass launcher is written into `$KEEPWATCH_RUN_DIR` when that directory exists (hooks have it), else the temp dir, and only when a password is used; on Windows in the OEM code page (falling back to UTF-8) with `%` doubled; `DISPLAY` is set (when unset) for OpenSSH older than 8.4, which uses askpass only then.
  - `transfer.known_hosts_option(path) -> list[str]` (the `-o UserKnownHostsFile="…"` pair)
  - `ctx.LEDGER_NAME` (the ledger-name regex) and `ctx.ledger_file(data_dir: Path, name: str) -> Path`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_transfer.py` (add `import sys`, `import time` to its imports, and `copy`, `known_hosts_option`, `pull` to the `keepwatch.transfer` import, keeping its names in alphabetical order (ruff checks it); add `from keepwatch import transfer`):

```python
def test_uri_paths_are_escaped():
    assert parse_endpoint("scp://h:2222/in/a b.gz").scp_arg() == "scp://h:2222/in/a\\ b.gz"


def test_local_names_with_a_colon_get_dot_slash():
    assert Endpoint(path=".run-12:00.txt.part").scp_arg() == "./.run-12:00.txt.part"
    assert Endpoint(path="C:/x/a.gz").scp_arg() == "C:/x/a.gz"
    assert Endpoint(path="/abs/a:b").scp_arg() == "/abs/a:b"


def test_domain_users():
    assert parse_endpoint("CORP\\me@winhost:C:/data/a.gz") == Endpoint(path="C:/data/a.gz", host="winhost", user="CORP\\me")
    assert parse_endpoint("a\\b:c") == Endpoint(path="a\\b:c")


def test_an_endpoint_port_becomes_dash_p():
    argv = scp_argv(Endpoint(path="in/a.gz", host="h", port=2222), parse_endpoint("a.gz"), ScpOptions(), (10, 5))
    assert argv[argv.index("-P") + 1] == "2222"


def test_known_hosts_option():
    assert known_hosts_option("C:/my keys/kh") == ["-o", 'UserKnownHostsFile="C:/my keys/kh"']


def test_ssh_options_must_be_a_list_of_arguments():
    with pytest.raises(ValueError, match="not a string"):
        ScpOptions(ssh_options="-oProxyJump=b")
    with pytest.raises(ValueError, match="must start with an option"):
        ScpOptions(ssh_options=["ProxyJump=b"])


def test_unknown_versions_are_not_cached(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise OSError("busy")
        return subprocess.CompletedProcess(argv, 0, "", "OpenSSH_10.5p1, OpenSSL 3")

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)
    assert transfer.openssh_version("fake-ssh-for-test") is None
    assert transfer.openssh_version("fake-ssh-for-test") == (10, 5)
    assert transfer.openssh_version("fake-ssh-for-test") == (10, 5)
    assert len(calls) == 2


def test_pull_rejects_a_malformed_sha256(tmp_path):
    with pytest.raises(ValueError, match="64 hex"):
        pull("me@h:a.gz", tmp_path, sha256="abc123")


def test_a_past_deadline_stops_a_transfer_before_scp(tmp_path):
    reports = []
    with pytest.raises(TransferFailed, match="no time left"):
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(deadline=time.time() - 1), report=lambda *a: reports.append(a))
    assert reports == []


@pytest.mark.posix_only
def test_scp_runs_in_the_callers_process_group(tmp_path):
    probe = tmp_path / "scp"
    probe.write_text(f"#!{sys.executable}\nimport os, sys\nsys.stderr.write(str(os.getpgid(0)))\nsys.exit(1)\n")
    probe.chmod(0o755)
    with pytest.raises(transfer.CommandFailed) as info:
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(scp_command=[str(probe)]))
    assert info.value.stderr.strip() == str(os.getpgid(0))


@pytest.mark.windows_only
def test_pull_refuses_names_windows_cannot_store(tmp_path):
    with pytest.raises(TransferFailed, match="Windows"):
        pull("me@h:/x/a:b.gz", tmp_path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -q`
Expected: FAIL — `cannot import name 'known_hosts_option'`.

- [ ] **Step 3: Implement**

(a) `src/keepwatch/ctx.py`: after `_LEDGER_NAME = re.compile(...)` add `LEDGER_NAME = _LEDGER_NAME  # public: config.py validates ledger names with it` and, after the `LedgerCorrupt` class, add:

```python
def ledger_file(data_dir: Path, name: str) -> Path:
    """Where ledger `name` of a watch is stored (data_dir is the watch's persistent data directory)."""
    return data_dir / "ledgers" / f"{name}.json"
```

and in `Ctx.ledger` replace `Ledger(self._data_dir / "ledgers" / f"{name}.json", …` with `Ledger(ledger_file(self._data_dir, name), …` (the rest of that line unchanged).

(b) `src/keepwatch/transfer.py`:

- Imports: add `import contextlib` and `import math`; remove `import functools`.
- Replace `Endpoint.scp_arg` with:

```python
    def scp_arg(self) -> str:
        if not self.remote:
            head = re.split(r"[\\/]", self.path, maxsplit=1)[0]
            if ":" in head and not _DRIVE.match(self.path):
                return "./" + self.path  # scp would read "name:with:colons" as host "name"
            return self.path
        who = f"{self.user}@{self.host}" if self.user else str(self.host)
        if self.uri:
            port = f":{self.port}" if self.port else ""
            return f"scp://{who}{port}/{escape_remote_path(self.path)}"
        return f"{who}:{escape_remote_path(self.path)}"
```

- In `parse_endpoint`, replace

```python
    before, colon, after = text.partition(":")
    if colon and before and "/" not in before and "\\" not in before:
        user, _, host = before.rpartition("@")
        return Endpoint(path=after, host=host, user=user or None)
```

with

```python
    before, colon, after = text.partition(":")
    user, _, host = before.rpartition("@")
    if colon and host and "/" not in before and "\\" not in host:  # DOMAIN\user@host:path is remote
        return Endpoint(path=after, host=host, user=user or None)
```

- Replace the `@functools.lru_cache(maxsize=8)` decorator and the whole `openssh_version` function with:

```python
_VERSIONS: dict[str, tuple[int, int]] = {}


def openssh_version(ssh: str = "ssh") -> tuple[int, int] | None:
    """The local OpenSSH version from `ssh -V` (cached per program once known), or None if it cannot be told."""
    if ssh in _VERSIONS:
        return _VERSIONS[ssh]
    try:
        completed = subprocess.run(
            [ssh, "-V"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=platform.NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # not cached: a busy or briefly missing ssh is asked again next time
    version = parse_openssh_version(completed.stderr + completed.stdout)
    if version is not None:
        _VERSIONS[ssh] = version
    return version
```

- `ScpOptions`: add the field `deadline: float | None = None` after `timeout`, and in `__post_init__`, before `object.__setattr__(self, "ssh_options", …)`, add:

```python
        if isinstance(self.ssh_options, str):
            raise ValueError("ssh_options must be a list of scp arguments, e.g. ['-o', 'ProxyJump=bastion'], not a string")
        if self.ssh_options and not str(self.ssh_options[0]).startswith("-"):
            raise ValueError(
                f"ssh_options are scp arguments and must start with an option, e.g. ['-o', {self.ssh_options[0]!r}]"
            )
```

- Add after `_shown`:

```python
def known_hosts_option(path: str | os.PathLike[str]) -> list[str]:
    """The ssh option that checks host keys against `path` (quoted: ssh splits option values at spaces)."""
    return ["-o", f'UserKnownHostsFile="{Path(path).as_posix()}"']
```

- In `scp_argv`, replace `argv += ["-o", f'UserKnownHostsFile="{Path(options.known_hosts).as_posix()}"']` with `argv += known_hosts_option(options.known_hosts)`, and replace

```python
    if options.port is not None:
        argv += ["-P", str(options.port)]
```

with

```python
    port = options.port
    if port is None:
        port = next((end.port for end in (source, destination) if end.remote and not end.uri and end.port), None)
    if port is not None:
        argv += ["-P", str(port)]
```

- Replace `write_askpass` with:

```python
def write_askpass(directory: Path, variable: str) -> Path:
    """Write the SSH_ASKPASS launcher: it runs `python -m keepwatch.askpass VARIABLE` (no secret in the file)."""
    if not _VARIABLE.fullmatch(variable):
        raise ValueError(f"not an environment variable name: {variable!r}")
    if platform.IS_WINDOWS:
        path = directory / "askpass.cmd"
        python = _askpass_python().replace("%", "%%")
        text = f'@echo off\r\n"{python}" -m keepwatch.askpass {variable} %*\r\n'
        try:
            data = text.encode("oem")  # cmd.exe reads batch files in the OEM code page (C:\Users\José\...)
        except (LookupError, UnicodeEncodeError):
            data = text.encode("utf-8")
        path.write_bytes(data)
        return path
    path = directory / "askpass.sh"
    path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(_askpass_python())} -m keepwatch.askpass {variable} "$@"\n', encoding="utf-8"
    )
    path.chmod(0o700)
    return path
```

- Replace `_run` with:

```python
def _run(argv: list[str], env: dict[str, str], timeout: float | None) -> tuple[int | None, str, str, bool]:
    try:
        if platform.IS_WINDOWS:
            process = platform.start_process(
                argv, cwd=None, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            popen, stop, close = process.popen, process.kill, process.close
        else:
            # The caller's process group: a hook killed at its deadline takes scp (and its ssh) with it.
            popen = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stop, close = popen.kill, (lambda: None)
    except OSError as exc:
        raise TransferFailed(f"cannot run {argv[0]}: {exc.strerror or exc} (is OpenSSH's scp installed?)") from exc
    timed_out = False
    try:
        try:
            out, err = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop()
            out, err = popen.communicate()
    finally:
        close()
    return popen.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out
```

- Replace `copy` with:

```python
def _askpass_dir() -> str | None:
    """Where the askpass launcher goes: the hook's run dir (often not noexec, unlike /tmp), else the temp dir."""
    run_dir = os.environ.get("KEEPWATCH_RUN_DIR")
    return run_dir if run_dir and os.path.isdir(run_dir) else None


def copy(
    source: str | os.PathLike[str] | Endpoint,
    destination: str | os.PathLike[str] | Endpoint,
    *,
    options: ScpOptions = ScpOptions(),
    report: Report | None = None,
) -> None:
    """Copy one file with scp. Raises CommandFailed when scp fails, TransferFailed when it cannot be attempted."""
    src, dst = _endpoint(source), _endpoint(destination)
    argv = scp_argv(src, dst, options, openssh_version(_ssh_beside(options.scp_command[0])))
    limit = options.timeout
    if options.deadline is not None:
        left = options.deadline - time.time()
        if left <= 0:
            raise TransferFailed(f"no time left for scp {src.scp_arg()} -> {dst.scp_arg()} (the hook's deadline has passed)")
        limit = left if limit is None else min(limit, left)
    env = dict(os.environ)
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        if options.password_env:
            if options.password_env not in os.environ:
                raise TransferFailed(
                    f"password_env names {options.password_env}, which is not set in keepwatch's environment "
                    "(on Windows: setx; for the service, restart it after setting the variable)"
                )
            work = stack.enter_context(tempfile.TemporaryDirectory(prefix="keepwatch-scp-", dir=_askpass_dir()))
            env["SSH_ASKPASS"] = str(write_askpass(Path(work), options.password_env))
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env.setdefault("DISPLAY", ":0")  # OpenSSH before 8.4 uses SSH_ASKPASS only when DISPLAY is set
        returncode, stdout, stderr, timed_out = _run(argv, env, limit)
    if report is not None:
        report(argv, returncode, timed_out, time.monotonic() - started, stdout, stderr)
    if timed_out:
        shown = format_duration(limit if limit is not None and not math.isinf(limit) else 0)
        raise TransferFailed(f"scp timed out after {shown}: {src.scp_arg()} -> {dst.scp_arg()}")
    if returncode != 0:
        raise CommandFailed(argv, returncode if returncode is not None else -1, stderr)
```

- In `pull`: after the `on_conflict` check add

```python
    if sha256 is not None and not re.fullmatch(r"[0-9A-Fa-f]{64}", sha256):
        raise ValueError(f"sha256 must be 64 hex characters, got {sha256!r}")
```

after the `if not name or name in (".", ".."):` check add

```python
    if platform.IS_WINDOWS and any(char in name for char in '<>:"|?*'):
        raise TransferFailed(f"{name!r} cannot be stored as a Windows file name (it holds one of < > : \" | ? *)")
```

and replace the block

```python
    if final.exists():
        if on_conflict == "skip-identical":
            if sha256 is not None and sha256_file(final) == sha256.lower():
                return final
            raise TransferFailed(
```

with

```python
    if final.exists():
        if sha256 is not None and sha256_file(final) == sha256.lower():
            return final  # an earlier pull of this exact file finished
        if on_conflict == "skip-identical":
            raise TransferFailed(
```

and update the docstring's first line to `"""Copy a remote file into local_dir through .NAME.part, verify size and sha256 when given, rename it (an identical existing file is a no-op)."""`.

- In `Transfer._options`, replace

```python
        timeout = options.pop("timeout", None)
        if self._remaining is not None:
            left = self._remaining()
            if left <= 0:
                raise TransferFailed("no time left in this hook for a transfer (raise action_timeout)")
            timeout = left if timeout is None else min(timeout, left)
        return ScpOptions(timeout=timeout, **options)
```

with

```python
        deadline = None
        if self._remaining is not None:
            left = self._remaining()
            if left <= 0:
                raise TransferFailed("no time left in this hook for a transfer (raise action_timeout)")
            deadline = time.time() + left  # each scp run (the file, then its marker) gets only what is left
        return ScpOptions(deadline=deadline, **options)
```

- Replace the body of `Transfer.tcp_open` with:

```python
        """True when host:port accepts a TCP connection within `timeout` seconds (allowed in a check)."""
        if self._remaining is not None:
            timeout = max(min(timeout, self._remaining()), 0.1)
        return tcp_open(host, port, timeout)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py tests/test_transfer_scp.py tests/test_ctx_transfer.py tests/test_cli_kit.py tests/test_ctx.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the three files. Then:

```bash
git add src/keepwatch/transfer.py src/keepwatch/ctx.py tests/test_transfer.py
git commit -m "Transfers: scp dies with its hook, deadline per scp run, endpoint and askpass fixes"
```

---

### Task 3: `keepwatch kit` and durations on the command line

**Files:**
- Modify: `src/keepwatch/cli.py`
- Modify: `tests/test_cli_kit.py`

**Interfaces:**
- Produces: `cli.DURATION` (a click `ParamType` named `duration`, parsing `"30s"`, `"5m"`, … into seconds) used by `kit … --timeout`, `kit tcp-open --timeout` and `observe --for`; `kit copy/pull/push` exit 1 with "a check must not transfer files" when `KEEPWATCH_HOOK=check`; OSErrors from transfers exit 1 with one line, no traceback.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli_kit.py`:

```python
def test_kit_refuses_to_transfer_in_a_check(xdg, tmp_path):
    result = CliRunner().invoke(cli, ["kit", "copy", str(tmp_path / "a.gz"), "me@h:"], env={"KEEPWATCH_HOOK": "check"})
    assert result.exit_code == 1
    assert "a check must not transfer files" in result.output


def test_kit_reports_os_errors_without_a_traceback(xdg, tmp_path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    result = run("kit", "pull", "me@h:a.gz", str(not_a_dir / "sub"), "--sha256", "0" * 64)  # mkdir fails
    assert result.exit_code == 1
    assert "Traceback" not in result.output and str(not_a_dir) in result.output


def test_kit_rejects_a_malformed_sha256(xdg, tmp_path):
    assert run("kit", "pull", "me@h:a.gz", str(tmp_path), "--sha256", "abc").exit_code == 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_kit.py -q`
Expected: FAIL — the check is not refused (scp is attempted), a traceback for the OSError.

- [ ] **Step 3: Implement** — in `src/keepwatch/cli.py`:

(a) Add after the `App` dataclass:

```python
class DurationType(click.ParamType):
    """A duration such as 30s, 5m, 1h30m or a number of seconds."""

    name = "duration"

    def convert(self, value, param, ctx):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return parse_duration(value)
        except DurationError as exc:
            self.fail(str(exc), param, ctx)


DURATION = DurationType()
```

(b) `observe`: change its `--for` option to `click.option("--for", "duration", type=DURATION, metavar="DURATION", help=…)` (same help), and replace the block

```python
    seconds = None
    if duration is not None:
        try:
            seconds = parse_duration(duration)
        except DurationError as exc:
            raise click.BadParameter(str(exc), param_hint="--for") from None
```

with `seconds = duration`; change its annotation to `duration: float | None`.

(c) `_scp_options`: change the `--timeout` option to `click.option("--timeout", type=DURATION, metavar="DURATION", help='Give up after this long, e.g. "10m". Default: no limit (a command hook\'s own timeout still stops it).')`. Change `_scp`'s last parameter from `timeout_text` to `timeout` and delete its `parse_duration` block (keep `timeout` as given).

(d) `kit_tcp_open`: change `--timeout` to `click.option("--timeout", type=DURATION, default="5s", show_default=True, metavar="DURATION", help="How long to wait for the connection.")`, the parameter to `timeout: float`, and delete its `parse_duration` block.

(e) Replace `_kit_run` with:

```python
def _kit_run(action, *, transfers: bool = True) -> None:
    if transfers and os.environ.get("KEEPWATCH_HOOK") == "check":
        _fail("a check must not transfer files; do it in an action (keepwatch kit tcp-open is fine in a check)")
    try:
        result = action()
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    except (CommandFailed, transfer.TransferFailed) as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc))
    if result is not None:
        click.echo(str(result))
```

(`kit_copy`, `kit_pull` and `kit_push` keep calling `_kit_run(lambda: …)`; `tcp-open` does not use `_kit_run`.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_kit.py tests/test_cli_observe.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the two files. Then:

```bash
git add src/keepwatch/cli.py tests/test_cli_kit.py
git commit -m "keepwatch kit: no transfers in checks, clean OSErrors; a duration parameter type"
```

---

### Task 4: Recipes, examples, validation, install checks

**Files:**
- Modify: `src/keepwatch/config.py`, `src/keepwatch/recipes/pull.py`, `src/keepwatch/recipes/push.py`, `src/keepwatch/observers.py`, `src/keepwatch/validation.py`, `src/keepwatch/installcheck.py`, `src/keepwatch/cli.py` (`_install_problem`)
- Modify: `src/keepwatch/examples/relay-pull/config.toml`, `src/keepwatch/examples/relay-push/config.toml`
- Modify: `tests/test_examples.py`, `tests/test_config_recipe.py`, `tests/test_recipe_pull.py`, `tests/test_installcheck.py`

**Interfaces:**
- Produces: pull setting `on_conflict` (`rename` default, `overwrite`, `skip-identical`); config errors for a malformed `dest` and for `archive_dir` equal to `local_dir`; `validate` does not require `ssh` on PATH when `ssh_command` is set; `installcheck.verify_command` matches the version as a whole word; `_install_problem` also checks the registered program's own path for transience; the relay examples share `~/relay/staging`.

- [ ] **Step 1: Write the failing tests**

(a) `tests/test_examples.py`: add `from pathlib import Path` and `from keepwatch.config import load_watch_config` to the imports; in `test_relay_push_example_waits_when_linux2_is_away` replace `staging = xdg.default_watches_dir / "relay-push" / "staging"` with `staging = Path.home() / "relay" / "staging"`; append:

```python
def test_relay_examples_share_the_staging_folder(xdg):
    pulled = load_watch_config(install(xdg, "relay-pull")).settings["local_dir"]
    pushed = load_watch_config(install(xdg, "relay-push")).settings["local_dir"]
    assert pulled == pushed == str(Path.home() / "relay" / "staging")
```

(b) `tests/test_config_recipe.py`, append:

```python
def test_a_malformed_dest_is_a_config_error(make_watch):
    [problem] = problems(make_watch, 'recipe = "push"\n[settings]\nlocal_dir = "s"\ndest = "scp://host"\n')
    assert "'dest':" in problem and "scp://[user@]host[:port]/path" in problem


def test_archive_dir_must_differ_from_local_dir(make_watch):
    [problem] = problems(make_watch, PUSH + 'after = "archive"\narchive_dir = "staging"\n')
    assert "'archive_dir' must not be 'local_dir'" in problem

```

and also append:

```python
def test_pull_on_conflict_default_and_choices(make_watch):
    assert load(make_watch, PULL).settings["on_conflict"] == "rename"


def test_pull_on_conflict_must_be_a_choice(make_watch):
    [problem] = problems(make_watch, PULL + 'on_conflict = "keep"\n')
    assert "'on_conflict' must be one of skip-identical, rename, overwrite" in problem
```

(c) `tests/test_recipe_pull.py`, append:

```python
def test_pull_recipe_renames_on_a_name_clash(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"new data")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "a.tar.gz").write_bytes(b"old, not pushed yet")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"new data")])
    assert not report.failed
    assert (stage / "a-1.tar.gz").read_bytes() == b"new data"
    assert (stage / "a.tar.gz").read_bytes() == b"old, not pushed yet"
```

(d) `tests/test_installcheck.py`, append:

```python
def test_the_version_must_match_as_a_whole_word():
    import os

    longer = [sys.executable, "-c", f"print('keepwatch, version {__version__}1')"]
    assert installcheck.verify_command(longer, os.environ) is not None


def test_install_checks_the_registered_program_too(xdg, monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    program = cache / "env" / "bin" / "keepwatch"
    program.parent.mkdir(parents=True)
    monkeypatch.setattr(installcheck, "uv_cache_dirs", lambda env: [cache])
    monkeypatch.setattr(installcheck, "service_check_argv", lambda: [str(program), "--version"])
    result = run("install", "--dry-run")
    assert result.exit_code == 1 and "uv's cache" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_examples.py tests/test_config_recipe.py tests/test_recipe_pull.py tests/test_installcheck.py -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement**

(a) Examples: in both `relay-pull/config.toml` and `relay-push/config.toml`, replace the `local_dir = "staging"…` line with:

```toml
local_dir = "~/relay/staging"   # the staging folder: the SAME path in relay-pull and relay-push
```

(b) `src/keepwatch/config.py`:
- change `from keepwatch.transfer import MARKERS, PROTOCOLS, parse_endpoint` to `from keepwatch.transfer import CONFLICTS, MARKERS, PROTOCOLS, known_hosts_option, parse_endpoint` and add `from keepwatch.ctx import LEDGER_NAME` right after `from keepwatch import platform` (ruff keeps imports sorted); replace the definition `_LEDGER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")` with `_LEDGER = LEDGER_NAME`.
- `RECIPE_SETTINGS["pull"]`: add after the `checksum` key:

```python
        Key(
            "on_conflict",
            "str",
            "rename",
            "When local_dir already holds a different file of that name (a newer version arrived before the old "
            "one was pushed): `rename` the new one to NAME-1.ext, `overwrite` the old one, or `skip-identical` (fail).",
        ),
```

- `_RECIPE_CHOICES`: add `"on_conflict": CONFLICTS,`.
- In `_recipe_settings`, replace everything from `    if recipe == "push":` down to (not including) the final `return {name: list(value) …}` line with:

```python
    for name in _RECIPE_PATHS:
        if isinstance(values.get(name), str) and values[name]:
            values[name] = str(_observed_path(values[name], watch_dir))
    if recipe == "push":
        try:
            dest_remote = not values["dest"] or parse_endpoint(values["dest"]).remote
        except ValueError as exc:
            collector.add(f"'dest': {exc}", key="dest", table="settings", topic="recipes")
            dest_remote = True
        if not dest_remote:
            collector.add(
                f"'dest' must be a remote directory (user@host:dir or scp://user@host/dir), got {values['dest']!r}",
                key="dest",
                table="settings",
                topic="recipes",
            )
        if values["after"] == "archive" and not values["archive_dir"]:
            collector.add("after = \"archive\" needs 'archive_dir' in [settings]", key="after", table="settings", topic="recipes")
        elif values["after"] == "archive" and values["local_dir"] and Path(values["archive_dir"]) == Path(values["local_dir"]):
            collector.add(
                "'archive_dir' must not be 'local_dir': archived files would be pushed again and again",
                key="archive_dir",
                table="settings",
                topic="recipes",
            )
```

- In `_recipe_observers`, replace `ssh_options += ["-o", f'UserKnownHostsFile="{Path(settings["known_hosts"]).as_posix()}"']` with `ssh_options += known_hosts_option(settings["known_hosts"])`.

(c) `src/keepwatch/recipes/pull.py`, `on_true`: add `on_conflict=settings["on_conflict"],` to the `ctx.transfer.pull(...)` call (after `sha256=sha256,`).

(d) `src/keepwatch/recipes/push.py`, `check`: replace

```python
    files = [str(path) for path in settled_files(settings) if pushed is None or ctx.file_key(path) not in pushed]
```

with

```python
    files = []
    for path in settled_files(settings):
        try:
            if pushed is None or ctx.file_key(path) not in pushed:
                files.append(str(path))
        except OSError:
            continue  # removed since the scan
```

(e) `src/keepwatch/observers.py`: change `from keepwatch.ctx import Ledger, LedgerCorrupt` to `from keepwatch.ctx import Ledger, LedgerCorrupt, ledger_file` and in `RemoteFilesObserver.__init__` replace `data_dir / "ledgers" / f"{config.skip_ledger}.json"` with `ledger_file(data_dir, config.skip_ledger)`.

(f) `src/keepwatch/validation.py`: replace `for program in ("ssh", "scp") if watch.recipe == "pull" else ("scp",):` with

```python
        needs_ssh = watch.recipe == "pull" and not watch.settings.get("ssh_command")
        for program in ("ssh", "scp") if needs_ssh else ("scp",):
```

(g) `src/keepwatch/installcheck.py`: add `import re`; in `verify_command` replace `if completed.returncode == 0 and __version__ in completed.stdout:` with

```python
    if completed.returncode == 0 and re.search(rf"(?<![\w.]){re.escape(__version__)}(?![\w.])", completed.stdout):
```

(h) `src/keepwatch/cli.py`, `_install_problem`: replace

```python
        reason = installcheck.transient_reason(
            Path(sys.prefix), cache_dirs=installcheck.uv_cache_dirs(os.environ), temp_dir=Path(tempfile.gettempdir())
        )
        if reason is not None:
```

with

```python
        caches, temp = installcheck.uv_cache_dirs(os.environ), Path(tempfile.gettempdir())
        program = Path(installcheck.service_check_argv()[0])
        reason = None
        for candidate in (Path(sys.prefix), program):  # the program the service will run may live elsewhere
            reason = reason or installcheck.transient_reason(candidate, cache_dirs=caches, temp_dir=temp)
        if reason is not None:
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_examples.py tests/test_config_recipe.py tests/test_recipe_pull.py tests/test_recipe_push.py tests/test_installcheck.py tests/test_cli_install.py tests/test_remote_files_observer.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the thirteen files. Then:

```bash
git add src/keepwatch/config.py src/keepwatch/recipes/pull.py src/keepwatch/recipes/push.py src/keepwatch/observers.py src/keepwatch/validation.py src/keepwatch/installcheck.py src/keepwatch/cli.py src/keepwatch/examples/relay-pull/config.toml src/keepwatch/examples/relay-push/config.toml tests/test_examples.py tests/test_config_recipe.py tests/test_recipe_pull.py tests/test_installcheck.py
git commit -m "Recipes and relay: shared staging, pull on_conflict, config and install-check fixes"
```

---

### Task 5: Documentation

**Files:**
- Modify: `src/keepwatch/reference/transfers.md`, `src/keepwatch/reference/observers.md`, `src/keepwatch/reference/recipes.md`, `src/keepwatch/reference/relay.md`
- Modify: `tests/test_docs.py`

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`: append `"icacls"`, `"TimeoutExpired"`, `"ssh_options"` to `KEY_FACTS["transfers"]`; append `"expire"` to `KEY_FACTS["observers"]`; append `"on_conflict"` to `KEY_FACTS["recipes"]`; append `"the same path"` to `KEY_FACTS["relay"]`.

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — those facts are missing.

- [ ] **Step 2: Edit the docs**

(a) `transfers.md`:
- In the Options table, replace the `ssh_options` row's meaning with: ``Extra scp arguments, placed before keepwatch's own (ssh keeps the first value given): a list such as `["-o", "ProxyJump=bastion"]`, never a string. (`keepwatch kit --ssh-option ProxyJump=bastion` adds the `-o` itself.)``
- In the Options table, replace the `identity` row's meaning with: ``A private key file (`ssh -i`); ssh then offers only that key. On Windows the file must be readable by you alone, or OpenSSH ignores it ("bad permissions"): `icacls KEY /inheritance:r /grant:r "%USERNAME%:F"` (keys made by ssh-keygen in `~\.ssh` already are).``
- In the Options table, replace the `timeout` row's meaning with: ``Give up after this many seconds. In `ctx.transfer` each scp run also stops at the hook's deadline (so a push's marker never gets more than what is left). Running out of time raises `TransferFailed`, not `subprocess.TimeoutExpired` (unlike `ctx.run`).``
- After the `pull` paragraph ("`pull` when `DIR/NAME` already exists: …"), add: ``Two transfers must not pull the same name into the same folder at the same time (they share `.NAME.part`).``

(b) `observers.md`, in the `remote_files` section, add a bullet after "**What it reports:**":

```markdown
- **Skipping what is done:** with `skip_ledger = "<name>"` (the `pull` recipe sets `"pulled"`), files whose `path|size|mtime` key is in that ledger are neither hashed nor reported after a reconnect. The ledger is read without any `expire`, so use one without expiry for this.
```

(c) `recipes.md`, in the pull section, add a bullet after the "**on_true**" bullet:

```markdown
- When `local_dir` already holds a different file of the same name (a newer version arrived while the old one still waits to be pushed), `on_conflict` decides: `rename` (default: the new one becomes NAME-1.ext), `overwrite`, or `skip-identical` (fail). An identical file (same sha256) is never copied again.
```

(d) `relay.md`, step 6: replace "set `remote`, `remote_dir`, `dest` and the same staging folder in both (`local_dir`; an absolute path such as `'C:/relay/staging'` when they live in different directories)" with "set `remote`, `remote_dir` and `dest`, and give both the same path in `local_dir` (the examples use `~/relay/staging`; a relative path would be relative to each watch's own directory, and the two would never meet)".

- [ ] **Step 3: Run the docs tests**

Run: `uv run pytest tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 4: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check --no-cache src tests` (no pipes); `git diff --stat` shows only the five files. Then:

```bash
git add src/keepwatch/reference/transfers.md src/keepwatch/reference/observers.md src/keepwatch/reference/recipes.md src/keepwatch/reference/relay.md tests/test_docs.py
git commit -m "Docs: key permissions on Windows, timeouts, ssh_options, skip ledgers, pull conflicts, shared staging"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, `git status --short` (only `M .gitignore`), and anything in this plan you had to question. Do not push.
