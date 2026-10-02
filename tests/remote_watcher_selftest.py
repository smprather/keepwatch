"""Tests for keepwatch's remote watcher. Python 3.6+ and the standard library only.

CI runs this file directly under Python 3.6 (`python tests/remote_watcher_selftest.py -v`); the main suite
imports WatcherTests (tests/test_remote_watcher.py), so it also runs on every supported Python and OS.
"""

import atexit
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
SOURCE = os.path.normpath(os.path.join(HERE, os.pardir, "src", "keepwatch", "remote_watcher.py"))
FAST = {"settle": 1, "interval": 0.2, "heartbeat": 30}
_COPY = []


def watcher():
    """The watcher copied into a directory of its own, as it runs in production (from stdin, away from keepwatch).

    Run in place, src/keepwatch/ would be first on sys.path and keepwatch's modules could shadow the stdlib.
    """
    if not _COPY:
        directory = tempfile.mkdtemp(prefix="keepwatch-watcher-")
        atexit.register(shutil.rmtree, directory, True)
        target = os.path.join(directory, "remote_watcher.py")
        shutil.copyfile(SOURCE, target)
        _COPY.append(target)
    return _COPY[0]


def age(path, seconds=60):
    old = time.time() - seconds
    os.utime(path, (old, old))


class Running(object):
    """A watcher subprocess whose stdout lines are parsed as they arrive."""

    def __init__(self, options, stdin_mode=False, env=None):
        if stdin_mode:
            with open(SOURCE, "rb") as handle:
                source = handle.read()
            prefix = "KEEPWATCH_REMOTE_ARGS = " + json.dumps(json.dumps(options)) + "\n"
            argv = [sys.executable, "-u", "-"]
            data = prefix.encode("ascii") + source
        else:
            argv = [sys.executable, "-u", watcher(), json.dumps(options)]
            data = b""
        self.stopped = False
        self.stderr_text = b""
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
        if self.stopped:
            return
        self.stopped = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(10)
        self.reader.join(5)
        self.stderr_text = self.process.stderr.read()
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
            [sys.executable, watcher(), json.dumps(options)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
            [sys.executable, "-u", watcher(), json.dumps(options)],
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

    @unittest.skipIf(os.name == "nt" or sys.platform == "darwin", "needs a filesystem that accepts any name bytes")
    def test_non_utf8_names_are_skipped_with_a_note(self):
        path = os.path.join(self.dir.encode("utf-8"), b"caf\xe9.gz")
        with open(path, "wb") as handle:
            handle.write(b"x")
        age(path)
        self.write("good.gz")
        running = self.start(heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["good.gz"])
        running.stop()
        self.assertEqual(running.stderr_text.count(b"not UTF-8"), 1)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a POSIX user that is not root"
    )
    def test_unreadable_file_is_noted_once(self):
        locked = self.write("locked.gz")
        os.chmod(locked, 0)
        self.write("ok.gz")
        running = self.start(heartbeat=0.5)
        try:
            names = running.file_names_for(3.0)
        finally:
            os.chmod(locked, 0o600)
        self.assertEqual(names, ["ok.gz"])
        running.stop()
        self.assertEqual(running.stderr_text.count(b"cannot read locked.gz"), 1)

    def test_rescan_option_is_accepted(self):
        self.write("a.gz")
        self.assertEqual(self.start(rescan=60).next_file()["name"], "a.gz")

    def test_skipped_files_are_not_reported(self):
        done = self.write("done.gz")
        self.write("new.gz")
        key = "%s|%d|%r" % (os.path.abspath(done), os.stat(done).st_size, os.stat(done).st_mtime)
        running = self.start(skip=[key], heartbeat=0.5)
        self.assertEqual(running.file_names_for(3.0), ["new.gz"])

    def delete(self, files, directory=None):
        options = {"mode": "delete", "dir": directory or self.dir, "files": files}
        code, out, err = self.run_once(options)
        lines = [json.loads(line) for line in out.decode("ascii").splitlines()]
        self.assertEqual(code, 0, err)
        self.assertEqual(lines[-1], {"event": "done"})
        return {line["path"]: line for line in lines[:-1]}

    def item(self, path):
        info = os.stat(path)
        return {"path": os.path.abspath(path), "size": info.st_size, "mtime": info.st_mtime}

    def test_delete_removes_an_unchanged_file(self):
        path = self.write("a.gz")
        results = self.delete([self.item(path)])
        self.assertEqual(results[os.path.abspath(path)]["event"], "deleted")
        self.assertFalse(os.path.exists(path))

    def test_delete_keeps_a_changed_file(self):
        path = self.write("a.gz")
        pulled = self.item(path)
        with open(path, "ab") as handle:
            handle.write(b"more")
        self.assertEqual(self.delete([pulled])[pulled["path"]]["event"], "changed")
        self.assertTrue(os.path.exists(path))

    def test_delete_reports_a_missing_file_as_gone(self):
        path = os.path.join(self.dir, "never.gz")
        results = self.delete([{"path": path, "size": 1, "mtime": 1.5}])
        self.assertEqual(results[path]["event"], "gone")

    def test_delete_refuses_outside_dir_and_symlinks(self):
        outside_dir = tempfile.mkdtemp()
        try:
            outside = os.path.join(outside_dir, "x.gz")
            with open(outside, "wb") as handle:
                handle.write(b"x")
            results = self.delete([self.item(outside)])
            self.assertEqual(results[os.path.abspath(outside)]["event"], "refused")
            self.assertTrue(os.path.exists(outside))
            if hasattr(os, "symlink") and os.name != "nt":
                link = os.path.join(self.dir, "link.gz")
                os.symlink(outside, link)
                info = os.lstat(link)
                item = {"path": os.path.abspath(link), "size": info.st_size, "mtime": info.st_mtime}
                self.assertEqual(self.delete([item])[item["path"]]["event"], "refused")
                self.assertTrue(os.path.exists(outside))
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    @unittest.skipIf(
        os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a POSIX user that is not root"
    )
    def test_delete_reports_a_failure(self):
        path = self.write("a.gz")
        os.chmod(self.dir, 0o500)
        try:
            results = self.delete([self.item(path)])
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(results[os.path.abspath(path)]["event"], "failed")
        self.assertIn("ermission", results[os.path.abspath(path)]["reason"])

    def test_delete_keeps_a_same_size_same_mtime_rewrite(self):
        path = self.write("a.gz", b"one")
        before = os.stat(path)
        pulled = self.item(path)
        pulled["sha256"] = hashlib.sha256(b"one").hexdigest()
        with open(path, "wb") as handle:
            handle.write(b"two")  # same size
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # and the exact same mtime, as cp -p leaves it
        self.assertEqual(os.stat(path).st_mtime, pulled["mtime"])
        self.assertEqual(self.delete([pulled])[pulled["path"]]["event"], "changed")
        self.assertTrue(os.path.exists(path))

    def test_delete_with_a_matching_sha256(self):
        path = self.write("a.gz", b"one")
        item = self.item(path)
        item["sha256"] = hashlib.sha256(b"one").hexdigest()
        self.assertEqual(self.delete([item])[item["path"]]["event"], "deleted")

    def test_delete_results_carry_their_index(self):
        first = self.write("a.gz")
        second = os.path.join(self.dir, "never.gz")
        options = {"mode": "delete", "dir": self.dir, "files": [self.item(first), {"path": second, "size": 1, "mtime": 1.5}]}
        code, out, err = self.run_once(options)
        lines = [json.loads(line) for line in out.decode("ascii").splitlines()]
        self.assertEqual([(line["index"], line["event"]) for line in lines[:-1]], [(0, "deleted"), (1, "gone")])


if __name__ == "__main__":
    unittest.main()
