# keepwatch Plan 6b (Remote Watcher) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `remote_files` observer kind: one long-lived outbound ssh session runs keepwatch's own remote watcher (a single Python 3.6-compatible file sent over ssh's stdin, nothing installed remotely), which reports settled files in a remote directory as events.

**Architecture:** `src/keepwatch/remote_watcher.py` is standalone (stdlib only, Python 3.6+, ASCII): it scans one directory (immediately on inotify through `ctypes` where available, else every `interval`), reports each settled file once with its sha256, and prints heartbeats. On the keepwatch side, `RemoteFilesObserver` is a `CommandObserver` whose command is `ssh … -- user@host <python> -u -` and whose stdin is the watcher's source with its options prepended as a Python assignment (so no argument ever passes through the remote login shell's quoting). Its `hello` line becomes an `observer.connected` record; any stdout line that is not a file event (login-script noise) is logged, not delivered.

**Tech Stack:** as Plan 6a; GitHub Actions gains a `python:3.6-slim` container job.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (sections 2.1 `remote_files`, 3, 8). Builds on Plan 6a (`docs/superpowers/plans/2026-10-01-keepwatch-plan-6a-observers.md`), which must be fully implemented first: this plan edits code that plan created.

## Global Constraints

- All Global Constraints of Plan 6a apply (branch `keepwatch-observers`, explicit `git add`, `uv run pytest -q` and `uv run ruff check src tests` without pipes before every commit, do not push, portable tests, stop and report on plan defects, attribution trailer).
- `src/keepwatch/remote_watcher.py` and `tests/remote_watcher_selftest.py` must run on **Python 3.6** with only the standard library: no dataclasses, no `:=`, no `subprocess` `capture_output=`/`text=`, no `from __future__ import annotations`, no `time.time_ns`/`monotonic_ns`, no `datetime.fromisoformat`, no `str.removeprefix`/`removesuffix`, no `shlex.join`, no `functools.cached_property`, no `breakpoint()`, no `asyncio.run`, no `math.prod`. `remote_watcher.py` must be pure ASCII. f-strings are allowed (3.6 has them) but the watcher code below does not need them.
- The supervisor verifies the Python 3.6 job on CI after you finish; locally the main suite checks syntax with `ast.parse(..., feature_version=(3, 7))` and a forbidden-word list.

## Review Focus

- A login shell that prints a banner on stdout (common in `.cshrc`/`.bashrc` on EDA hosts) must not turn into events. Test: Task 3 `test_login_script_noise_is_logged_not_delivered`.
- A remote host whose locale is `C` (Python 3.6 then uses ASCII for file names) must still report UTF-8 file names correctly. Test: Task 1 `test_utf8_names_under_the_c_locale`.
- A file being copied with its old modification time preserved must not be reported (or hashed into an event) before it stops growing. Test: Task 1 `test_growing_file_with_old_mtime_waits`.
- When keepwatch goes away (ssh killed), the remote watcher must exit by itself, quietly. Test: Task 1 `test_exits_quietly_when_stdout_closes`.
- An unreachable host must be retried with backoff and its ssh error shown. Test: Task 3 `test_connection_failure_is_retried`.

---

### Task 1: The remote watcher

**Files:**
- Create: `src/keepwatch/remote_watcher.py`
- Create: `tests/remote_watcher_selftest.py` (unittest, Python 3.6-compatible; not collected by pytest on its own: its name does not start with `test_`)
- Create: `tests/test_remote_watcher.py` (pytest: imports the selftest case so it runs in the main suite, plus 3.6 syntax checks)
- Modify: `.github/workflows/ci.yml` (Python 3.6 job)

**Interfaces:**
- Produces:
  - `remote_watcher.main(argv: list) -> int` — options from `argv[0]` (a JSON object) or, when argv is empty, the module global `KEEPWATCH_REMOTE_ARGS` (a JSON string)
  - options (all but `dir` optional): `dir`, `pattern` (`"*"`), `ignore` (`[".*", "*.tmp", "*.part", "*~"]`), `settle` (10), `interval` (2), `checksum` (true), `heartbeat` (30); unknown options are an error
  - stdout lines: `{"event": "hello", "version": 1, "python", "inotify", "dir"}`, then `{"event": "file", "path", "name", "size", "mtime", "sha256"?}` and `{"event": "heartbeat"}`
  - exit status 0 when stdout closes (or Ctrl-C), 2 for bad options, a missing directory or a scan error (message on stderr)

- [ ] **Step 1: Write the failing tests** — create `tests/remote_watcher_selftest.py`:

