import os
import time
from pathlib import Path

import pytest

from keepwatch import observers
from keepwatch.config import load_watch_config
from keepwatch.observers import FilesObserver, build_observer
from portable import toml_path


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


def wait_for(predicate, timeout=15.0):
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


def files_observer(make_watch, xdg, inbox, extra="", settle="1s"):
    config = f"[observe.src]\nkind = 'files'\npath = {toml_path(inbox)}\nsettle = '{settle}'\n{extra}"
    watch = load_watch_config(make_watch("w", config=config))
    events, records = [], []
    observer = build_observer(
        watch,
        watch.observers["src"],
        deliver=events.append,
        sink=records.append,
        environment={},
        data_dir=xdg.watch_data_dir("w"),
    )
    return observer, events, records


def stop(observer):
    observer.stop()
    assert observer.join(15)


def test_reports_settled_files_once(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "old.gz").write_text("x")
    age(inbox / "old.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox)
    assert isinstance(observer, FilesObserver)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 1)
        (inbox / "new.gz").write_text("yy")
        assert wait_for(lambda: len(events) == 2)
        time.sleep(2.5)
        assert len(events) == 2
    finally:
        stop(observer)
    first = events[0]
    assert (first["event"], first["name"], first["size"], first["observer"]) == ("file", "old.gz", 1, "src")
    assert Path(first["path"]) == inbox / "old.gz"
    assert isinstance(first["mtime"], float)
    assert (events[1]["name"], events[1]["size"]) == ("new.gz", 2)
    started = kinds(records, "observer.started")[0]
    assert started["kind"] == "files" and started["native"] is True


def test_ignored_names_are_never_reported(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.gz", ".hidden", "x.tmp", "y.part", "z~"):
        (inbox / name).write_text("x")
        age(inbox / name)
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]


def test_pattern(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("a.gz", "b.txt"):
        (inbox / name).write_text("x")
        age(inbox / name)
    observer, events, records = files_observer(make_watch, xdg, inbox, "pattern = '*.gz'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]


def test_a_file_still_growing_is_not_reported_early(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    target = inbox / "copy.gz"
    target.write_text("x")
    age(target)  # like cp -p: the copy carries an old modification time
    observer, events, records = files_observer(make_watch, xdg, inbox, settle="2s")
    observer.start()
    try:
        time.sleep(0.5)
        with target.open("a") as handle:
            handle.write("y")
        age(target)
        assert wait_for(lambda: events)
        time.sleep(1.0)
    finally:
        stop(observer)
    assert [event["size"] for event in events] == [2]


def test_recursive(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "a.gz").write_text("x")
    age(inbox / "sub" / "a.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "recursive = true\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert Path(events[0]["path"]) == inbox / "sub" / "a.gz"


def test_not_recursive_by_default(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "a.gz").write_text("x")
    age(inbox / "sub" / "a.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.started"))
        time.sleep(2.0)
    finally:
        stop(observer)
    assert events == []


def test_missing_directory_is_retried(tmp_path, make_watch, xdg):
    inbox = tmp_path / "later"
    observer, events, records = files_observer(make_watch, xdg, inbox)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
        inbox.mkdir()
        (inbox / "a.gz").write_text("x")
        age(inbox / "a.gz")
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert "does not exist" in first["reason"] and first["level"] == "WARNING"
    assert events[0]["name"] == "a.gz"
    assert kinds(records, "observer.stopped")[-1]["reason"] == "stopped"
