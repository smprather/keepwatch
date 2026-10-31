"""watchglass's remote watcher: reports settled files in one directory as JSON lines on stdout.

watchglass sends this file over ssh to the remote host's Python and runs it there; nothing is installed.
It must stay compatible with Python 3.6, use only the standard library and stay ASCII (the list of newer
features to avoid is in tests/test_remote_watcher.py).

Options are one JSON object: the first command-line argument, or WATCHGLASS_REMOTE_ARGS when watchglass
prepends that assignment to the source it sends on stdin (so nothing passes through the remote login
shell's quoting rules). With inotify the directory is rescanned on every notification and every
"rescan" seconds; without it every "interval" seconds.

Output, one JSON object per line, flushed at once:
  {"event": "hello", "version", "python", "inotify", "dir"}     once, at start
  {"event": "file", "path", "name", "size", "mtime", "sha256"}  once per settled file (sha256 with checksum)
  {"event": "heartbeat"}                                         after `heartbeat` seconds without output

Tree mode ({"mode": "tree", "dir", "ignore", "settle", "expected_files": [{"path", "size", "mtime"}],
"expected_dirs": [path]}): walks `dir` recursively and reports only the difference against the caller's expected
state: the caller's own record of what it has already handled (its checkpoint). A path that matches its expected
size and mtime is not mentioned; a new or changed one is; and an expected path the walk does not find is reported
`gone`, so a deletion that happened while watchglass was away is still heard. Symlinks are not followed. Output:

  {"event": "hello", ..., "mode": "tree", "dir"}
  {"event": "files", "entries": [[path, size, mtime], ...]}   new or changed, in chunks of TREE_CHUNK
  {"event": "dirs", "paths": [path, ...]}                     directories not in expected_dirs
  {"event": "gone", "paths": [path, ...]}                     expected or reported paths the walk did not find
  {"event": "heartbeat"}
  {"event": "ready", "files", "dirs", "sent", "new_dirs", "gone", "skipped", "settling", "walk_ms"}
                                                                 after every complete walk (not delivered to hooks)

Delete mode ({"mode": "delete", "dir", "files": [{"path", "size", "mtime"}]}): deletes each file only if it is
a regular file directly in `dir` (resolved, never through a symlink) whose size and mtime still match (and
sha256, when the item carries one); prints
{"event": "deleted" | "gone" | "changed" | "refused" | "failed", "path", "reason"?, "index"} per file, then
{"event": "done"}.

File-operation modes (used by the mirror's resumable push; one JSON line and exit):
  {"mode": "hash", "path"}                         -> {"event": "hash", "size", "sha256"} |
                                                       {"event": "missing" | "refused" | "failed", ...}
  {"mode": "finalize", "path", "target"}           -> os.replace(path, target); {"event": "finalized"} |
                                                       {"event": "missing" | "failed", ...}
  {"mode": "remove", "path"}                        -> {"event": "removed" | "gone" | "failed", ...}
Exit status: 0 when stdout is closed (watchglass went away), 2 for bad options, a missing directory or an
error while scanning (the message goes to stderr).
"""

import errno
import fnmatch
import hashlib
import json
import os
import select
import socket
import stat
import sys
import time

# Do not import `platform` (or any name watchglass uses for a module): run as a script from src/watchglass/,
# watchglass's own platform.py would shadow the standard library's.
WATCHER_VERSION = 2
DEFAULTS = {
    "dir": None,
    "pattern": "*",
    "ignore": [".*", "*.tmp", "*.part", "*~"],
    "settle": 10.0,
    "interval": 2.0,
    "checksum": True,
    "heartbeat": 30.0,
    "rescan": 30.0,
    "skip": [],
    "mode": "watch",
    "files": [],
    "expected_files": [],
    "expected_dirs": [],
    "path": None,
    "target": None,
}
SETTLE_STEP = 1.0  # rescan this often while a file is settling
MIN_RESCAN = 0.5  # at most two scans a second, however many notifications arrive
CHUNK = 1024 * 1024
TREE_ONLY = ("expected_files", "expected_dirs")  # mode "tree" only, so watch mode cannot silently ignore them
TREE_CHUNK = 500  # entries or paths per event: a line stays far under watchglass's 1 MiB MAX_LINE
# IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
IN_MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200


