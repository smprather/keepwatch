import hashlib
import json
import logging
import os
import shutil
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from portable import toml_path
from scp_server import ScpServer
from watchglass.cli import cli
from watchglass.config import load_global_config, load_watch_config
from watchglass.ctx import Ledger
from watchglass.pollengine import PollEngine
from watchglass.recipes.pull import cleanup_parts, event_key
from watchglass.runner import Runner
from watchglass.state import Outcome, WatchState

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


def test_pull_recipe_headlines_the_story(make_watch, server, tmp_path, xdg):
    """found -> pulling -> pulled, in plain language, without needing to know recipe internals."""
    (server.root / "a.tar.gz").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    records = []
    report = engine(xdg, records).poll(watch, WatchState(False), events=[file_event("a.tar.gz", b"data")])
    assert not report.failed, records
    headlines = [r["message"] for r in records if r["event"] == "plugin.event"]
    assert any(m.startswith("found a.tar.gz (4 B) on u@127.0.0.1") for m in headlines), headlines
    assert any(m.startswith("pulling a.tar.gz (4 B) from u@127.0.0.1") for m in headlines), headlines
    assert any(m.startswith("pulled a.tar.gz (4 B) in ") for m in headlines), headlines


def test_a_resumed_pull_says_where_it_continues(make_watch, server, tmp_path, xdg):
    data = bytes(range(256)) * 40
    (server.root / "big.bin").write_bytes(data)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / ".big.bin.part").write_bytes(data[:4096])
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path,
                                         extra='protocol = "sftp"\nresume = true\n'))
    records = []
    report = engine(xdg, records).poll(watch, WatchState(False), events=[file_event("big.bin", data)])
    assert not report.failed, records
    headlines = [r["message"] for r in records if r["event"] == "plugin.event"]
    assert any(m.startswith("resuming big.bin at 4 KiB of 10 KiB from u@127.0.0.1") for m in headlines), headlines


def test_a_failing_file_does_not_stop_the_rest_of_the_queue(make_watch, server, tmp_path, xdg):
    """One file that cannot be handled must not starve the queue, and the poll still fails so it is retried (issue 5)."""
    (server.root / "good.tar.gz").write_bytes(b"data")
    (server.root / "clash.tar.gz").write_bytes(b"new data")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "clash.tar.gz").write_bytes(b"old, not pushed yet")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path, extra='on_conflict = "skip-identical"\n'))
    records = []
    events = [file_event("clash.tar.gz", b"new data"), file_event("good.tar.gz", b"data")]
    report = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert report.failed  # reported, so the next poll retries
    assert (stage / "good.tar.gz").read_bytes() == b"data"  # the queue carried on
    failures = [r for r in records if r["event"] == "plugin.event" and "the rest of the queue is still tried" in r["message"]]
    assert failures and all(r["level"] == "WARNING" for r in failures), failures  # quiet must show it


WITHDRAWN = b"gone before the pull"  # the size and sha of a file that is no longer on the source


def withdrawn_key(name):
    return event_key(file_event(name, WITHDRAWN))


@pytest.mark.parametrize(
    ("extra", "partials"),
    [("", {}), ('protocol = "sftp"\nresume = true\n', {"withdrawn.bin": b"x", "good.bin": b"d"})],
    ids=["classic-scp", "sftp-resume"],
)
def test_a_withdrawn_file_is_skipped_and_the_queue_behind_it_flows(make_watch, server, tmp_path, xdg, extra, partials):
    """A file gone from the source when its pull runs is withdrawn: skip it, succeed, and keep the queue moving (issue 14)."""
    (server.root / "good.bin").write_bytes(b"data")
    stage = tmp_path / "stage"
    for name, prefix in partials.items():  # an earlier attempt's partial (a true prefix): the sftp reget path
        stage.mkdir(exist_ok=True)
        (stage / f".{name}.part").write_bytes(prefix)
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path, extra=extra))
    records = []
    events = [file_event("withdrawn.bin", WITHDRAWN), file_event("good.bin", b"data")]
    first = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert not first.failed, records
    assert (stage / "good.bin").read_bytes() == b"data"  # the queue behind it flowed
    skipped = Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "skipped.json")
    assert withdrawn_key("withdrawn.bin") in skipped
    messages = [r.get("message", "") for r in records if r["event"] == "plugin.event"]
    assert any("withdrawn.bin" in message and "gone" in message for message in messages), messages
    assert not (stage / ".withdrawn.bin.part").exists()  # a resumable partial of a withdrawn file is litter
    second = engine(xdg, []).poll(watch, first.after, events=events)
    assert second.outcome is Outcome.FALSE  # the event was acknowledged: it is not retried


