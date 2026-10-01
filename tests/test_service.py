import json
import textwrap
import threading
import time

import pytest

from keepwatch import service as service_module
from keepwatch.config import ConfigError
from keepwatch.service import Service


def make_service(xdg, records, **kwargs):
    return Service(paths=xdg, config_path=xdg.config_file, sink=records.append, start_threads=False, **kwargs)


def events(records, name):
    return [r for r in records if r["event"] == name]


def write_global(xdg, text):
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(textwrap.dedent(text))


def test_start_loads_watches_and_writes_status(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    assert list(service.runners) == ["a"]
    assert [r["watch"] for r in events(records, "watch.added")] == ["a"]
    status = json.loads(xdg.status_file.read_text())
    assert status["service"]["running"] is True
    assert status["watches"]["a"]["condition"] is False


def test_watches_are_added_and_removed_live(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    service.tick()
    assert sorted(service.runners) == ["a", "b"]
    runner_a = service.runners["a"]
    (xdg.default_watches_dir / "a").rename(xdg.default_watches_dir / "_a")
    service.tick()
    assert list(service.runners) == ["b"]
    assert runner_a._stop.is_set()
    assert [r["watch"] for r in events(records, "watch.removed")] == ["a"]


def test_changed_config_is_handed_to_the_runner(xdg, make_watch):
    watch_dir = make_watch("a", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    (watch_dir / "config.toml").write_text('interval = "300s"\n[hooks]\ncheck = ["true"]\n')
    service.tick()
    assert events(records, "watch.changed")[0]["watch"] == "a"
    service.runners["a"].poll_once(service.runners["a"].next_due)
    assert service.runners["a"].config.interval == 300


def test_broken_edit_keeps_last_good_config(xdg, make_watch):
    watch_dir = make_watch("a", config='interval = "60s"\n[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    (watch_dir / "config.toml").write_text('intervall = "60s"\n[hooks]\ncheck = ["true"]\n')
    service.tick()
    service.tick()
    errors = events(records, "config.error")
    assert len(errors) == 1
    assert errors[0]["watch"] == "a" and errors[0]["running_previous"] is True
    assert "unknown key 'intervall'" in errors[0]["error"]
    assert service.runners["a"].config.interval == 60
    status = json.loads(xdg.status_file.read_text())
    assert "intervall" in status["watches"]["a"]["config_error"]
    (watch_dir / "config.toml").write_text('interval = "60s"\n[hooks]\ncheck = ["true"]\n  \n')
    service.tick()
    assert service.runners["a"].config_error is None


def test_new_invalid_watch_is_not_started(xdg, make_watch):
    make_watch("bad", config="interval = 0\n")
    records = []
    service = make_service(xdg, records)
    service.start()
    assert service.runners == {}
    status = json.loads(xdg.status_file.read_text())
    assert "interval" in status["invalid_watches"]["bad"]


def test_broken_global_config(xdg, make_watch):
    write_global(xdg, "watch_dirs = 5\n")
    with pytest.raises(ConfigError):
        make_service(xdg, []).start()
    write_global(xdg, "reload_interval = \"5s\"\n")
    xdg.default_watches_dir.mkdir(parents=True, exist_ok=True)
    records = []
    service = make_service(xdg, records)
    service.start()
    write_global(xdg, "watch_dirs = 5 \n")
    service.tick()
    assert len(events(records, "config.error")) == 1
    assert service.global_config.reload_interval == 5


def test_global_defaults_reach_running_watches(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    write_global(xdg, '[defaults]\ninterval = "2m"\n')
    service.tick()
    assert events(records, "config.loaded")
    runner = service.runners["a"]
    runner.poll_once(runner.next_due)
    assert runner.config.interval == 120


def test_only_limits_the_watches(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    service = make_service(xdg, [], only={"b"})
    service.start()
    assert list(service.runners) == ["b"]


def test_stop(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    service.stop()
    assert service.runners["a"]._stop.is_set()
    assert records[-1]["event"] == "service.stop"
    assert json.loads(xdg.status_file.read_text())["service"]["running"] is False


def test_tick_survives_errors(xdg, make_watch, monkeypatch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    service.start()
    real_write = service_module.write_json_atomic

    def full_disk(path, document):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service_module, "write_json_atomic", full_disk)
    service.tick()
    service.tick()
    errors = events(records, "service.error")
    assert len(errors) == 1
    assert errors[0]["error"] == "OSError: [Errno 28] No space left on device"
    monkeypatch.setattr(service_module, "write_json_atomic", real_write)
    service.tick()
    monkeypatch.setattr(service_module, "write_json_atomic", full_disk)
    service.tick()
    assert len(events(records, "service.error")) == 2


def test_stop_request_file_stops_the_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    thread = threading.Thread(target=service.run, args=(threading.Event(),), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not xdg.status_file.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    xdg.stop_request.parent.mkdir(parents=True, exist_ok=True)
    xdg.stop_request.write_text("now")
    thread.join(15)
    assert not thread.is_alive()
    assert not xdg.stop_request.exists()
    assert [r["event"] for r in records][-2:] == ["service.stop_requested", "service.stop"]
