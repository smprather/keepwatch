import os
import time

import pytest

from keepwatch import observers
from keepwatch.config import load_global_config, load_watch_config
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, write_offline
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.scheduler import WatchRunner
from portable import PY, literal

RECORDING_WATCH = """
    def check(ctx):
        if (ctx.watch_dir / "fail").exists():
            raise RuntimeError("asked to fail")
        if (ctx.watch_dir / "unknown").exists():
            return None
        return True, [event["n"] for event in ctx.events]
"""
FEED = """
import json, os, time
print(json.dumps({"n": int(os.environ.get("FEED_N", "1"))}), flush=True)
time.sleep(60)
"""


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


@pytest.fixture
def runner_for(xdg):
    def make(watch_dir, records, clock):
        global_config = load_global_config(xdg.config_file, xdg)
        engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())
        return WatchRunner(
            load_watch_config(watch_dir),
            engine=engine,
            paths=xdg,
            sink=records.append,
            alert=lambda event, watch, reason: None,
            clock=clock,
        )

    return make


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def feed_config(extra=""):
    return f"interval = '1h'\n[observe.feed]\nkind = 'command'\ncommand = [{literal(PY)}, 'feed.py']\n{extra}"


def test_events_are_delivered_and_acknowledged(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    runner.add_event({"n": 2})
    report = runner.poll_once(clock())
    assert report.payload == [1, 2]
    assert len(runner.events) == 0


def test_a_failed_poll_keeps_the_events(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "fail").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    assert runner.poll_once(clock()).failed
    assert len(runner.events) == 1
    (watch_dir / "fail").unlink()
    clock.now += 3600
    assert runner.poll_once(clock()).payload == [1]
    assert len(runner.events) == 0


def test_an_unknown_poll_keeps_the_events(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "unknown").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    runner.poll_once(clock())
    assert len(runner.events) == 1


def test_a_waking_event_makes_the_watch_due(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.poll_once(clock())
    assert runner.seconds_until_due(clock()) == 60
    runner.add_event({"n": 1}, wake=False)
    assert runner.seconds_until_due(clock()) == 60
    runner.add_event({"n": 2})
    assert runner.seconds_until_due(clock()) == 0
    assert runner.poll_once(clock()).payload == [1, 2]
    assert runner.seconds_until_due(clock()) == 60


def test_events_do_not_cut_backoff_short(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    (watch_dir / "fail").write_text("")
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.poll_once(clock())
    runner.add_event({"n": 1})
    assert runner.seconds_until_due(clock()) > 0


def test_event_arriving_during_a_poll_is_kept_and_wakes(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    clock = Clock()
    runner = runner_for(watch_dir, [], clock)
    runner.add_event({"n": 1})
    real_poll = runner._engine.poll

    def poll(*args, **kwargs):
        runner.add_event({"n": 2})
        return real_poll(*args, **kwargs)

    runner._engine.poll = poll
    assert runner.poll_once(clock()).payload == [1]
    assert runner.events.pending()[1] == [{"n": 2}]
    assert runner.seconds_until_due(clock()) == 0


def test_a_full_queue_drops_the_oldest_with_a_warning(make_watch, runner_for):
    watch_dir = make_watch("w", files={"watch.py": RECORDING_WATCH})
    records = []
    runner = runner_for(watch_dir, records, Clock())
    runner.events.cap = 2
    for number in (1, 2, 3, 4):
        runner.add_event({"n": number, "observer": "feed"})
    assert runner.events.pending()[1] == [{"n": 3, "observer": "feed"}, {"n": 4, "observer": "feed"}]
    [dropped] = kinds(records, "observer.dropped")
    assert (dropped["level"], dropped["observer"], dropped["dropped"], dropped["cap"]) == ("WARNING", "feed", 1, 2)


def test_observer_events_wake_the_running_watch(make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: any(r.get("payload") == [1] for r in kinds(records, "check.outcome")))
        # The acknowledgement follows the poll's last record, so wait for it rather than racing it.
        assert wait_for(lambda: runner.snapshot(time.time())["pending_events"] == 0)
        snapshot = runner.snapshot(time.time())
        assert snapshot["observers"]["feed"]["running"] is True
        assert snapshot["observers"]["feed"]["kind"] == "command"
        assert snapshot["observers"]["feed"]["last_event"] is not None
    finally:
        runner.stop()
        assert runner.join(20)
    assert kinds(records, "observer.stopped")[-1]["reason"] == "stopped"
    assert runner.snapshot(time.time())["observers"] == {}


def test_offline_stops_observers_and_online_restarts_them(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.started"))
        marker = OfflineMarker(reason="disabled by user", since=iso_time(time.time()), by_user=True)
        write_offline(xdg, "w", marker)
        assert wait_for(lambda: kinds(records, "observer.stopped"))
        assert kinds(records, "observer.stopped")[0]["reason"] == "stopped"
        clear_offline(xdg, "w")
        assert wait_for(lambda: len(kinds(records, "observer.started")) == 2)
    finally:
        runner.stop()
        assert runner.join(20)


def test_parked_watch_runs_no_observers(make_watch, runner_for):
    watch_dir = make_watch(
        "w", config="enabled = false\n" + feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED}
    )
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        time.sleep(1.5)
    finally:
        runner.stop()
        assert runner.join(20)
    assert not kinds(records, "observer.started")


def test_a_config_change_restarts_the_observers(make_watch, runner_for):
    watch_dir = make_watch("w", config=feed_config(), files={"watch.py": RECORDING_WATCH, "feed.py": FEED})
    records = []
    runner = runner_for(watch_dir, records, time.time)
    runner.start()
    try:
        assert wait_for(lambda: any(r.get("payload") == [1] for r in kinds(records, "check.outcome")))
        (watch_dir / "config.toml").write_text(feed_config("[environment]\nFEED_N = '7'\n"))
        runner.update_config(load_watch_config(watch_dir))
        assert wait_for(lambda: any(r.get("payload") == [7] for r in kinds(records, "check.outcome")))
    finally:
        runner.stop()
        assert runner.join(20)
    assert len(kinds(records, "observer.started")) == 2
