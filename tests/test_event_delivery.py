import json
import os

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.state import Outcome, WatchState
from portable import py

EVENTS = ({"event": "file", "name": "a.gz", "observer": "inbox"}, {"line": "hi", "observer": "feed"})
COPY_EVENTS = py('import os, shutil; shutil.copy(os.environ["KEEPWATCH_EVENTS_FILE"], "events.json")')
WATCH_PY = """
    import json


    def check(ctx):
        return True, [event.get("name") or event.get("line") for event in ctx.events]


    def on_true(ctx):
        (ctx.watch_dir / "seen.json").write_text(json.dumps(ctx.events))
"""


def engine_for(xdg, records):
    return PollEngine(
        runner=Runner(),
        paths=xdg,
        global_config=load_global_config(xdg.config_file, xdg),
        sink=records.append,
        pid=os.getpid(),
    )


def test_python_hooks_get_ctx_events(make_watch, xdg):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    records = []
    report = engine_for(xdg, records).poll(load_watch_config(watch_dir), WatchState(False), events=EVENTS)
    assert report.outcome is Outcome.TRUE and report.payload == ["a.gz", "hi"]
    assert json.loads((watch_dir / "seen.json").read_text()) == list(EVENTS)
    assert [r["events"] for r in records if r["event"] == "poll.start"] == [2]


def test_python_hooks_without_events_get_an_empty_list(make_watch, xdg):
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    return ctx.events == []\n"})
    report = engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False))
    assert report.outcome is Outcome.TRUE


def test_command_hooks_get_the_events_file(make_watch, xdg):
    watch_dir = make_watch("w", config=f"[hooks]\ncheck = {COPY_EVENTS}\n")
    engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False), events=EVENTS)
    assert json.loads((watch_dir / "events.json").read_text()) == list(EVENTS)


def test_command_hooks_without_events_get_an_empty_array(make_watch, xdg):
    watch_dir = make_watch("w", config=f"[hooks]\ncheck = {COPY_EVENTS}\n")
    engine_for(xdg, []).poll(load_watch_config(watch_dir), WatchState(False))
    assert json.loads((watch_dir / "events.json").read_text()) == []
    assert not list(xdg.run_dir(os.getpid(), "w").glob("*-events.json"))