```python
"""Tests for keepwatch's remote watcher. Python 3.6+ and the standard library only.

CI runs this file directly under Python 3.6 (`python tests/remote_watcher_selftest.py -v`); the main suite
imports WatcherTests (tests/test_remote_watcher.py), so it also runs on every supported Python and OS.
"""

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHER = os.path.normpath(os.path.join(HERE, os.pardir, "src", "keepwatch", "remote_watcher.py"))
FAST = {"settle": 1, "interval": 0.2, "heartbeat": 30}


def age(path, seconds=60):
    old = time.time() - seconds
    os.utime(path, (old, old))


class Running(object):
    """A watcher subprocess whose stdout lines are parsed as they arrive."""

    def __init__(self, options, stdin_mode=False, env=None):
        if stdin_mode:
            with open(WATCHER, "rb") as handle:
                source = handle.read()
            prefix = "KEEPWATCH_REMOTE_ARGS = " + json.dumps(json.dumps(options)) + "\n"
            argv = [sys.executable, "-u", "-"]
            data = prefix.encode("ascii") + source
        else:
            argv = [sys.executable, "-u", WATCHER, json.dumps(options)]
            data = b""
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.process.stdin.write(data)
        self.process.stdin.close()
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read)
        self.reader.daemon = True
        self.reader.start()

    def _read(self):
        for raw in iter(self.process.stdout.readline, b""):
            self.lines.put(json.loads(raw.decode("ascii")))
        self.lines.put(None)

    def next(self, timeout=15):
        return self.lines.get(timeout=timeout)

    def next_file(self, timeout=15):
        deadline = time.time() + timeout
        while True:
            message = self.next(max(deadline - time.time(), 0.01))
            if message is None or message.get("event") == "file":
                return message

    def file_names_for(self, seconds):
        names = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                message = self.next(0.5)
            except queue.Empty:
                continue
            if message is None:
                break
            if message.get("event") == "file":
                names.append(message["name"])
        return names

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(10)
        self.reader.join(5)
        self.process.stdout.close()
        self.process.stderr.close()


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.running = []

    def tearDown(self):
        for item in self.running:
            item.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, stdin_mode=False, env=None, **options):
        merged = dict(FAST, dir=self.dir)
        merged.update(options)
        item = Running(merged, stdin_mode=stdin_mode, env=env)
        self.running.append(item)
        return item

    def write(self, name, data=b"x", old=True):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        if old:
            age(path)
        return path

    def run_once(self, options):
        process = subprocess.Popen(
            [sys.executable, WATCHER, json.dumps(options)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        out, err = process.communicate(timeout=15)
        return process.returncode, out, err

    def test_hello_comes_first(self):
        hello = self.start().next()
        self.assertEqual(hello["event"], "hello")
        self.assertEqual(hello["version"], 1)
        self.assertEqual(hello["python"].split(".")[0], "3")
        self.assertIsInstance(hello["inotify"], bool)
        if sys.platform.startswith("linux"):
            self.assertTrue(hello["inotify"])
        self.assertEqual(os.path.normcase(hello["dir"]), os.path.normcase(os.path.abspath(self.dir)))

    def test_existing_file_is_reported_with_its_checksum(self):
        path = self.write("a.tar.gz", b"hello")
        event = self.start().next_file()
        self.assertEqual(event["name"], "a.tar.gz")
        self.assertEqual(event["size"], 5)
        self.assertEqual(os.path.normcase(event["path"]), os.path.normcase(os.path.abspath(path)))
        self.assertEqual(event["sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertIsInstance(event["mtime"], float)

    def test_new_file_is_reported_after_it_settles(self):
        running = self.start()
        self.assertEqual(running.next()["event"], "hello")
        self.write("late.gz", b"yy", old=False)
        written = time.time()
        event = running.next_file()
        self.assertEqual(event["name"], "late.gz")
        self.assertGreaterEqual(time.time() - written, 0.9)

    def test_each_file_is_reported_once(self):
        self.write("a.gz")
        running = self.start(heartbeat=0.5)
        self.assertEqual(running.next_file()["name"], "a.gz")
        self.assertEqual(running.file_names_for(2.0), [])

    def test_heartbeats_when_idle(self):
        running = self.start(heartbeat=0.5)
        self.assertEqual(running.next()["event"], "hello")
        self.assertEqual(running.next(5), {"event": "heartbeat"})

    def test_ignored_names_are_never_reported(self):
        for name in ("a.gz", ".hidden", "b.tmp", "c.part", "d~"):
            self.write(name)
        running = self.start(heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["a.gz"])

    def test_pattern(self):
        for name in ("a.gz", "b.txt"):
            self.write(name)
        running = self.start(pattern="*.gz", heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["a.gz"])

    def test_no_checksum(self):
        self.write("a.gz")
        self.assertNotIn("sha256", self.start(checksum=False).next_file())

    def test_growing_file_with_old_mtime_waits(self):
        path = self.write("copy.gz", b"x")  # an old mtime, as cp -p leaves it
        running = self.start(settle=2, heartbeat=0.5)
        self.assertEqual(running.next()["event"], "hello")
        time.sleep(0.5)
        with open(path, "ab") as handle:
            handle.write(b"y")
        age(path)
        event = running.next_file()
        self.assertEqual(event["size"], 2)
        self.assertEqual(event["sha256"], hashlib.sha256(b"xy").hexdigest())

    def test_options_prepended_to_the_source_on_stdin(self):
        self.write("a.gz")
        running = self.start(stdin_mode=True)
        self.assertEqual(running.next()["event"], "hello")
        self.assertEqual(running.next_file()["name"], "a.gz")

    def test_exits_quietly_when_stdout_closes(self):
        options = dict(FAST, dir=self.dir, heartbeat=0.2)
        process = subprocess.Popen(
            [sys.executable, "-u", WATCHER, json.dumps(options)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(json.loads(process.stdout.readline().decode("ascii"))["event"], "hello")
        process.stdout.close()
        self.assertEqual(process.wait(15), 0)
        self.assertEqual(process.stderr.read(), b"")
        process.stderr.close()

    def test_missing_directory(self):
        code, out, err = self.run_once({"dir": os.path.join(self.dir, "missing")})
        self.assertEqual(code, 2)
        self.assertIn(b"does not exist", err)

    def test_unknown_option(self):
        code, out, err = self.run_once({"dir": self.dir, "colour": 1})
        self.assertEqual(code, 2)
        self.assertIn(b"unknown option(s): colour", err)

    @unittest.skipIf(os.name == "nt", "locale variables are a POSIX matter")
    def test_utf8_names_under_the_c_locale(self):
        name = u"été.gz"
        path = os.path.join(self.dir.encode("utf-8"), name.encode("utf-8"))
        with open(path, "wb") as handle:
            handle.write(b"x")
        age(path)
        env = dict(os.environ, LC_ALL="C", LANG="C")
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        event = self.start(env=env).next_file()
        self.assertEqual(event["name"], name)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_remote_watcher.py`:

```python
import ast
from pathlib import Path

from remote_watcher_selftest import WatcherTests  # noqa: F401  (pytest collects it from this module)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "keepwatch" / "remote_watcher.py"
SELFTEST = Path(__file__).resolve().with_name("remote_watcher_selftest.py")
FORBIDDEN = (
    "dataclass",
    "capture_output",
    "text=True",
    "__future__",
    "time.time_ns(",  # st_mtime_ns (3.3+) is fine
    "time.monotonic_ns(",
    "fromisoformat",
    "removeprefix",
    "removesuffix",
    "shlex.join",
    "cached_property",
    "breakpoint(",
    "asyncio.run",
    "math.prod",
    ":=",
)


def test_the_watcher_is_ascii():
    SOURCE.read_text(encoding="ascii")


def test_the_watcher_and_its_tests_parse_as_old_python():
    # (3, 7) is the oldest grammar newer Pythons can check; 3.7 added no syntax over 3.6. CI also runs 3.6 itself.
    for path in (SOURCE, SELFTEST):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 7))


def test_the_watcher_and_its_tests_avoid_newer_features():
    for path in (SOURCE, SELFTEST):
        text = path.read_text(encoding="utf-8")
        assert [word for word in FORBIDDEN if word in text] == [], path.name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_remote_watcher.py -q`
Expected: FAIL — `test_the_watcher_is_ascii` with `FileNotFoundError`, and the WatcherTests fail because the watcher file does not exist.

- [ ] **Step 3: Implement** — create `src/keepwatch/remote_watcher.py`:

