import hashlib
import os
import shutil
import socket
import time

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import toml_path
from scp_server import ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def staged(tmp_path, name="c.tar.gz", data=b"payload"):
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    path = stage / name
    path.write_bytes(data)
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def push_watch(make_watch, server, tmp_path, extra="", settle="1s"):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return load_watch_config(
        make_watch(
            "relay-push",
            config=(
                f'recipe = "push"\n[settings]\nlocal_dir = {toml_path(tmp_path / "stage")}\ndest = "u@127.0.0.1:"\n'
                f"port = {server.port}\nidentity = {toml_path(server.client_key)}\n"
                f"known_hosts = {toml_path(server.known_hosts)}\n"
                f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\nsettle = '{settle}'\n{extra}"
            ),
        )
    )


def poll(xdg, watch, state=None):
    global_config = load_global_config(xdg.config_file, xdg)
    engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=lambda record: None, pid=os.getpid())
    return engine.poll(watch, state or WatchState(False))


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_push_recipe_uploads_with_a_marker_then_deletes(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    report = poll(xdg, push_watch(make_watch, server, tmp_path))
    assert report.outcome is Outcome.TRUE and not report.failed
    assert (server.root / "c.tar.gz").read_bytes() == b"payload"
    digest = hashlib.sha256(b"payload").hexdigest()
    assert (server.root / "c.tar.gz.sha256").read_text(encoding="utf-8") == f"{digest}  c.tar.gz\n"
    assert not path.exists()


def test_unreachable_destination_is_unknown(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    watch = push_watch(make_watch, server, tmp_path, f"reachable_port = {closed_port()}\nreachable_timeout = '2s'\n")
    report = poll(xdg, watch)
    assert report.outcome is Outcome.UNKNOWN and not report.failed
    assert "not reachable" in report.reason
    assert path.exists() and not (server.root / "c.tar.gz").exists()


def test_unsettled_files_wait(make_watch, server, tmp_path, xdg):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "fresh.tar.gz").write_bytes(b"x")
    (stage / ".partial.part").write_bytes(b"x")
    report = poll(xdg, push_watch(make_watch, server, tmp_path, settle="1h"))
    assert report.outcome is Outcome.FALSE


def test_archive_moves_and_prunes(make_watch, server, tmp_path, xdg):
    archive = tmp_path / "archive"
    archive.mkdir()
    ancient = archive / "ancient.tar.gz"
    ancient.write_bytes(b"old")
    os.utime(ancient, (1, 1))
    path = staged(tmp_path)
    extra = f'after = "archive"\narchive_dir = {toml_path(archive)}\nkeep_for = "1d"\n'
    report = poll(xdg, push_watch(make_watch, server, tmp_path, extra))
    assert not report.failed
    assert not path.exists() and (archive / "c.tar.gz").read_bytes() == b"payload"
    assert not ancient.exists()


def test_keep_remembers_what_was_pushed(make_watch, server, tmp_path, xdg):
    path = staged(tmp_path)
    watch = push_watch(make_watch, server, tmp_path, 'after = "keep"\nmarker = "none"\n')
    first = poll(xdg, watch)
    assert first.outcome is Outcome.TRUE and path.exists()
    assert sorted(p.name for p in server.root.iterdir()) == ["c.tar.gz"]
    assert poll(xdg, watch, first.after).outcome is Outcome.FALSE
