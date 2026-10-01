import contextlib
import os
import time

import pytest

from keepwatch import scheduler as scheduler_module
from keepwatch.config import load_global_config, load_watch_config
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.pollengine import PollEngine
from keepwatch.runner import Runner
from keepwatch.scheduler import WatchRunner
from keepwatch.state import Outcome, WatchState


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def runner_for(xdg):
    def make(watch_dir, records, alerts, clock):
        global_config = load_global_config(xdg.config_file, xdg)
        engine = PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())
        return WatchRunner(
            load_watch_config(watch_dir),
            engine=engine,
            paths=xdg,
            sink=records.append,
            alert=lambda event, watch, reason: alerts.append((event, watch, reason)),
            clock=clock,
        )

    return make


def events(records, name):
    return [r for r in records if r["event"] == name]


def test_polls_immediately_then_after_the_interval(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.poll_once(clock()) is not None
    clock.now += 59
    assert runner.poll_once(clock()) is None
    clock.now += 1
    report = runner.poll_once(clock())
    assert report is not None and report.before.condition is True


def test_backoff_then_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "10s"\nmax_failures = 2\n[hooks]\ncheck = "exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert runner.state.failures == 1
    assert runner.seconds_until_due(clock()) == 60
    clock.now += 60
    runner.poll_once(clock())
    marker = read_offline(xdg, "w")
    assert marker.reason == "2 consecutive failed polls" and marker.by_user is False
    assert marker.last_failure.startswith("check error")
    (offline,) = events(records, "watch.offline")
    assert offline["level"] == "CRITICAL"
    assert alerts == [("offline", "w", f"2 consecutive failed polls; last: {marker.last_failure}")]
    clock.now += 100_000
    assert runner.poll_once(clock()) is None


def test_trial_poll_brings_the_watch_back(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 1\nretry_after = "1h"\n[hooks]\ncheck = "test -f ok || exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert read_offline(xdg, "w") is not None
    clock.now += 3599
    assert runner.poll_once(clock()) is None
    clock.now += 1
    failed_trial = runner.poll_once(clock())
    assert failed_trial.trial is True and read_offline(xdg, "w") is not None
    (watch_dir / "ok").write_text("")
    clock.now += 3600
    trial = runner.poll_once(clock())
    assert trial.trial is True and not trial.failed
    assert read_offline(xdg, "w") is None and runner.offline is None
    assert events(records, "watch.online")[0]["reason"] == "trial poll succeeded"
    assert alerts[-1] == ("online", "w", "trial poll succeeded")
    assert events(records, "poll.start")[-1]["trial"] is True


def test_disable_and_enable_through_marker_files(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='retry_after = "1m"\n[hooks]\ncheck = ["true"]\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    write_offline(xdg, "w", OfflineMarker(reason="disabled by user", since=iso_time(clock()), by_user=True))
    clock.now += 10_000
    assert runner.poll_once(clock()) is None
    assert events(records, "watch.offline")[0]["level"] == "WARNING"
    clear_offline(xdg, "w")
    assert runner.poll_once(clock()) is not None
    assert events(records, "watch.online")[0]["reason"] == "enabled by user"
    assert alerts == [("online", "w", "enabled by user")]


def test_offline_survives_restart(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    write_offline(xdg, "w", OfflineMarker(reason="5 consecutive failed polls", since=iso_time(1.0)))
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.offline is not None
    assert runner.poll_once(clock()) is None


def test_poll_interrupted_by_shutdown_does_not_go_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 1\n[hooks]\ncheck = "exit 9"\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.stop()
    runner.poll_once(clock())
    assert read_offline(xdg, "w") is None


def test_config_update_applies_next_poll_and_keeps_state(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.poll_once(clock())
    (watch_dir / "config.toml").write_text('interval = "5m"\n[hooks]\ncheck = ["true"]\n')
    runner.update_config(load_watch_config(watch_dir))
    clock.now += 60
    report = runner.poll_once(clock())
    assert report.before.condition is True
    assert runner.config.interval == 300
    assert runner.seconds_until_due(clock()) == 300


def test_parked_watch_never_polls(make_watch, runner_for):
    watch_dir = make_watch("w", config='enabled = false\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    assert runner.poll_once(clock()) is None
    assert runner.snapshot(clock())["next_poll"] is None


def test_snapshot(make_watch, runner_for):
    watch_dir = make_watch("w", config='description = "d"\n[hooks]\ncheck = ["true"]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.poll_once(clock())
    snap = runner.snapshot(clock())
    assert snap["description"] == "d" and snap["condition"] is True and snap["failures"] == 0
    assert snap["last_poll"]["outcome"] == "true" and snap["offline"] is None
    assert snap["next_poll"] == iso_time(clock() + 60)


def test_thread_loop_polls_and_stops(make_watch, runner_for):
    watch_dir = make_watch("w", config='interval = "1s"\n[hooks]\ncheck = "echo x >> polls"\n')
    runner = runner_for(watch_dir, [], [], time.time)
    runner.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        polls = watch_dir / "polls"
        if polls.exists() and len(polls.read_text().splitlines()) >= 2:
            break
        time.sleep(0.1)
    runner.stop()
    assert runner.join(10) is True
    assert len((watch_dir / "polls").read_text().splitlines()) >= 2


def test_missing_watch_dir_is_not_polled(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    clock, records = Clock(), []
    runner = runner_for(watch_dir, records, [], clock)
    watch_dir.rename(watch_dir.parent / "renamed")
    assert runner.poll_once(clock()) is None
    assert runner.state.failures == 0
    assert not xdg.watch_state_dir("w").exists()
    assert records == []


def disable_during_poll(runner, xdg):
    real_poll = runner._engine.poll

    def poll(*args, **kwargs):
        write_offline(xdg, runner.name, OfflineMarker(reason="disabled by user", since=iso_time(1.0), by_user=True))
        return real_poll(*args, **kwargs)

    runner._engine.poll = poll


def test_directory_renamed_while_waiting_for_the_lock(xdg, make_watch, runner_for, monkeypatch):
    watch_dir = make_watch("w", config='[hooks]\ncheck = ["true"]\n')
    clock, records = Clock(), []
    runner = runner_for(watch_dir, records, [], clock)
    real_hold_lock = scheduler_module.hold_lock

    @contextlib.contextmanager
    def renaming_lock(path, **kwargs):
        watch_dir.rename(watch_dir.parent / "renamed")
        with real_hold_lock(path, **kwargs):
            yield

    monkeypatch.setattr(scheduler_module, "hold_lock", renaming_lock)
    assert runner.poll_once(clock()) is None
    assert runner.state.failures == 0
    assert not xdg.watch_state_dir("w").exists()


def test_unknown_poll_never_takes_a_watch_offline(xdg, make_watch, runner_for):
    watch_dir = make_watch("w", config='max_failures = 2\n[hooks]\ncheck = "exit 3"\n[check_exit_codes]\nunknown = [3]\n')
    clock = Clock()
    runner = runner_for(watch_dir, [], [], clock)
    runner.state = WatchState(False, None, 3)
    report = runner.poll_once(clock())
    assert report.outcome is Outcome.UNKNOWN and not report.failed
    assert read_offline(xdg, "w") is None


def test_user_disable_during_a_poll_wins(xdg, make_watch, runner_for):
    # Going offline: the user's marker is not replaced by an automatic one.
    watch_dir = make_watch("a", config='max_failures = 1\n[hooks]\ncheck = "exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    disable_during_poll(runner, xdg)
    runner.poll_once(clock())
    assert read_offline(xdg, "a").by_user is True
    assert alerts == []
    assert not [r for r in records if r["event"] == "watch.offline" and r["level"] == "CRITICAL"]

    # A successful trial poll does not clear a marker the user wrote during it.
    watch_dir = make_watch("b", config='max_failures = 1\nretry_after = "1m"\n[hooks]\ncheck = "test -f ok || exit 9"\n')
    clock, records, alerts = Clock(), [], []
    runner = runner_for(watch_dir, records, alerts, clock)
    runner.poll_once(clock())
    assert read_offline(xdg, "b").by_user is False
    (watch_dir / "ok").write_text("")
    disable_during_poll(runner, xdg)
    clock.now += 60
    report = runner.poll_once(clock())
    assert report.trial is True and not report.failed
    assert read_offline(xdg, "b").by_user is True
    assert [event for event, _, _ in alerts] == ["offline"]
    assert not [r for r in records if r["event"] == "watch.online"]