```python
"""keepwatch's remote watcher: reports settled files in one directory as JSON lines on stdout.

keepwatch sends this file over ssh to the remote host's Python and runs it there; nothing is installed.
It must stay compatible with Python 3.6, use only the standard library and stay ASCII (the list of newer
features to avoid is in tests/test_remote_watcher.py).

Options are one JSON object: the first command-line argument, or KEEPWATCH_REMOTE_ARGS when keepwatch
prepends that assignment to the source it sends on stdin (so nothing passes through the remote login
shell's quoting rules).

Output, one JSON object per line, flushed at once:
  {"event": "hello", "version", "python", "inotify", "dir"}     once, at start
  {"event": "file", "path", "name", "size", "mtime", "sha256"}  once per settled file (sha256 with checksum)
  {"event": "heartbeat"}                                         after `heartbeat` seconds without output
Exit status: 0 when stdout is closed (keepwatch went away), 2 for bad options, a missing directory or an
error while scanning (the message goes to stderr).
"""

import errno
import fnmatch
import hashlib
import json
import os
import platform
import select
import stat
import sys
import time

WATCHER_VERSION = 1
DEFAULTS = {
    "dir": None,
    "pattern": "*",
    "ignore": [".*", "*.tmp", "*.part", "*~"],
    "settle": 10.0,
    "interval": 2.0,
    "checksum": True,
    "heartbeat": 30.0,
}
SETTLE_STEP = 1.0  # rescan this often while a file is settling
MIN_RESCAN = 0.5  # at most two scans a second, however many notifications arrive
CHUNK = 1024 * 1024
# IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
IN_MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200


class Closed(Exception):
    """stdout was closed: keepwatch is gone."""


class Output(object):
    """JSON lines on stdout; remembers when it last wrote, for heartbeats."""

    def __init__(self, stream):
        self.stream = stream
        self.last = time.monotonic()

    def send(self, message):
        try:
            self.stream.write(json.dumps(message, sort_keys=True) + "\n")
            self.stream.flush()
        except BrokenPipeError:
            raise Closed()
        except ValueError:  # write to a closed file
            raise Closed()
        except OSError as exc:
            if exc.errno in (errno.EPIPE, errno.EINVAL):
                raise Closed()
            raise
        self.last = time.monotonic()

    def heartbeat_if_due(self, every):
        if time.monotonic() - self.last >= every:
            self.send({"event": "heartbeat"})


def open_inotify(directory):
    """An inotify file descriptor watching `directory` (bytes), or None where inotify is unavailable."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        init = libc.inotify_init
        add = libc.inotify_add_watch
    except (ImportError, OSError, AttributeError):
        return None
    add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    fd = init()
    if fd < 0:
        return None
    if add(fd, directory, IN_MASK) < 0:
        os.close(fd)
        return None
    return fd


def wanted(name, options):
    if not fnmatch.fnmatch(name, options["pattern"]):
        return False
    for pattern in options["ignore"]:
        if fnmatch.fnmatch(name, pattern):
            return False
    return True


def sha256_of(path, output, heartbeat):
    """The file's sha256, read in chunks; heartbeats keep flowing while a large file is hashed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            output.heartbeat_if_due(heartbeat)
    return digest.hexdigest()


def decode(raw):
    # File names are bytes on POSIX; decode as UTF-8 whatever the locale (Python 3.6 under LANG=C would use ASCII).
    return raw.decode("utf-8", "surrogateescape")


class Watcher(object):
    def __init__(self, options, output):
        self.options = options
        self.output = output
        directory = os.path.abspath(os.path.expanduser(options["dir"]))
        self.directory = directory.encode("utf-8", "surrogateescape")
        self.candidates = {}  # path -> ((size, mtime_ns), when that state was first seen)
        self.reported = set()  # (path, size, mtime_ns)

    def scan(self):
        """Report settled files. Returns True while some file is still settling. Raises OSError if the directory is gone."""
        settle = self.options["settle"]
        names = os.listdir(self.directory)
        now = time.monotonic()
        wall = time.time()
        present = set()
        current = set()
        for raw in sorted(names):
            name = decode(raw)
            if not wanted(name, self.options):
                continue
            path = os.path.join(self.directory, raw)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            key = (info.st_size, info.st_mtime_ns)
            present.add(path)
            current.add((path,) + key)
            if (path,) + key in self.reported:
                continue
            first = self.candidates.get(path)
            if first is None or first[0] != key:
                first = (key, now)
                self.candidates[path] = first
            if now - first[1] < settle or wall - info.st_mtime < settle:
                continue
            event = {"event": "file", "path": decode(path), "name": name, "size": info.st_size, "mtime": info.st_mtime}
            if self.options["checksum"]:
                try:
                    event["sha256"] = sha256_of(path, self.output, self.options["heartbeat"])
                    after = os.stat(path)
                except OSError:
                    continue
                if (after.st_size, after.st_mtime_ns) != key:
                    del self.candidates[path]  # changed while it was hashed: let it settle again
                    continue
            del self.candidates[path]
            self.reported.add((path,) + key)
            self.output.send(event)
        for path in list(self.candidates):
            if path not in present:
                del self.candidates[path]
        self.reported &= current
        return bool(self.candidates)

    def run(self):
        inotify = open_inotify(self.directory)
        self.output.send(
            {
                "event": "hello",
                "version": WATCHER_VERSION,
                "python": platform.python_version(),
                "inotify": inotify is not None,
                "dir": decode(self.directory),
            }
        )
        interval = self.options["interval"]
        heartbeat = self.options["heartbeat"]
        last_scan = 0.0
        while True:
            pause = last_scan + MIN_RESCAN - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            last_scan = time.monotonic()
            settling = self.scan()
            wait = min(interval, SETTLE_STEP) if settling else interval
            wait = max(min(wait, heartbeat - (time.monotonic() - self.output.last)), 0.0)
            self.wait(inotify, wait)
            self.output.heartbeat_if_due(heartbeat)

    @staticmethod
    def wait(inotify, seconds):
        if inotify is None:
            time.sleep(seconds)
            return
        ready, _, _ = select.select([inotify], [], [], seconds)
        if ready:
            try:
                os.read(inotify, 65536)
            except OSError:
                pass


def parse_options(text):
    given = json.loads(text)
    if not isinstance(given, dict):
        raise ValueError("options must be a JSON object")
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(unknown))
    options = dict(DEFAULTS)
    options.update(given)
    if not options["dir"]:
        raise ValueError("option 'dir' is required")
    return options


def silence_stdout():
    """Point stdout at the null device, so Python's exit-time flush does not complain about the closed pipe."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def fail(message):
    sys.stderr.write("keepwatch remote watcher on " + platform.node() + ": " + message + "\n")
    sys.stderr.flush()
    return 2


def main(argv):
    raw = argv[0] if argv else globals().get("KEEPWATCH_REMOTE_ARGS", "{}")
    try:
        options = parse_options(raw)
    except ValueError as exc:
        return fail(str(exc))
    watcher = Watcher(options, Output(sys.stdout))
    if not os.path.isdir(watcher.directory):
        return fail("directory " + decode(watcher.directory) + " does not exist")
    try:
        watcher.run()
    except Closed:
        silence_stdout()
        return 0
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        return fail(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Add the CI job** — in `.github/workflows/ci.yml`, add a second job under `jobs:` (same indentation as `test:`):

```yaml
  remote-watcher-python-3-6:
    runs-on: ubuntu-latest
    container: python:3.6-slim
    steps:
      - uses: actions/checkout@v4
      - run: python -m py_compile src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py
      - run: python tests/remote_watcher_selftest.py -v
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_remote_watcher.py -q` and `uv run python tests/remote_watcher_selftest.py -v`
Expected: all pass (about 20 s each).

- [ ] **Step 6: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/remote_watcher.py tests/remote_watcher_selftest.py tests/test_remote_watcher.py .github/workflows/ci.yml
git commit -m "Remote watcher: Python 3.6-compatible, sent over ssh, reports settled files"
```