def test_cleanup_parts_reaches_the_subdirectories_mirror_stages_into(tmp_path):
    """mirror pulls into <local>/<relative>/, so the sweeper has to look below the root a pull uses."""
    stage = tmp_path / "stage"
    (stage / "sub" / "deeper").mkdir(parents=True)
    leftover = stage / "sub" / "deeper" / ".a.bin.part"
    leftover.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(leftover, (old, old))
    cleanup_parts(SimpleNamespace(log=logging.getLogger("test")), stage, max_age=60)
    assert not leftover.exists()


def test_a_skipped_entry_older_than_seven_days_is_forgotten(make_watch, server, tmp_path, xdg):
    """`skipped` expires, so a file reported again after a week is pulled, not skipped forever."""
    (server.root / "good.bin").write_bytes(b"data")
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path))
    ledger = xdg.watch_data_dir("relay-pull") / "ledgers" / "skipped.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    stale = event_key(file_event("good.bin", b"data"))
    ledger.write_text(json.dumps({"version": 1, "entries": {stale: time.time() - 8 * 86400}}), encoding="utf-8")
    report = engine(xdg, []).poll(watch, WatchState(False), events=[file_event("good.bin", b"data")])
    assert not report.failed
    assert (tmp_path / "stage" / "good.bin").read_bytes() == b"data"


def test_on_missing_fail_keeps_the_withdrawn_file_failing(make_watch, server, tmp_path, xdg):
    """on_missing = "fail" keeps the strict behavior: the poll fails and the event is retried (issue 14)."""
    watch = load_watch_config(pull_watch(make_watch, server, tmp_path, extra='on_missing = "fail"\n'))
    records = []
    events = [file_event("withdrawn.bin", WITHDRAWN)]
    report = engine(xdg, records).poll(watch, WatchState(False), events=events)
    assert report.failed
    skipped = Ledger(xdg.watch_data_dir("relay-pull") / "ledgers" / "skipped.json")
    assert withdrawn_key("withdrawn.bin") not in skipped
    messages = [r.get("message", "") for r in records if r["event"] == "plugin.event"]
    assert any("withdrawn.bin" in message and "the rest of the queue is still tried" in message for message in messages), messages


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
    assert any("after it was reported" in r["message"] for r in records if r["event"] == "plugin.event")


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


def test_the_delete_helper_speaks_posix_paths_and_asks_for_a_verified_delete(tmp_path):
    """The command line is what differs on Windows (quoting, console), and it was never pinned there."""
    import json
    import subprocess as subprocess_module

    from watchglass.ctx import Ledger
    from watchglass.recipes.pull import TO_DELETE, delete_remote, queue_key

    calls = []

    class FakeCtx:
        settings = {
            "remote": "u@h", "port": 2222, "identity": None, "ssh_options": (), "known_hosts": None,
            "ssh_command": None, "remote_python": "auto", "remote_dir": "/data/out",
        }
        log = logging.getLogger("test.pull.delete")

        def __init__(self):
            self.data_dir = tmp_path / "data"
            self.settings = dict(type(self).settings)

        def ledger(self, name, expire=None):
            return Ledger(self.data_dir / "ledgers" / f"{name}.json", expire=expire)

        def run(self, argv, *, input=None, check=True):
            calls.append(([str(item) for item in argv], input))
            return subprocess_module.CompletedProcess(argv, 0, "", "")

    ctx = FakeCtx()
    entry = queue_key("/data/out/a b.tar.gz|7|1700000000.0", "a" * 64)
    ctx.ledger(TO_DELETE).add(entry)
    delete_remote(ctx, [entry])
    [(argv, payload)] = calls
    assert argv[0] == "ssh"
    options = json.loads(json.loads(payload.split("\n", 1)[0].split("= ", 1)[1]))
    assert options["mode"] == "delete" and options["dir"] == "/data/out"
    [item] = options["files"]
    assert (item["path"], item["size"], item["sha256"]) == ("/data/out/a b.tar.gz", 7, "a" * 64)
