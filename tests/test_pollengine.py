import os

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import Fake, PollEngine, parse_fakes
from keepwatch.runner import Runner
from keepwatch.state import Edge, Outcome, WatchState

PSG_WATCH = '''
    from pathlib import Path

    def check(ctx):
        files = [str(f) for f in ctx.glob(ctx.settings["pattern"])
                 if ctx.file_key(f) not in ctx.ledger("sent")]
        return bool(files), files

    def on_true(ctx):
        sent = ctx.ledger("sent")
        for f in ctx.payload:
            ctx.run(["cp", f, ctx.settings["dest"]])
            sent.add(ctx.file_key(f))
'''


@pytest.fixture
def engine_for(xdg):
    def make(records):
        global_config = load_global_config(xdg.config_file, xdg)
        return PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())

    return make


def test_parse_fakes():
    assert parse_fakes("true, FALSE,timeout") == [Outcome.TRUE, Outcome.FALSE, Outcome.TIMEOUT]
    with pytest.raises(ValueError, match="'maybe' is not an outcome; use a comma-separated list of: true, false"):
        parse_fakes("true,maybe")


def test_psg_style_watch_sends_once(tmp_path, make_watch, engine_for):
    incoming, dest = tmp_path / "incoming", tmp_path / "dest"
    incoming.mkdir()
    dest.mkdir()
    (incoming / "psg-export-1.tar.gz").write_text("one")
    watch_dir = make_watch("psg", config=f'''
        [settings]
        pattern = "{incoming}/psg-export-*.tar.gz"
        dest = "{dest}"
    ''', files={"watch.py": PSG_WATCH})
    watch = load_watch_config(watch_dir)
    records = []
    engine = engine_for(records)

    first = engine.poll(watch, WatchState(False))
    assert first.outcome is Outcome.TRUE
    assert first.planned == ("on_true",)
    assert first.failed is False and first.after == WatchState(True, None, 0)
    assert (dest / "psg-export-1.tar.gz").read_text() == "one"

    second = engine.poll(watch, first.after)
    assert second.outcome is Outcome.FALSE and second.planned == ()

    events = [r["event"] for r in records if r["poll_id"] == first.poll_id]
    assert events == ["poll.start", "hook.end", "check.outcome", "command", "hook.end", "poll.end"]
    assert all(r["watch"] == "psg" for r in records)


def test_failed_edge_is_retried_until_it_succeeds(make_watch, engine_for):
    watch_dir = make_watch("edge", files={"watch.py": '''
        def check(ctx):
            return True

        def on_rise(ctx):
            flag = ctx.watch_dir / "allow"
            if not flag.exists():
                raise RuntimeError("not yet")
    '''})
    watch = load_watch_config(watch_dir)
    engine = engine_for([])
    first = engine.poll(watch, WatchState(False))
    assert first.failed and first.after == WatchState(True, Edge.RISE, 1)
    (watch_dir / "allow").write_text("")
    second = engine.poll(watch, first.after)
    assert second.planned == ("on_rise",)
    assert not second.failed and second.after == WatchState(True, None, 0)


def test_dry_run_does_not_run_actions(make_watch, engine_for):
    watch_dir = make_watch("dry", files={"watch.py": '''
        def check(ctx):
            return True

        def on_true(ctx):
            (ctx.watch_dir / "ran").write_text("")
    '''})
    report = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False), dry_run=True)
    assert report.planned == ("on_true",) and report.results == []
    assert not (watch_dir / "ran").exists()
    assert report.after == WatchState(True, None, 0)


def test_fake_skips_the_check_and_hands_over_the_payload(make_watch, engine_for):
    watch_dir = make_watch("fake", files={"watch.py": '''
        def check(ctx):
            (ctx.watch_dir / "checked").write_text("")
            return False

        def on_true(ctx):
            (ctx.watch_dir / "payload").write_text(repr(ctx.payload))
    '''})
    records = []
    report = engine_for(records).poll(load_watch_config(watch_dir), WatchState(False), fake=Fake(Outcome.TRUE, ["x"]))
    assert report.faked and report.outcome is Outcome.TRUE
    assert not (watch_dir / "checked").exists()
    assert (watch_dir / "payload").read_text() == "['x']"
    assert records[0]["faked"] is True


def test_syntax_error_in_watch_py_is_an_error_outcome(make_watch, engine_for):
    watch_dir = make_watch("broken", files={"watch.py": "def check(ctx):\n    return (\n"})
    report = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False))
    assert report.outcome is Outcome.ERROR and report.failed
    assert report.reason.startswith("watch.py line 2: syntax error")
    assert report.after.failures == 1


def test_missing_check_and_duplicate_hooks(make_watch, engine_for):
    no_check = make_watch("nocheck", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    report = engine_for([]).poll(load_watch_config(no_check), WatchState(False))
    assert report.outcome is Outcome.ERROR and report.reason.startswith("the watch has no check")

    both = make_watch("both", config='[hooks]\ncheck = "true"\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    report = engine_for([]).poll(load_watch_config(both), WatchState(False))
    assert report.outcome is Outcome.ERROR and "defined both" in report.reason


def test_report_to_dict(make_watch, engine_for):
    watch_dir = make_watch("d", config='[hooks]\ncheck = "exit 1"\n')
    data = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False)).to_dict()
    assert data["outcome"] == "false" and data["condition_after"] is False
    assert data["planned_actions"] == [] and data["failed"] is False