---

### Task 2: `kind = "remote_files"` configuration

**Files:**
- Modify: `src/keepwatch/config.py` (code added by Plan 6a Task 1)
- Modify: `tests/test_config_observe.py` (two assertions from Plan 6a change; new tests)

**Interfaces:**
- Consumes: Plan 6a's `OBSERVER_KINDS`, `OBSERVER_KEYS`, `_OBSERVER_KIND_KEYS`, `_OBSERVER_REQUIRED`, `ObserverConfig`, `_observer`, `_observed_path`.
- Produces:
  - `OBSERVER_KINDS = ("command", "files", "remote_files")`
  - `ObserverConfig` gains: `remote: str | None = None`, `dir: str | None = None`, `interval: float = 2.0`, `checksum: bool = True`, `heartbeat: float = 30.0`, `remote_python: str = "auto"`, `port: int | None = None`, `identity: Path | None = None`, `ssh_options: tuple[str, ...] = ()`, `ssh_command: tuple[str, ...] | None = None`
  - `remote_files` keys: `remote`, `dir` (both required), `pattern`, `ignore`, `settle`, `interval`, `checksum`, `heartbeat`, `heartbeat_timeout`, `remote_python`, `port`, `identity`, `ssh_options`, `ssh_command`
  - a key used by another kind is reported as `'<key>' only applies to kind = "a" or "b"; this observer is kind = "c"`

- [ ] **Step 1: Write the failing tests** — in `tests/test_config_observe.py`:

(a) In `test_missing_kind`, change the first assertion to:

```python
    assert "[observe.x] needs kind = one of command, files, remote_files; got missing" in problem
```

(b) In `test_key_of_the_other_kind`, change the first assertion to:

```python
    assert "'pattern' only applies to kind = \"files\" or \"remote_files\"; this observer is kind = \"command\"" in problem
```

(c) Append these tests:

```python
REMOTE = "[observe.r]\nkind = 'remote_files'\nremote = 'me@linux1'\ndir = '/data/out'\n"


def test_remote_files_observer(make_watch):
    config = load(
        make_watch,
        REMOTE + "pattern = '*.tar.gz'\nport = 2222\nidentity = '~/.ssh/id_relay'\n"
        "ssh_options = ['-o', 'ProxyJump=bastion']\n",
    )
    observer = config.observers["r"]
    assert (observer.kind, observer.remote, observer.dir, observer.pattern, observer.port) == (
        "remote_files",
        "me@linux1",
        "/data/out",
        "*.tar.gz",
        2222,
    )
    assert observer.identity == Path.home() / ".ssh" / "id_relay"
    assert observer.ssh_options == ("-o", "ProxyJump=bastion")
    assert (observer.interval, observer.checksum, observer.heartbeat) == (2.0, True, 30.0)
    assert (observer.remote_python, observer.ssh_command, observer.heartbeat_timeout) == ("auto", None, None)
    assert (observer.settle, observer.ignore) == (10.0, DEFAULT_IGNORE)


def test_remote_dir_is_not_expanded_locally(make_watch):
    config = load(make_watch, "[observe.r]\nkind = 'remote_files'\nremote = 'h'\ndir = '~/out'\n")
    assert config.observers["r"].dir == "~/out"


def test_remote_files_needs_remote_and_dir(make_watch):
    found = problems(make_watch, "[observe.r]\nkind = 'remote_files'\n")
    assert len(found) == 2
    assert any("needs 'remote'" in problem for problem in found)
    assert any("needs 'dir'" in problem for problem in found)


def test_remote_files_rejects_local_keys(make_watch):
    [problem] = problems(make_watch, REMOTE + "path = 'x'\n")
    assert "'path' only applies to kind = \"files\"; this observer is kind = \"remote_files\"" in problem


def test_remote_files_port_range(make_watch):
    [problem] = problems(make_watch, REMOTE + "port = 70000\n")
    assert "'port' must be between 1 and 65535, got 70000" in problem


def test_remote_files_empty_values(make_watch):
    found = problems(make_watch, "[observe.r]\nkind = 'remote_files'\nremote = ''\ndir = ''\nremote_python = ''\n")
    assert len(found) == 3 and all("must not be empty" in problem for problem in found)


def test_remote_files_ssh_command(make_watch):
    config = load(make_watch, REMOTE + f"ssh_command = [{literal(PY)}, 'fake_ssh.py']\nheartbeat_timeout = '5m'\n")
    observer = config.observers["r"]
    assert observer.ssh_command == (PY, "fake_ssh.py") and observer.heartbeat_timeout == 300.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config_observe.py -q`
Expected: the two changed tests and the new ones FAIL (`remote_files` is an unknown kind).

- [ ] **Step 3: Implement** — in `src/keepwatch/config.py`:

(a) Replace `OBSERVER_KINDS = ("command", "files")` with `OBSERVER_KINDS = ("command", "files", "remote_files")`.

(b) Replace the whole `OBSERVER_KEYS = (...)` assignment with:

