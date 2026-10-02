import hashlib
import os
import shutil

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.config import load_global_config, load_watch_config
from keepwatch.ctx import Ledger
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


def pull_watch(make_watch, server, tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return make_watch(
        "relay-pull",
        config=(
            'recipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\nremote_dir = "/"\n'
            f"local_dir = {toml_path(tmp_path / 'stage')}\nport = {server.port}\n"
            f"identity = {toml_path(server.client_key)}\nknown_hosts = {toml_path(server.known_hosts)}\n"
            f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\n"
        ),
    )


def file_event(name, data, sha=None):
    return {
        "event": "file",
        "path": f"/{name}",
        "name": name,
        "size": len(data),
        "mtime": 1790000000.5,
        "sha256": sha or hashlib.sha256(data).hexdigest(),
        "remote": "u@127.0.0.1",
        "observer": "remote",
        "received": "2026-10-02T00:00:00.000+00:00",
    }


def engine(xdg, records):
    global_config = load_global_config(xdg.config_file, xdg)
    return PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())


def test_pull_recipe_pulls_new_files_once(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    events = [file_event("a.tar.gz", b"data")]
    first = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert first.outcome is Outcome.TRUE and not first.failed, records
    assert (tmp_path / "stage" / "a.tar.gz").read_bytes() == b"data"
    assert "/a.tar.gz|4|1790000000.5" in Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")
    targets = [r["target"] for r in records if r["event"] == "hook.end"]
    assert targets == ["recipe pull:check", "recipe pull:on_true"]
    second = engine(xdg, []).poll(watch, first.after, events=events)
    assert second.outcome is Outcome.FALSE


def test_pull_recipe_does_not_record_a_failed_pull(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"data", sha="0" * 64)])
    assert report.failed
    assert not list((tmp_path / "stage").iterdir())
    assert len(Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")) == 0


def test_pull_recipe_ignores_other_events(make_watch, server, tmp_path, xdg):
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[{"line": "noise", "observer": "remote"}])
    assert report.outcome is Outcome.FALSE


def test_validate_a_recipe_watch(make_watch, server, tmp_path, xdg):
    pull_watch(make_watch, server, tmp_path)
    result = CliRunner().invoke(cli, ["validate", "relay-pull"])
    assert result.exit_code == 0, result.output
