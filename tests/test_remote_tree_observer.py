"""The remote_tree observer: the watcher's tree mode over the fake ssh, its expected state from a checkpoint.

The kind is internal (a recipe declares it), so these tests build the ObserverConfig by hand rather than through
a watch's `[observe.*]` table.
"""

import json
from typing import Any, cast

import pytest

from portable import PY
from test_remote_files_observer import FAKE_SSH, age, kinds, stop, wait_for
from watchglass import observers
from watchglass.config import ObserverConfig, load_watch_config
from watchglass.ctx import Checkpoint, checkpoint_file
from watchglass.observers import RemoteTreeObserver, _remote_path, build_observer

WATCH_CONFIG = "[observe.local]\nkind = 'files'\npath = '.'\n"  # a valid watch; the observer under test is built by hand


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


class FakeProcess(object):
    """Stands in for a running ssh: reconnect() only has to terminate it."""

    def __init__(self, terminated):
        self.terminated = terminated

    def terminate(self):
        self.terminated.append(True)


def tree_observer(make_watch, xdg, remote_dir, *, checkpoint=None, deliver=None, settle=1.0):
    watch = load_watch_config(make_watch("w", config=WATCH_CONFIG))
    data_dir = xdg.watch_data_dir("w")
    if checkpoint is not None:
        Checkpoint(checkpoint_file(data_dir, "mirror")).save(checkpoint)
    config = ObserverConfig(
        name="remote",
        kind="remote_tree",
        remote="me@linux1.example",
        dir=str(remote_dir),
        settle=settle,
        interval=1.0,
        remote_python=PY,
        ssh_command=(PY, str(FAKE_SSH)),
        skip_checkpoint="mirror",
    )
    events, records = [], []
    built = build_observer(
        watch,
        config,
        deliver=deliver or (lambda event: events.append(event) or 0),
        sink=records.append,
        environment={},
        data_dir=data_dir,
    )
    assert isinstance(built, RemoteTreeObserver)
    return built, events, records


def options_of(observer):
    """The WATCHGLASS_REMOTE_ARGS the observer sends the watcher on stdin, as the watcher parses them."""
    assert observer.config.stdin is not None
    first = observer.config.stdin.split("\n", 1)[0]
    return json.loads(json.loads(first.split(" = ", 1)[1]))


def test_expected_paths_follow_the_watchers_own_separator():
    """The watcher walks with os.path.join on its host: a Windows tree gets backslashes, a POSIX tree slashes.

    Windows CI caught the hardcoded "/": with the wrong separator the expected state never matched the walk, so
    every file of a checkpointed tree looked new again on every poll.
    """
    assert _remote_path("C:\\tree", "sub/a.txt") == "C:\\tree\\sub\\a.txt"
    assert _remote_path("/data/tree", "sub/a.txt") == "/data/tree/sub/a.txt"
    assert _remote_path("/", "a.txt") == "/a.txt"
    assert _remote_path("/data/tree/", "a.txt") == "/data/tree/a.txt"


def test_tree_options_come_from_the_checkpoint(make_watch, xdg, tmp_path):
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    observer, _, _ = tree_observer(
        make_watch,
        xdg,
        remote_dir,
        checkpoint={"files": {"a.tar.gz": [4, 1700000000.0]}, "dirs": ["sub"]},
    )
    options = options_of(observer)
    assert options["mode"] == "tree"
    assert options["dir"] == str(remote_dir)
    assert options["expected_files"] == [
        {"path": str(remote_dir / "a.tar.gz"), "size": 4, "mtime": 1700000000.0}
    ]
    assert options["expected_dirs"] == [str(remote_dir / "sub")]


def test_tree_options_without_a_checkpoint_are_empty(make_watch, xdg, tmp_path):
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    observer, _, _ = tree_observer(make_watch, xdg, remote_dir)
    options = options_of(observer)
    assert options["expected_files"] == [] and options["expected_dirs"] == []


def test_the_checkpoint_is_reread_at_every_connect(make_watch, xdg, tmp_path, monkeypatch):
    """An action promotes the checkpoint while the connection is open, so a reconnect must see the new one."""
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    observer, _, _ = tree_observer(make_watch, xdg, remote_dir)
    assert options_of(observer)["expected_files"] == []
    monkeypatch.setattr(observers.CommandObserver, "run_once", lambda _self: None)  # no connect, just the rebuild
    Checkpoint(checkpoint_file(xdg.watch_data_dir("w"), "mirror")).save({"files": {"later.txt": [1, 5.0]}, "dirs": []})
    observer.run_once()
    assert options_of(observer)["expected_files"] == [
        {"path": str(remote_dir / "later.txt"), "size": 1, "mtime": 5.0}
    ]


def test_tree_observer_reports_an_unchanged_expected_file_by_silence(make_watch, xdg, tmp_path):
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    path = remote_dir / "a.tar.gz"
    path.write_bytes(b"data")
    age(path)
    observer, events, records = tree_observer(
        make_watch, xdg, remote_dir, checkpoint={"files": {"a.tar.gz": [4, path.stat().st_mtime]}, "dirs": []}
    )
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.ready"))
        assert events == []  # the checkpoint said this file was already handled
    finally:
        stop(observer)


def test_tree_observer_delivers_a_changed_file_and_a_gone_one(make_watch, xdg, tmp_path):
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    path = remote_dir / "a.tar.gz"
    path.write_bytes(b"data")
    age(path)
    observer, events, records = tree_observer(
        make_watch, xdg, remote_dir, checkpoint={"files": {"a.tar.gz": [4, path.stat().st_mtime]}, "dirs": []}
    )
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.ready"))
        path.write_bytes(b"more data")
        assert wait_for(lambda: kinds(events, "files"))
        changed = kinds(events, "files")[0]
        assert changed["entries"][0][0] == str(path)
        assert changed["entries"][0][1] == 9
        assert changed["observer"] == "remote" and changed["received"]
        path.unlink()
        assert wait_for(lambda: kinds(events, "gone"))
        assert str(path) in kinds(events, "gone")[0]["paths"]
    finally:
        stop(observer)


def test_a_dropped_event_reconnects_the_watcher(make_watch, xdg, tmp_path):
    """A hole in the stream would let a check infer the wrong thing, so the observer drops the connection."""
    remote_dir = tmp_path / "out"
    remote_dir.mkdir()
    terminated = []
    observer, _, records = tree_observer(make_watch, xdg, remote_dir, deliver=lambda _event: 2)
    observer._process = cast(Any, FakeProcess(terminated))
    observer.emit({"event": "files", "entries": [[str(remote_dir / "a"), 1, 2.0]]})
    assert terminated == [True]
    assert any("queue dropped" in json.dumps(record, default=str) for record in records)


def test_remote_tree_is_not_a_user_observer_kind():
    """The mirror recipe declares this observer itself: no watch config may name it (yet)."""
    from watchglass import config

    assert "remote_tree" not in config._OBSERVER_KIND_KEYS