```python
OBSERVER_KEYS = (
    Key(
        "kind",
        "str",
        None,
        "Required. `command`: a long-running program; each line it prints is an event. "
        "`files`: a local directory; each settled file is an event. "
        "`remote_files`: a directory on another host, watched over one ssh connection.",
    ),
    Key("wake", "bool", True, "Poll the watch as soon as an event arrives (unless it is backing off or offline)."),
    Key(
        "command",
        "command",
        None,
        "kind = command, required: the program. A list runs directly (a relative program is resolved against the "
        "watch directory); a string runs through the watch's `shell`.",
    ),
    Key("stdin", "str", None, "kind = command: text written to the program's standard input once at start; then stdin is closed."),
    Key(
        "heartbeat_timeout",
        "interval",
        None,
        "kind = command or remote_files: restart when no line (heartbeats included) arrives for this long. "
        "Default: none for command, 3 × `heartbeat` for remote_files.",
    ),
    Key(
        "path",
        "str",
        None,
        "kind = files, required: the directory to observe. Relative to the watch directory; `~` and environment "
        "variables are expanded.",
    ),
    Key("pattern", "str", "*", "kind = files or remote_files: report only file names matching this glob."),
    Key(
        "ignore",
        "str_list",
        DEFAULT_IGNORE,
        "kind = files or remote_files: never report file names matching any of these globs.",
    ),
    Key("recursive", "bool", False, "kind = files: also observe subdirectories."),
    Key(
        "settle",
        "duration",
        10.0,
        "kind = files or remote_files: report a file once its size and modification time have not changed for "
        "this long and it was last modified at least this long ago.",
    ),
    Key("remote", "str", None, "kind = remote_files, required: `user@host`, or a Host alias from ~/.ssh/config."),
    Key(
        "dir",
        "str",
        None,
        "kind = remote_files, required: the remote directory (not recursive). `~` and relative paths are "
        "relative to the remote home directory.",
    ),
    Key("interval", "interval", 2.0, "kind = remote_files: rescan this often (at once on inotify, where the host has it)."),
    Key("checksum", "bool", True, "kind = remote_files: compute each file's sha256 on the remote host (event key `sha256`)."),
    Key("heartbeat", "interval", 30.0, "kind = remote_files: the remote watcher prints a heartbeat after this long without output."),
    Key(
        "remote_python",
        "str",
        "auto",
        "kind = remote_files: the remote Python 3.6+ that runs the watcher. `auto`: /usr/bin/python3 if it exists, "
        "else python3 from the remote PATH.",
    ),
    Key("port", "int", None, "kind = remote_files: the ssh port (default: ssh's own, normally 22)."),
    Key(
        "identity",
        "str",
        None,
        "kind = remote_files: a private key file for ssh -i. Relative to the watch directory; `~` is expanded.",
    ),
    Key(
        "ssh_options",
        "str_list",
        (),
        'kind = remote_files: extra ssh arguments, placed before keepwatch\'s own, e.g. ["-o", "ProxyJump=bastion"].',
    ),
    Key(
        "ssh_command",
        "argv",
        None,
        'kind = remote_files: the ssh program and leading arguments. Default: ["ssh"].',
    ),
)
```

(c) Replace the `_OBSERVER_KIND_KEYS = {...}` and `_OBSERVER_REQUIRED = {...}` assignments with:

```python
_OBSERVER_KIND_KEYS = {
    "command": ("command", "stdin", "heartbeat_timeout"),
    "files": ("path", "pattern", "ignore", "recursive", "settle"),
    "remote_files": (
        "remote",
        "dir",
        "pattern",
        "ignore",
        "settle",
        "interval",
        "checksum",
        "heartbeat",
        "heartbeat_timeout",
        "remote_python",
        "port",
        "identity",
        "ssh_options",
        "ssh_command",
    ),
}
_OBSERVER_REQUIRED = {"command": ("command",), "files": ("path",), "remote_files": ("remote", "dir")}
_OBSERVER_NON_EMPTY = ("path", "remote", "dir", "remote_python")
```

(d) In `ObserverConfig`, add after `settle: float = 10.0`:

```python
    remote: str | None = None
    dir: str | None = None
    interval: float = 2.0
    checksum: bool = True
    heartbeat: float = 30.0
    remote_python: str = "auto"
    port: int | None = None
    identity: Path | None = None
    ssh_options: tuple[str, ...] = ()
    ssh_command: tuple[str, ...] | None = None
```

(e) Replace the whole `_observer` function with:

```python
def _observer(collector: _Collector, name: str, raw: dict[str, Any], watch_dir: Path) -> ObserverConfig | None:
    table = f"observe.{name}"
    kind = raw.get("kind")
    if kind not in OBSERVER_KINDS:
        shown = "missing" if kind is None else repr(kind)
        collector.add(
            f"[{table}] needs kind = one of {', '.join(OBSERVER_KINDS)}; got {shown}",
            key="kind" if kind is not None else None,
            table=table,
            topic="observers",
        )
        return None
    by_name = {key.name: key for key in OBSERVER_KEYS}
    allowed = ("kind", "wake", *_OBSERVER_KIND_KEYS[kind])
    before = len(collector.problems)
    values: dict[str, Any] = {}
    for key_name, item in raw.items():
        if key_name == "kind":
            continue
        if key_name not in by_name:
            _unknown_key(collector, key_name, allowed, table=table)
            continue
        if key_name not in allowed:
            owners = " or ".join(f'"{owner}"' for owner, names in _OBSERVER_KIND_KEYS.items() if key_name in names)
            collector.add(
                f"'{key_name}' only applies to kind = {owners}; this observer is kind = \"{kind}\"",
                key=key_name,
                table=table,
                topic="observers",
            )
            continue
        if key_name == "command":
            command = _command(collector, item, key="command", table=table, topic="observers")
            if command is not None:
                values["command"] = command
            continue
        converted = _convert(collector, by_name[key_name], item, table=table)
        if converted is _INVALID:
            continue
        if key_name in _OBSERVER_NON_EMPTY and converted == "":
            collector.add(f"'{key_name}' must not be empty", key=key_name, table=table, topic="observers")
            continue
        if key_name == "port" and not 1 <= converted <= 65535:
            collector.add(
                f"'port' must be between 1 and 65535, got {converted}", key="port", table=table, topic="observers"
            )
            continue
        values[key_name] = converted
    for required in _OBSERVER_REQUIRED[kind]:
        if required not in raw:
            collector.add(f"[{table}] (kind = \"{kind}\") needs '{required}'", table=table, topic="observers")
    if len(collector.problems) > before:
        return None
    for key_name in ("path", "identity"):
        if key_name in values:
            values[key_name] = _observed_path(values[key_name], watch_dir)
    return ObserverConfig(name=name, kind=kind, **values)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_observe.py tests/test_docs.py -q`
Expected: all pass.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes). `build_observer` does not know `remote_files` yet; no existing test builds one. Then:

```bash
git add src/keepwatch/config.py tests/test_config_observe.py
git commit -m "Parse remote_files observers"
```

