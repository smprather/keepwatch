import hashlib
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


def test_recursive_skips_ignored_directories(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    for directory in ("sub", ".git/objects/3f", ".stversions", "partial.tmp"):
        (inbox / directory).mkdir(parents=True)
    for relative in ("sub/a.gz", ".git/objects/3f/a1b2c3", ".stversions/old.gz", "partial.tmp/b.gz"):
        (inbox / relative).write_text("x")
        age(inbox / relative)
    observer, events, records = files_observer(make_watch, xdg, inbox, "recursive = true\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.gz"]


def write_marker(directory, name, digest=None):
    data = (directory / name).read_bytes()
    marker = directory / f"{name}.sha256"
    marker.write_text(f"{digest or hashlib.sha256(data).hexdigest()}  {name}\n", encoding="utf-8")
    age(marker)
    return marker


def test_marker_mode_waits_for_a_matching_marker(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        time.sleep(2.5)
        assert events == []  # no marker yet
        write_marker(inbox, "a.tar.gz")
        assert wait_for(lambda: events)
        time.sleep(2.0)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["a.tar.gz"]  # the marker itself is never reported
    assert events[0]["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert Path(events[0]["marker"]) == inbox / "a.tar.gz.sha256"


def test_marker_mismatch_warns_once_then_a_reupload_is_reported(tmp_path, make_watch, xdg):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    write_marker(inbox, "a.tar.gz", digest="0" * 64)
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.output"))
        time.sleep(3.0)
        assert events == []
        write_marker(inbox, "a.tar.gz")
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    warnings = [r for r in kinds(records, "observer.output") if "does not match" in r["text"]]
    assert len(warnings) == 1


def test_a_marker_that_arrives_late_is_noticed_within_seconds(tmp_path, make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "FILES_RESCAN", 600.0)  # no idle rescan to rescue it
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n", settle="3s")
    observer.start()
    try:
        time.sleep(1.5)  # the data is now a candidate; its settle check comes at about 3s
        marker = inbox / "a.tar.gz.sha256"
        marker.write_text(f"{hashlib.sha256(b'payload').hexdigest()}  a.tar.gz\n", encoding="utf-8")
        # Fresh: still unsettled when the data's check comes, and no filesystem event follows it.
        assert wait_for(lambda: events, timeout=10.0)
    finally:
        stop(observer)


def test_a_file_without_a_marker_costs_no_rescans(tmp_path, make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "FILES_RESCAN", 600.0)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.tar.gz").write_bytes(b"payload")
    age(inbox / "a.tar.gz")
    scans = []
    scan = observers.FilesObserver._scan
    monkeypatch.setattr(observers.FilesObserver, "_scan", lambda self: scans.append(1) or scan(self))
    observer, events, records = files_observer(make_watch, xdg, inbox, "marker = 'sha256'\n")
    observer.start()
    try:
        time.sleep(2.5)  # settled (settle is 1s) with no marker
        before = len(scans)
        time.sleep(3.0)
        assert len(scans) == before  # held until the file or its marker changes, not re-checked every second
        write_marker(inbox, "a.tar.gz")  # its marker is still noticed
        assert wait_for(lambda: events, timeout=8.0)
    finally:
        stop(observer)
