"""keepwatch's remote watcher: reports settled files in one directory as JSON lines on stdout.

keepwatch sends this file over ssh to the remote host's Python and runs it there; nothing is installed.
It must stay compatible with Python 3.6, use only the standard library and stay ASCII (the list of newer
features to avoid is in tests/test_remote_watcher.py).

Options are one JSON object: the first command-line argument, or KEEPWATCH_REMOTE_ARGS when keepwatch
prepends that assignment to the source it sends on stdin (so nothing passes through the remote login
shell's quoting rules). With inotify the directory is rescanned on every notification and every
"rescan" seconds; without it every "interval" seconds.

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
import select
import socket
import stat
import sys
import time

# Do not import `platform` (or any name keepwatch uses for a module): run as a script from src/keepwatch/,
# keepwatch's own platform.py would shadow the standard library's.
WATCHER_VERSION = 1
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
        self.unreadable = {}  # (path, size, mtime_ns) -> when reading it last failed
        self.noted = set()
        self.skip = set(options["skip"])  # "path|size|mtime" keys the caller has already handled

    def note(self, message):
        """Tell keepwatch about a problem once (stderr lines become observer.output records)."""
        if message in self.noted:
            return
        self.noted.add(message)
        sys.stderr.write("keepwatch remote watcher: " + message + "\n")
        sys.stderr.flush()

    def scan(self):
        """Report settled files. Returns True while some file is still settling. Raises OSError if the directory is gone."""
        settle = self.options["settle"]
        names = os.listdir(self.directory)
        now = time.monotonic()
        wall = time.time()
        present = set()
        current = set()
        for raw in sorted(names):
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                # keepwatch could not name it to scp, log it or hand it to a hook as text.
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
    sys.stderr.write("keepwatch remote watcher on " + socket.gethostname() + ": " + message + "\n")
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