---

### Task 3: The `remote_files` observer

**Files:**
- Modify: `src/keepwatch/observers.py`, `src/keepwatch/output.py`
- Create: `tests/fake_ssh.py` (a test helper, not a test module)
- Test: `tests/test_remote_files_observer.py` (new)

**Interfaces:**
- Consumes: `CommandObserver`, `_OutputLimiter` (as `self._output`), `build_observer`, `observer_environment` (Plan 6a); `ObserverConfig` remote fields (Task 2); `remote_watcher.py` (Task 1).
- Produces:
  - `observers.AUTO_PYTHON: str` (the remote command for `remote_python = "auto"`)
  - `observers.remote_command(config: ObserverConfig) -> list[str]`
  - `observers.watcher_options(config: ObserverConfig) -> dict[str, Any]`
  - `observers.watcher_source(options: Mapping[str, Any]) -> str`
  - `observers.RemoteFilesObserver(CommandObserver)` (`kind = "remote_files"`); `build_observer` returns it
  - delivered events: only `{"event": "file", …, "remote": <config.remote>}`; `hello` becomes record `observer.connected` (`remote`, `dir`, `python`, `version`, `inotify`); any other stdout line goes to `observer.output` with `stream = "stdout"`
  - `tests/fake_ssh.py`: drops ssh options and the host and runs the remote command locally; `$FAKE_SSH_LOG` (append one JSON line of `options`, `host`, `command`), `$FAKE_SSH_FAIL` (exit 255 with an ssh-like error), `$FAKE_SSH_BANNER` (print it on stdout first)

- [ ] **Step 1: Create the test helper** — `tests/fake_ssh.py`:

```python
"""A stand-in for ssh in tests: drops ssh's options and the host, then runs the remote command here.

Environment: FAKE_SSH_LOG (append {"options", "host", "command"} as one JSON line), FAKE_SSH_FAIL (fail like
an unreachable host), FAKE_SSH_BANNER (print this on stdout first, like a chatty login script).
"""

import json
import os
import shlex
import subprocess
import sys

VALUE_OPTIONS = set("BbcDEeFIiJLlmOoPpQRSWw")


def split(argv):
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        index += 2 if len(arg) == 2 and arg[1] in VALUE_OPTIONS else 1
    return argv[:index], argv[index], " ".join(argv[index + 1 :])


def main():
    options, host, command = split(sys.argv[1:])
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"options": options, "host": host, "command": command}) + "\n")
    if os.environ.get("FAKE_SSH_FAIL"):
        sys.stderr.write(f"ssh: connect to host {host} port 22: Connection refused\n")
        return 255
    banner = os.environ.get("FAKE_SSH_BANNER")
    if banner:
        print(banner, flush=True)
    return subprocess.call(shlex.split(command))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write the failing tests** — create `tests/test_remote_files_observer.py`:

```python
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path

import pytest

from keepwatch import observers
from keepwatch.config import ObserverConfig, load_watch_config
from keepwatch.observers import (
    AUTO_PYTHON,
    RemoteFilesObserver,
    build_observer,
    remote_command,
    watcher_options,
    watcher_source,
)
from keepwatch.output import format_record
from portable import PY, literal, toml_path

FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")
DEFAULT_SSH = [
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
]


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def age(path, seconds=60):
    old = time.time() - seconds
    os.utime(path, (old, old))


def remote_observer(make_watch, xdg, remote_dir, extra="", python=None):
    remote_python = literal(PY) if python is None else python
    config = (
        "[observe.remote]\nkind = 'remote_files'\nremote = 'me@linux1.example'\n"
        f"dir = {toml_path(remote_dir)}\nsettle = '1s'\ninterval = '1s'\n"
        f"remote_python = {remote_python}\nssh_command = [{literal(PY)}, {toml_path(FAKE_SSH)}]\n{extra}"
    )
    watch = load_watch_config(make_watch("w", config=config))
    events, records = [], []
    observer = build_observer(
        watch,
        watch.observers["remote"],
        deliver=events.append,
        sink=records.append,
        environment={},
        data_dir=xdg.watch_data_dir("w"),
    )
    return observer, events, records


def stop(observer):
    observer.stop()
    assert observer.join(20)


def remote_dir_with_file(tmp_path, data=b"data"):
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    (remote_dir / "a.tar.gz").write_bytes(data)
    age(remote_dir / "a.tar.gz")
    return remote_dir


def test_remote_command_defaults():
    config = ObserverConfig(name="r", kind="remote_files", remote="me@h", dir="/data/out")
    assert remote_command(config) == ["ssh", *DEFAULT_SSH, "--", "me@h", AUTO_PYTHON]


def test_remote_command_options():
    config = ObserverConfig(
        name="r",
        kind="remote_files",
        remote="me@h",
        dir="/d",
        port=2222,
        identity=Path("/keys/id"),
        ssh_options=("-o", "ProxyJump=bastion"),
        remote_python="/opt/my py/python3",
        ssh_command=("ssh.exe",),
    )
    assert remote_command(config) == [
        "ssh.exe",
        "-o",
        "ProxyJump=bastion",
        *DEFAULT_SSH,
        "-p",
        "2222",
        "-i",
        str(Path("/keys/id")),
        "--",
        "me@h",
        "'/opt/my py/python3' -u -",
    ]


def test_auto_python_prefers_the_system_interpreter():
    assert AUTO_PYTHON == (
        "sh -c 'if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 \"$@\"; else exec python3 \"$@\"; fi' sh -u -"
    )


def test_watcher_source_carries_the_options():
    options = watcher_options(ObserverConfig(name="r", kind="remote_files", remote="h", dir="~/out/été"))
    assert options == {
        "dir": "~/out/été",
        "pattern": "*",
        "ignore": [".*", "*.tmp", "*.part", "*~"],
        "settle": 10.0,
        "interval": 2.0,
        "checksum": True,
        "heartbeat": 30.0,
    }
    source = watcher_source(options)
    first, rest = source.split("\n", 1)
    namespace = {}
    exec(first, namespace)
    assert json.loads(namespace["KEEPWATCH_REMOTE_ARGS"]) == options
    assert "def main(argv)" in rest
    source.encode("ascii")