class Closed(Exception):
    """stdout was closed: watchglass is gone."""


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
    except (ImportError, OSError, AttributeError):
        return None
    fd = init()
    if fd < 0:
        return None
    if not inotify_add(fd, directory):
        os.close(fd)
        return None
    return fd


_LIBC = []  # the loaded libc, once: tree mode adds a watch per directory as the walk finds them


def inotify_add(fd, directory):
    """Watch another directory on an existing inotify fd (inotify is not recursive); False when it did not take."""
    try:
        if not _LIBC:
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            _LIBC.append(libc)
        return _LIBC[0].inotify_add_watch(fd, directory, IN_MASK) >= 0
    except (ImportError, OSError, AttributeError):
        return False


def wanted(name, options):
    if not fnmatch.fnmatch(name, options["pattern"]):
        return False
    for pattern in options["ignore"]:
        if fnmatch.fnmatch(name, pattern):
            return False
    return True


def ignored(name, patterns):
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def sha256_of(path, output=None, heartbeat=None):
    """The file's sha256, read in chunks; with an output, heartbeats keep flowing while a large file is hashed."""
    digest = hashlib.sha256()
    try:
        handle = open(path, "rb")
    except OSError:
        raise  # the caller decides what an unreadable file means (one still being written, say)
    with handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            if output is not None:
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
        self.unreadable = {}  # (path, size, mtime_ns) -> when reading it last failed
        self.noted = set()
        self.skip = set(options["skip"])  # "path|size|mtime" keys the caller has already handled

    def note(self, message):
        """Tell watchglass about a problem once (stderr lines become observer.output records)."""
        if message in self.noted:
            return
        self.noted.add(message)
        sys.stderr.write("watchglass remote watcher: " + message + "\n")
        sys.stderr.flush()

    def scan(self):
        """Report settled files. Returns True while some file is still settling. Raises OSError if the directory is gone."""
        settle = self.options["settle"]
        try:
            names = os.listdir(self.directory)
        except OSError:
            raise  # documented above: the directory may be gone, and the caller reports that
        now = time.monotonic()
        wall = time.time()
        present = set()
        current = set()
        for raw in sorted(names):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                # watchglass could not name it to scp, log it or hand it to a hook as text.
                self.note("skipping a file whose name is not UTF-8: %r" % raw)
                continue
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
            if self.skip and "%s|%d|%r" % (decode(path), info.st_size, info.st_mtime) in self.skip:
                self.reported.add((path,) + key)
                continue
            failed_at = self.unreadable.get((path,) + key)
            if failed_at is not None and now - failed_at < self.options["rescan"]:
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
                except OSError as exc:
                    del self.candidates[path]
                    self.unreadable[(path,) + key] = now
                    self.note("cannot read %s: %s" % (name, exc.strerror or exc))
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
        for stale in set(self.unreadable) - current:
            del self.unreadable[stale]
        return bool(self.candidates)

    def run(self):
        inotify = open_inotify(self.directory)
        self.output.send(
            {
                "event": "hello",
                "version": WATCHER_VERSION,
                "python": "%d.%d.%d" % sys.version_info[:3],
                "inotify": inotify is not None,
                "dir": decode(self.directory),
            }
        )
        # With inotify, changes wake the loop at once; full rescans are only a safety net.
        interval = self.options["interval"] if inotify is None else max(self.options["interval"], self.options["rescan"])
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


