import json
import re
import time

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.locks import hold_lock
from keepwatch.offline import OfflineMarker, iso_time, write_offline
from keepwatch.paths import ensure_private_dir, write_json_atomic
from keepwatch.service import Service


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_status_shows_what_a_watch_is_running_and_transferring(xdg, make_watch):
    """`status` says which hook runs and how far its transfer is, straight from status.json (issue 3)."""
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    part = xdg.state_home / ".a.bin.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 2048)
    write_json_atomic(xdg.status_file, {
        "format": 1,
        "service": {"running": True, "pid": 4711, "version": "2026.10.10", "started": iso_time(time.time() - 60),
                    "updated": iso_time(time.time()), "config": "x", "config_error": None, "only": []},
        "watches": {"a": {"description": "x", "watch_dir": "/w/a", "enabled": True, "interval": 30.0,
                          "condition": True, "failures": 0, "last_poll": None, "next_poll": None,
                          "config_error": None, "pending_events": 0, "observers": {},
                          "in_flight": {"hook": "on_true", "poll_id": "p1", "target": "recipe pull:on_true",
                                        "started_at": iso_time(time.time() - 90), "timeout": 7200.0},
                          "transfer": {"name": "a.bin", "part": str(part), "size": 4096, "part_size": 2048,
                                       "started_at": iso_time(time.time() - 90)}}},
        "invalid_watches": {},
        "problems": [],
    })
    ensure_private_dir(xdg.runtime)
    with hold_lock(xdg.service_lock):
        result = run("status", "a")
        data = json.loads(run("status", "--json").output)
    assert "polling" in result.output
    assert "recipe pull:on_true running 1m30s" in result.output
    assert "pulling a.bin 50% (2 KiB/4 KiB)" in result.output
    assert data["watches"]["a"]["in_flight"]["hook"] == "on_true"


def test_status_without_a_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    write_offline(xdg, "b", OfflineMarker(reason="disabled by user", since="2026-09-30T10:00:00.000-05:00", by_user=True))
    (xdg.state_home / "watches" / "gone").mkdir(parents=True)
    result = run("status")
    assert result.exit_code == 0
    assert "service: not running" in result.output
    assert re.search(r"^a\s+idle", result.output, re.M)
    assert re.search(r"^b\s+offline", result.output, re.M)
    assert "offline since 2026-09-30T10:00:00.000-05:00: disabled by user" in result.output
    assert "orphaned state: gone" in result.output
    data = json.loads(run("status", "--json").output)
    assert data["service"]["running"] is False
    assert data["orphaned_state"] == ["gone"]
    assert data["watches"]["b"]["offline"]["by_user"] is True
    assert data["watches"]["a"]["known_to_service"] is False


def test_status_with_a_running_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    ensure_private_dir(xdg.runtime)
    service = Service(paths=xdg, config_path=xdg.config_file, sink=lambda record: None, start_threads=False)
    service.start()
    service.runners["a"].poll_once(time.time())
    service.tick()
    with hold_lock(xdg.service_lock):
        data = json.loads(run("status", "--json").output)
        text = run("status", "a").output
    assert data["service"]["running"] is True
    entry = data["watches"]["a"]
    assert entry["condition"] is True
    assert entry["last_poll"]["outcome"] == "true"
    assert re.search(r"^a\s+online\s+condition TRUE\s+failures 0\s+last true", text, re.M)
    assert "service: running" in text


def test_status_for_an_unknown_watch(xdg, make_watch):
    make_watch("psg-export", config='[hooks]\ncheck = ["true"]\n')
    result = run("status", "psg-exprot")
    assert result.exit_code == 1
    assert "did you mean 'psg-export'" in result.output