def test_reports_remote_files(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    observer, events, records = remote_observer(make_watch, xdg, remote_dir)
    assert isinstance(observer, RemoteFilesObserver) and observer.kind == "remote_files"
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    event = events[0]
    assert (event["event"], event["name"], event["size"]) == ("file", "a.tar.gz", 4)
    assert (event["remote"], event["observer"]) == ("me@linux1.example", "remote")
    assert event["sha256"] == hashlib.sha256(b"data").hexdigest()
    assert Path(event["path"]) == remote_dir / "a.tar.gz"
    [connected] = kinds(records, "observer.connected")
    assert connected["remote"] == "me@linux1.example" and connected["python"] == platform.python_version()
    assert connected["inotify"] is sys.platform.startswith("linux")
    assert kinds(records, "observer.started")[0]["kind"] == "remote_files"
    assert all(event["event"] == "file" for event in events)
    assert "connected to me@linux1.example" in format_record(connected)


def test_ssh_arguments(tmp_path, make_watch, xdg):
    log = tmp_path / "ssh.log"
    remote_dir = remote_dir_with_file(tmp_path)
    extra = f"port = 2222\n[environment]\nFAKE_SSH_LOG = {toml_path(log)}\n"
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, extra)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.connected"))
    finally:
        stop(observer)
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["host"] == "me@linux1.example"
    assert "BatchMode=yes" in entry["options"] and "-T" in entry["options"]
    assert entry["options"][entry["options"].index("-p") + 1] == "2222"
    assert entry["command"].endswith(" -u -")


def test_connection_failure_is_retried(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, "[environment]\nFAKE_SSH_FAIL = '1'\n")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["exit_code"] == 255 and first["level"] == "WARNING"
    assert "Connection refused" in first["stderr_tail"][0]
    assert events == []


def test_login_script_noise_is_logged_not_delivered(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    extra = "[environment]\nFAKE_SSH_BANNER = 'Welcome to linux1'\n"
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, extra)
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.tar.gz"]
    outputs = [r for r in kinds(records, "observer.output") if r["stream"] == "stdout"]
    assert outputs[0]["text"] == "Welcome to linux1"


def test_missing_remote_directory_is_retried(tmp_path, make_watch, xdg):
    observer, events, records = remote_observer(make_watch, xdg, tmp_path / "missing")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["exit_code"] == 2 and "does not exist" in first["stderr_tail"][0]


def test_heartbeat_timeout_defaults_to_three_heartbeats(tmp_path, make_watch, xdg):
    observer, events, records = remote_observer(make_watch, xdg, tmp_path, "heartbeat = '10s'\n")
    assert observer.config.heartbeat_timeout == 30.0


def test_explicit_heartbeat_timeout(tmp_path, make_watch, xdg):
    observer, events, records = remote_observer(make_watch, xdg, tmp_path, "heartbeat_timeout = '5m'\n")
    assert observer.config.heartbeat_timeout == 300.0


@pytest.mark.posix_only
def test_auto_remote_python(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, python="'auto'")
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert kinds(records, "observer.connected")[0]["python"].startswith("3.")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_remote_files_observer.py -q`
Expected: collection error, `ImportError: cannot import name 'AUTO_PYTHON'`.

- [ ] **Step 4: Implement `src/keepwatch/observers.py`**

(a) Imports: add `import shlex` to the stdlib block, `from dataclasses import replace` and `from importlib import resources`; change `from keepwatch.config import ObserverConfig, WatchConfig` to `from keepwatch.config import Command, ObserverConfig, WatchConfig`.

(b) Add after the `FILES_*` constants:

```python
AUTO_PYTHON = (
    "sh -c 'if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 \"$@\"; else exec python3 \"$@\"; fi' sh -u -"
)
SSH_DEFAULTS = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    # No connection sharing: a ControlPersist master would outlive the ssh keepwatch stops and hold its pipes.
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)
```

(c) Update the module docstring's first paragraph to also say: `a remote_files observer runs keepwatch's remote watcher on another host over ssh.`

(d) Add before `build_observer`:

```python
def remote_command(config: ObserverConfig) -> list[str]:
    """The ssh command line that runs the remote watcher. User ssh_options come first: ssh keeps the first value."""
    argv = [*(config.ssh_command or ("ssh",)), *config.ssh_options, *SSH_DEFAULTS]
    if config.port is not None:
        argv += ["-p", str(config.port)]
    if config.identity is not None:
        argv += ["-i", str(config.identity)]
    python = AUTO_PYTHON if config.remote_python == "auto" else f"{shlex.quote(config.remote_python)} -u -"
    assert config.remote is not None
    return [*argv, "--", config.remote, python]


def watcher_options(config: ObserverConfig) -> dict[str, Any]:
    return {
        "dir": config.dir,
        "pattern": config.pattern,
        "ignore": list(config.ignore),
        "settle": config.settle,
        "interval": config.interval,
        "checksum": config.checksum,
        "heartbeat": config.heartbeat,
    }


def watcher_source(options: Mapping[str, Any]) -> str:
    """The remote watcher's source with its options prepended as an assignment (ASCII: json escapes the rest)."""
    source = resources.files("keepwatch").joinpath("remote_watcher.py").read_text(encoding="utf-8")
    return f"KEEPWATCH_REMOTE_ARGS = {json.dumps(json.dumps(options))}\n{source}"


class RemoteFilesObserver(CommandObserver):
    """A command observer running the remote watcher over ssh; only file events are delivered."""

    kind = "remote_files"

    def __init__(
        self, watch: WatchConfig, config: ObserverConfig, *, deliver: Deliver, sink: Sink, env: Mapping[str, str]
    ) -> None:
        timeout = config.heartbeat_timeout if config.heartbeat_timeout is not None else 3 * config.heartbeat
        command_config = replace(
            config,
            command=Command(argv=tuple(remote_command(config))),
            stdin=watcher_source(watcher_options(config)),
            heartbeat_timeout=timeout,
        )
        super().__init__(watch, command_config, deliver=deliver, sink=sink, env=env)

    def emit(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "hello":
            self._sink(
                make_record(
                    "observer.connected",
                    **self._tag(),
                    remote=self.config.remote,
                    dir=event.get("dir"),
                    python=event.get("python"),
                    version=event.get("version"),
                    inotify=event.get("inotify"),
                )
            )
            return
        if kind != "file":
            # Anything else on stdout (a login script's banner, say) is logged, never delivered.
            text = event["line"] if set(event) == {"line"} else json.dumps(event, ensure_ascii=False)
            self._output.line(text, stream="stdout")
            return
        super().emit({**event, "remote": self.config.remote})
```

(e) In `build_observer`, replace the `if config.kind == "command":` block with:

```python
    if config.kind in ("command", "remote_files"):
        with contextlib.suppress(OSError):
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = observer_environment(watch, config.name, environment=environment, data_dir=data_dir)
        cls = CommandObserver if config.kind == "command" else RemoteFilesObserver
        return cls(watch, config, deliver=deliver, sink=sink, env=env)
```

