import hashlib
import os
import shutil
import time

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


def pull_watch(make_watch, server, tmp_path, extra=""):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return make_watch(
        "relay-pull",
        config=(
            'recipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\nremote_dir = "/"\n'
            f"local_dir = {toml_path(tmp_path / 'stage')}\nport = {server.port}\n"
            f"identity = {toml_path(server.client_key)}\nknown_hosts = {toml_path(server.known_hosts)}\n"
            f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\n{extra}"
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


def test_a_failing_file_does_not_stop_the_rest_of_the_queue(make_watch, server, tmp_path, xdg):
    """One unreachable file must not starve the queue, and the poll still fails so it is retried (issue 5)."""
    (server.root / "good.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    events = [file_event("missing.tar.gz", b"gone"), file_event("good.tar.gz", b"data")]
    report = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert report.failed  # reported, so the next poll retries
    assert (tmp_path / "stage" / "good.tar.gz").read_bytes() == b"data"  # the queue carried on
    messages = [r.get("message", "") for r in records if r["event"] == "plugin.log"]
    assert any("the rest of the queue is still tried" in message for message in messages), messages


def test_pull_recipe_resumes_a_partial_file(make_watch, server, tmp_path, xdg):
    """With resume = true an unfinished .NAME.part is continued with sftp, not restarted (issue 4)."""
    data = bytes(range(256)) * 40  # 10240 bytes
    (server.root / "big.bin").write_bytes(data)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / ".big.bin.part").write_bytes(data[:4096])
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path,
                                         extra='protocol = "sftp"\nresume = true\n'))
    server.reads.clear()
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("big.bin", data)])
    assert not report.failed
    assert (stage / "big.bin").read_bytes() == data
    assert not (stage / ".big.bin.part").exists()
    assert server.reads and all(offset >= 4096 for offset in server.reads), server.reads


def test_pull_recipe_discards_a_partial_file_without_resume(make_watch, server, tmp_path, xdg):
    """Without resume the partial goes, and the log says what that cost (issue 4)."""
    data = b"data" * 1000
    (server.root / "big.bin").write_bytes(data)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / ".big.bin.part").write_bytes(b"x" * 2048)
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    report = engine(xdg, records).poll(watch, WatchState(False), events=[file_event("big.bin", data)])
    assert not report.failed
    assert (stage / "big.bin").read_bytes() == data
    messages = [r.get("message", "") for r in records if r["event"] == "plugin.log"]
    assert any("discarded 2 KiB" in message and "big.bin" in message for message in messages), messages


def test_pull_recipe_deletes_stale_part_files_after_a_successful_pull(make_watch, server, tmp_path, xdg):
    """part_max_age sweeps the staging folder: a stale .part from a killed transfer goes (issue 4)."""
    (server.root / "a.bin").write_bytes(b"data")
    stage = tmp_path / "stage"
    stage.mkdir()
    stale = stage / ".old.bin.part"
    stale.write_bytes(b"x" * 100)
    old = time.time() - 8 * 86400
    os.utime(stale, (old, old))
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("a.bin", b"data")])
    assert not report.failed
    assert not stale.exists()


def test_pull_recipe_does_not_record_a_mismatched_pull(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    report = engine(xdg, records).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"data", sha="0" * 64)])
    assert not report.failed
    assert not list((tmp_path / "stage").iterdir())
    assert len(Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "pulled.json")) == 0
    assert any("after it was reported" in r["message"] for r in records if r["event"] == "plugin.log")


def test_pull_recipe_ignores_other_events(make_watch, server, tmp_path, xdg):
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[{"line": "noise", "observer": "remote"}])
    assert report.outcome is Outcome.FALSE


def test_validate_a_recipe_watch(make_watch, server, tmp_path, xdg):
    pull_watch(make_watch, server, tmp_path)
    result = CliRunner().invoke(cli, ["validate", "relay-pull"])
    assert result.exit_code == 0, result.output


def test_pull_recipe_renames_on_a_name_clash(make_watch, server, tmp_path, xdg):
    (server.root / "a.tar.gz").write_bytes(b"new data")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "a.tar.gz").write_bytes(b"old, not pushed yet")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"new data")])
    assert not report.failed
    assert (stage / "a-1.tar.gz").read_bytes() == b"new data"
    assert (stage / "a.tar.gz").read_bytes() == b"old, not pushed yet"