class TreeWatcher(object):
    """mode = "tree": walk `dir` recursively and report the difference against the caller's expected state.

    The expected state is the caller's checkpoint: what it last recorded as handled. Nothing is inferred from
    silence, so a walk that cannot read a subtree reports a problem on stderr instead of a `gone`.
    """

    def __init__(self, options, output):
        self.options = options
        self.output = output
        self.directory = os.path.abspath(os.path.expanduser(options["dir"])).encode("utf-8", "surrogateescape")
        self.expected_files = {}
        for item in options["expected_files"]:
            self.expected_files[os.fsencode(item["path"])] = (int(item["size"]), float(item["mtime"]))
        self.expected_dirs = set(os.fsencode(path) for path in options["expected_dirs"])
        self.reported = {}  # path -> (size, mtime): what this connection has already told
        self.reported_dirs = set()
        self.watched = set()
        self.inotify = None
        self.noted = set()

    def note(self, message):
        if message in self.noted:
            return
        self.noted.add(message)
        sys.stderr.write("watchglass remote watcher: " + message + "\n")
        sys.stderr.flush()

    def text(self, path):
        """`decode(path)` when it survives a UTF-8 round trip; None (and a note, once) when it does not.

        watchglass could not log such a name, hand it to a hook, or name it to scp, and its client drops a whole
        record it cannot encode: reporting one would cost the 499 good names it travels with.
        """
        name = decode(path)
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            self.note("skipping a name that is not UTF-8: %r" % path)
            return None
        return name

    def walk(self):
        """(files, dirs, held, fresh): the whole tree. `held` are files still inside `settle`, not gone."""
        files = {}
        dirs = set()
        held = set()
        fresh = False
        settle = self.options["settle"]
        now = time.time()
        for root, dirnames, filenames in os.walk(self.directory):
            keep = []
            for name in sorted(dirnames):
                path = os.path.join(root, name)
                if os.path.islink(path):
                    self.note("not following the symlink to a directory: " + decode(path))
                    continue
                if ignored(decode(name), self.options["ignore"]):
                    continue
                dirs.add(path)
                keep.append(name)
            dirnames[:] = keep
            for name in sorted(filenames):
                if ignored(decode(name), self.options["ignore"]):
                    continue
                path = os.path.join(root, name)
                try:
                    info = os.lstat(path)
                except OSError as exc:
                    self.note("cannot read " + decode(path) + ": " + (exc.strerror or str(exc)))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    if stat.S_ISLNK(info.st_mode):
                        self.note("not following the symlink: " + decode(path))
                    continue
                if settle and now - info.st_mtime < settle:
                    held.add(path)
                    fresh = True
                    continue
                files[path] = (info.st_size, info.st_mtime)
        return files, dirs, held, fresh

    def add_watches(self, dirs):
        """Watch each directory the walk found: inotify is not recursive, and the rescan covers a refused one."""
        if self.inotify is None:
            return
        for path in sorted(dirs):
            if path in self.watched:
                continue
            self.watched.add(path)  # one attempt each: a subtree that cannot be watched is polled instead
            if not inotify_add(self.inotify, path):
                self.note("cannot watch " + decode(path) + " (its changes are picked up by the rescan)")

    def report(self, files, dirs, held, started):
        """Deliver one walk (files, dirs, gone, ready) and forget what it accounted for."""
        sent = 0
        skipped = 0
        entries = []
        for path in sorted(files):
            state = files[path]
            if self.expected_files.get(path) == state:
                skipped += 1
                self.reported[path] = state
                continue
            if self.reported.get(path) == state:
                continue
            shown = self.text(path)
            if shown is None:
                continue
            self.reported[path] = state
            self.expected_files.pop(path, None)
            entries.append([shown, state[0], state[1]])
            sent += 1
        for index in range(0, len(entries), TREE_CHUNK):
            self.output.send({"event": "files", "entries": entries[index : index + TREE_CHUNK]})
        new_dirs = []
        for path in sorted(dirs):
            if path in self.expected_dirs or path in self.reported_dirs:
                continue
            shown = self.text(path)
            if shown is None:
                continue
            self.reported_dirs.add(path)
            new_dirs.append(shown)
        for index in range(0, len(new_dirs), TREE_CHUNK):
            self.output.send({"event": "dirs", "paths": new_dirs[index : index + TREE_CHUNK]})
        missing = set()
        for path in sorted(self.expected_files):
            if path not in files and path not in held:
                missing.add(path)
        for path in missing:
            del self.expected_files[path]
        for path in sorted(self.reported):
            if path not in files and path not in held:
                missing.add(path)
        for path in missing:
            self.reported.pop(path, None)
        missing_dirs = set()
        for path in sorted(self.expected_dirs):
            if path not in dirs:
                missing_dirs.add(path)
        for path in sorted(self.reported_dirs):
            if path not in dirs:
                missing_dirs.add(path)
        self.expected_dirs -= missing_dirs
        self.reported_dirs -= missing_dirs
        gone = []
        for path in sorted(missing | missing_dirs):
            shown = self.text(path)
            if shown is not None:
                gone.append(shown)
        for index in range(0, len(gone), TREE_CHUNK):
            self.output.send({"event": "gone", "paths": gone[index : index + TREE_CHUNK]})
        self.output.send(
            {
                "event": "ready",
                "files": len(files),
                "dirs": len(dirs),
                "sent": sent,
                "new_dirs": len(new_dirs),
                "gone": len(gone),
                "skipped": skipped,
                "settling": bool(held),
                "walk_ms": int((time.monotonic() - started) * 1000),
            }
        )

    @staticmethod
    def wait(inotify, seconds):
        return Watcher.wait(inotify, seconds)

    def run(self):
        self.inotify = open_inotify(self.directory)
        self.output.send(
            {
                "event": "hello",
                "version": WATCHER_VERSION,
                "python": "%d.%d.%d" % sys.version_info[:3],
                "inotify": self.inotify is not None,
                "mode": "tree",
                "dir": decode(self.directory),
            }
        )
        interval = self.options["interval"] if self.inotify is None else max(
            self.options["interval"], self.options["rescan"]
        )
        heartbeat = self.options["heartbeat"]
        last_scan = 0.0
        while True:
            pause = last_scan + MIN_RESCAN - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            last_scan = time.monotonic()
            started = time.monotonic()
            files, dirs, held, fresh = self.walk()
            self.add_watches(dirs)
            self.report(files, dirs, held, started)
            wait = min(interval, SETTLE_STEP) if fresh else interval
            wait = max(min(wait, heartbeat - (time.monotonic() - self.output.last)), 0.0)
            self.wait(self.inotify, wait)
            self.output.heartbeat_if_due(heartbeat)


