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
from keepwatch.ctx import Ledger
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
        "-o",
        "IdentitiesOnly=yes",
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
        "rescan": 30.0,
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


def test_skip_ledger_files_are_not_reported(tmp_path, make_watch, xdg):
    remote_dir = remote_dir_with_file(tmp_path)
    (remote_dir / "b.tar.gz").write_bytes(b"more")
    age(remote_dir / "b.tar.gz")
    done = remote_dir / "a.tar.gz"
    info = done.stat()
    key = f"{os.path.join(os.path.abspath(remote_dir), 'a.tar.gz')}|{info.st_size}|{info.st_mtime!r}"
    Ledger(xdg.watch_data_dir("w") / "ledgers" / "pulled.json").add(key)
    observer, events, records = remote_observer(make_watch, xdg, remote_dir, "skip_ledger = 'pulled'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
        time.sleep(2.5)
    finally:
        stop(observer)
    assert [event["name"] for event in events] == ["b.tar.gz"]