- [ ] **Step 5: Implement `src/keepwatch/output.py`** — add before `_SKIP = ...`:

```python
def _observer_connected(record: dict[str, Any], verbose: bool) -> str:
    notify = "inotify" if record.get("inotify") else "rescanning"
    return (
        f"observer {record.get('observer')} connected to {record.get('remote')}: watching {record.get('dir')} "
        f"(remote Python {record.get('python')}, {notify})"
    )
```

and add `"observer.connected": _observer_connected,` to `_FORMATTERS`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_remote_files_observer.py tests/test_observers.py -q`
Expected: all pass.

- [ ] **Step 7: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/observers.py src/keepwatch/output.py tests/fake_ssh.py tests/test_remote_files_observer.py
git commit -m "remote_files observers: the remote watcher over one ssh connection"
```

---

### Task 4: Documentation

**Files:**
- Modify: `src/keepwatch/reference/observers.md` (created by Plan 6a), `src/keepwatch/reference/logging.md`
- Modify: `docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md` (section 3: how options travel)
- Test: `tests/test_docs.py` (`KEY_FACTS`)

- [ ] **Step 1: Write the failing test** — in `tests/test_docs.py`, `KEY_FACTS["observers"]`: append `"remote_files"`, `"remote_python"`, `"BatchMode"`, `"known_hosts"`, `"observer.connected"`, `"Python 3.6"`, `"ssh-agent"`; append `"observer.connected"` to `KEY_FACTS["logging"]`.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_docs.py -q`
Expected: FAIL — the observers and logging topics are missing the new facts.

- [ ] **Step 3: Write the docs**

(a) `src/keepwatch/reference/observers.md`: insert this section right before the line `## What hooks receive`:

````markdown
### kind = "remote_files"

Watches a directory on another host through **one long-lived outbound ssh connection**. Nothing is installed there: keepwatch sends its remote watcher (one Python file) over ssh's standard input to the remote host's Python 3.6 or newer, which runs it from memory.

```toml
[observe.remote]
kind = "remote_files"
remote = "me@linux1"           # or a Host alias from ~/.ssh/config
dir = "/data/out"              # remote path; ~ and relative paths start at the remote home
pattern = "*.tar.gz"
settle = "30s"
```

- **What it reports:** the same file events as `files`, plus `"sha256"` (computed remotely; `checksum = false` turns it off) and `"remote"`. Every settled file is reported once per connection, **including everything already there when it connects**, so after a reconnect it catches up; handled files must be recorded in a ledger.
- **How it decides:** the same settle rules as `files`. It rescans every `interval` (2s), and at once when the host has inotify (used directly, without inotify-tools).
- **Liveness:** the watcher prints a heartbeat after `heartbeat` (30s) without output; a connection that delivers nothing for `heartbeat_timeout` (default 3 × `heartbeat`) is closed and reopened. ssh also runs with `ServerAliveInterval=15` and `ConnectTimeout=15`, and every disconnect is retried with the observer backoff (5s up to 5 minutes).
- **The ssh command:** `ssh [ssh_options] -T -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ControlMaster=no -o ControlPath=none [-p port] [-i identity] -- <remote> <python> -u -`. Connection sharing is off on purpose (a `ControlPersist` master in `~/.ssh/config` would outlive the connection keepwatch stops). `observer.started` shows the command in full.
- **Which Python:** `remote_python = "auto"` runs `/usr/bin/python3` when it exists (the system interpreter, which on EL8 is 3.6), else `python3` from the remote PATH. Name another one when needed: `remote_python = "/opt/python3.11/bin/python3"`. Python 2 does not work.
- **Login scripts:** any shell works (sh, bash, csh, tcsh): the watcher's options travel inside the program text, not through shell quoting. Lines a login script prints on stdout are logged as `observer.output`, never delivered.

**Requirements on the keepwatch host.** BatchMode means ssh never asks anything, so:

1. **Key authentication.** A key without a passphrase (`identity = '~/.ssh/id_relay'`), or a key held by ssh-agent. On Windows the OpenSSH Authentication Agent service works for the service too; on Linux the service only sees an agent through `SSH_AUTH_SOCK` in `[environment]` (see `keepwatch docs environment`).
2. **A known host key.** Connect once by hand (`ssh me@linux1 true`) to put the key in `known_hosts`, or the connection fails with "Host key verification failed".
3. **Check it the way the service will:** `ssh -o BatchMode=yes me@linux1 true` must succeed without a prompt, then `keepwatch observe <watch> remote --for 1m` must print the files.

When it does not connect, `keepwatch logs <watch> --event observer.stopped` shows ssh's last error lines (`stderr_tail`); `observer.connected` records each successful connection with the remote Python version and whether inotify is used.
````

(b) `src/keepwatch/reference/logging.md`: add a row after the `observer.started` row:

```markdown
| `observer.connected` | `observer`, `remote`, `dir`, `python`, `version`, `inotify`: a remote_files observer's ssh connection is up |
```

(c) Spec, section 3: replace the sentence

```
and writes the watcher's source to ssh's stdin.
```

with

```
and writes the watcher's source to ssh's stdin, with its options prepended as one Python assignment
(`KEEPWATCH_REMOTE_ARGS = "<json>"`) instead of a command-line argument, so nothing passes through the
remote login shell's quoting (EDA hosts often use csh/tcsh). keepwatch also passes `-T`,
`-o ConnectTimeout=15`, `-o ControlMaster=no -o ControlPath=none` (no connection sharing) and `--` before the host; the watcher's `hello` becomes an `observer.connected`
record, and stdout lines other than file events (login-script banners) are logged, not delivered.
```

and in the command shown just above it, replace `<python> -u - '<json args>'` with `<python> -u -`.

- [ ] **Step 4: Run the docs tests and read the topic**

Run: `uv run pytest tests/test_docs.py -q`, then `uv run keepwatch docs observers` and `uv run keepwatch docs config`.
Expected: tests pass; the `remote_files` keys appear in the `[observe.<name>]` table.

- [ ] **Step 5: Full suite, lint, commit**

Run `uv run pytest -q` and `uv run ruff check src tests` (no pipes), then:

```bash
git add src/keepwatch/reference/observers.md src/keepwatch/reference/logging.md tests/test_docs.py docs/superpowers/specs/2026-10-01-keepwatch-observers-relay-design.md
git commit -m "Document remote_files observers"
```

---

## After the last task

Report: the commits made, the final `uv run pytest -q` summary line, and anything in this plan you had to question. Do not push; the supervisor pushes the branch and checks the Windows and Python 3.6 CI jobs.
