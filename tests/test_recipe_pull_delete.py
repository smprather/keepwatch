import hashlib
import os
import shutil
import time
from pathlib import Path

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.ctx import Ledger
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import PY, literal, toml_path
from scp_server import ScpServer

pytestmark = [pytest.mark.posix_only, pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")]
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


@pytest.fixture
def linux1(tmp_path):
    root = tmp_path / "linux1"
    root.mkdir()
    (tmp_path / "keys").mkdir()
    server = ScpServer(root, tmp_path / "keys", chroot=False).start()
    yield server
    server.stop()


def sweep_watch(make_watch, server, tmp_path, extra=""):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return load_watch_config(
        make_watch(
            "sweep",
            config=(
                'recipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\n'
                f"remote_dir = {toml_path(server.root)}\nlocal_dir = {toml_path(tmp_path / 'stage')}\n"
                f"port = {server.port}\nidentity = {toml_path(server.client_key)}\n"
                f"known_hosts = {toml_path(server.known_hosts)}\nremote_python = {literal(PY)}\n"
                f"ssh_command = [{literal(PY)}, {toml_path(FAKE_SSH)}]\n"
                f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\n"
                f"delete_remote = true\n{extra}"
            ),
        )
    )


def file_event(path):
    info = path.stat()
    return {
        "event": "file",
        "path": str(path),
        "name": path.name,
        "size": info.st_size,
        "mtime": info.st_mtime,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "observer": "remote",
        "received": "2026-10-02T00:00:00.000+00:00",
    }


def poll(xdg, watch, events=(), state=None, records=None):
    global_config = load_global_config(xdg.config_file, xdg)
    sink = records.append if records is not None else (lambda record: None)
    engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=sink, pid=os.getpid())
    return engine.poll(watch, state or WatchState(False), events=list(events))


def ledger(xdg, name):
    return Ledger(xdg.watch_data_dir("sweep") / "ledgers" / f"{name}.json")


def test_a_verified_pull_deletes_the_source(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [file_event(source)], records=records)
    assert report.outcome is Outcome.TRUE and not report.failed
    assert (tmp_path / "stage" / "a.tar.gz").read_bytes() == b"data"
    assert not source.exists()
    assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 0  # done: out of the skip list
    assert len(ledger(xdg, "skipped")) == 1
    messages = [r["message"] for r in records if r["event"] == "plugin.log"]
    assert any(m.startswith("deleted a.tar.gz from u@127.0.0.1") for m in messages)


def test_a_failed_delete_is_retried_after_delete_retry(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    watch = sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '1s'\n")
    os.chmod(linux1.root, 0o500)
    try:
        first = poll(xdg, watch, [file_event(source)])
        assert first.outcome is Outcome.TRUE and not first.failed
        assert source.exists() and len(ledger(xdg, "to_delete")) == 1
        assert poll(xdg, watch, [], state=first.after).outcome is Outcome.FALSE  # not due yet
    finally:
        os.chmod(linux1.root, 0o700)
    time.sleep(1.2)
    retry = poll(xdg, watch, [], state=first.after)
    assert retry.outcome is Outcome.TRUE and not retry.failed
    assert not source.exists() and len(ledger(xdg, "to_delete")) == 0


def test_a_file_changed_before_its_pull_does_not_fail_the_poll(linux1, make_watch, tmp_path, xdg):
    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    event = file_event(source)
    source.write_bytes(b"changed after the observer reported it")
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [event], records=records)
    assert not report.failed
    assert source.exists() and len(ledger(xdg, "pulled")) == 0 and len(ledger(xdg, "to_delete")) == 0
    assert any("changed on u@127.0.0.1 after it was reported" in r["message"] for r in records if r["event"] == "plugin.log")


def test_a_changed_source_is_kept(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "a.tar.gz"
    source.write_bytes(b"data")
    before = source.stat()
    event = file_event(source)
    key = pull.event_key(event)
    source.write_bytes(b"datb")  # same size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))  # the exact same mtime: only the content differs
    assert source.stat().st_mtime == event["mtime"]
    ledger(xdg, "pulled").add(key)
    ledger(xdg, "to_delete").add(pull.queue_key(key, event["sha256"]))
    records = []
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n"), [], records=records)
    assert not report.failed and source.read_bytes() == b"datb"
    assert len(ledger(xdg, "to_delete")) == 0 and len(ledger(xdg, "pulled")) == 0
    assert any("changed on u@127.0.0.1 since it was pulled" in r["message"] for r in records if r["event"] == "plugin.log")


def test_files_pulled_before_delete_remote_are_deleted_too(linux1, make_watch, tmp_path, xdg):
    from keepwatch.recipes import pull

    source = linux1.root / "old.tar.gz"
    source.write_bytes(b"old")
    ledger(xdg, "pulled").add(pull.event_key(file_event(source)))  # pulled while delete_remote was off
    report = poll(xdg, sweep_watch(make_watch, linux1, tmp_path), [])
    assert report.outcome is Outcome.TRUE and not report.failed
    assert not source.exists()


def test_a_refused_delete_is_kept_and_not_retried(linux1, make_watch, tmp_path, xdg):
    if os.name == "nt":
        pytest.skip("symlinks")
    target = tmp_path / "elsewhere.tar.gz"
    target.write_bytes(b"x")
    link = linux1.root / "link.tar.gz"
    link.symlink_to(target)
    info = os.lstat(link)
    key = f"{link}|{info.st_size}|{info.st_mtime!r}"
    ledger(xdg, "pulled").add(key)
    watch = sweep_watch(make_watch, linux1, tmp_path, "delete_retry = '0s'\n")
    first = poll(xdg, watch, [])
    assert not first.failed and link.is_symlink() and target.exists()
    assert key in ledger(xdg, "kept") and key in ledger(xdg, "pulled")
    assert poll(xdg, watch, [], state=first.after).outcome is Outcome.FALSE  # nothing left to do