def delete_one(item, directory):
    """Delete one pulled file if it is still exactly what was pulled: size, mtime and sha256 (required)."""
    raw = str(item.get("path", "")).encode("utf-8", "surrogateescape")
    if os.path.realpath(os.path.dirname(raw)) != directory:
        return {"event": "refused", "reason": "not directly in " + decode(directory)}
    try:
        info = os.lstat(raw)
    except FileNotFoundError:
        return {"event": "gone"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
    if not stat.S_ISREG(info.st_mode):
        return {"event": "refused", "reason": "not a regular file"}
    if info.st_size != item.get("size") or info.st_mtime != float(item.get("mtime", -1)):
        return {"event": "changed"}
    expected = item.get("sha256")
    if not expected:
        return {"event": "refused", "reason": "no sha256 given: the content cannot be checked"}
    try:
        if sha256_of(raw) != expected:
            return {"event": "changed", "reason": "its content differs from the pulled copy"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
    try:
        os.unlink(raw)
    except FileNotFoundError:
        return {"event": "gone"}
    except OSError as exc:
        return {"event": "failed", "reason": exc.strerror or str(exc)}
    return {"event": "deleted"}


def delete_files(options, output):
    directory = os.path.realpath(os.path.expanduser(options["dir"]).encode("utf-8", "surrogateescape"))
    for index, item in enumerate(options["files"]):
        try:
            result: dict = delete_one(item, directory)
        except Exception as exc:  # keep going: one odd entry must not stop the others
            result = {"event": "failed", "reason": str(exc)}
        result["path"] = item.get("path")
        result["index"] = index
        output.send(result)
    output.send({"event": "done"})
    return 0


def hash_path(options, output):
    """One file's size and sha256, for a mirror's resumable push (see the module docstring)."""
    path = str(options["path"]).encode("utf-8", "surrogateescape")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        output.send({"event": "missing"})
        return 0
    except OSError as exc:
        output.send({"event": "failed", "reason": exc.strerror or str(exc)})
        return 0
    if not stat.S_ISREG(before.st_mode):
        output.send({"event": "refused", "reason": "not a regular file"})
        return 0
    try:
        digest = sha256_of(path, output, options["heartbeat"])
        after = os.lstat(path)
    except OSError as exc:
        output.send({"event": "failed", "reason": exc.strerror or str(exc)})
        return 0
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        output.send({"event": "changed"})
        return 0
    output.send({"event": "hash", "size": after.st_size, "sha256": digest})
    return 0


def finalize_path(options, output):
    """Atomically replace `target` with the uploaded temp (`os.replace`: one name, never a partial final)."""
    path = str(options["path"]).encode("utf-8", "surrogateescape")
    target = str(options["target"]).encode("utf-8", "surrogateescape")
    try:
        os.replace(path, target)
    except FileNotFoundError:
        output.send({"event": "missing"})
        return 0
    except OSError as exc:
        output.send({"event": "failed", "reason": exc.strerror or str(exc)})
        return 0
    output.send({"event": "finalized"})
    return 0


def remove_path(options, output):
    """Delete an abandoned temp file; a missing one is not an error."""
    path = str(options["path"]).encode("utf-8", "surrogateescape")
    try:
        os.unlink(path)
    except FileNotFoundError:
        output.send({"event": "gone"})
    except OSError as exc:
        output.send({"event": "failed", "reason": exc.strerror or str(exc)})
    else:
        output.send({"event": "removed"})
    return 0


def parse_options(text):
    try:
        given = json.loads(text)
    except ValueError:
        raise  # not JSON at all: the caller says so on stderr and exits
    if not isinstance(given, dict):
        raise ValueError("options must be a JSON object")
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        raise ValueError("unknown option(s): " + ", ".join(unknown))
    options = dict(DEFAULTS)
    options.update(given)
    if options["mode"] not in ("watch", "delete", "tree", "hash", "finalize", "remove"):
        raise ValueError("option 'mode' must be watch, delete, tree, hash, finalize or remove")
    if options["mode"] != "tree":
        tree_only = sorted(set(given) & set(TREE_ONLY))
        if tree_only:
            raise ValueError("option(s) need mode 'tree': " + ", ".join(tree_only))
    if options["mode"] in ("hash", "finalize", "remove"):
        if not options["path"]:
            raise ValueError("option 'path' is required")
        if options["mode"] == "finalize" and not options["target"]:
            raise ValueError("option 'target' is required")
        return options
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
    sys.stderr.write("watchglass remote watcher on " + socket.gethostname() + ": " + message + "\n")
    sys.stderr.flush()
    return 2


def main(argv):
    raw = argv[0] if argv else globals().get("WATCHGLASS_REMOTE_ARGS", "{}")
    try:
        options = parse_options(raw)
    except ValueError as exc:
        return fail(str(exc))
    if options["mode"] == "delete":
        try:
            return delete_files(options, Output(sys.stdout))
        except Closed:
            silence_stdout()
            return 0
    if options["mode"] in ("hash", "finalize", "remove"):
        operation = {"hash": hash_path, "finalize": finalize_path, "remove": remove_path}[options["mode"]]
        try:
            return operation(options, Output(sys.stdout))
        except Closed:
            silence_stdout()
            return 0
    if options["mode"] == "tree":
        watcher = TreeWatcher(options, Output(sys.stdout))
    else:
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
