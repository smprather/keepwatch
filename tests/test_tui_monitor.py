"""The monitor's data layer: tests first (see docs/superpowers/plans/2026-10-06-keepwatch-plan-8-tui.md)."""

import ast
import time
from pathlib import Path

from keepwatch.locks import hold_lock
from keepwatch.logstore import LogWriter, make_record
from keepwatch.offline import OfflineMarker, iso_time, write_offline
from keepwatch.paths import ensure_private_dir, write_json_atomic
from keepwatch.tui import monitor as monitor_module
from keepwatch.tui.monitor import Monitor


def last_poll(at, outcome="true", failed=False, reason=None):
    return {"poll_id": "p1", "at": iso_time(at), "outcome": outcome, "reason": reason, "failed": failed, "trial": False,
            "results": []}


def entry(**fields):
    data = {
        "description": "test watch",
        "watch_dir": "/tmp/watches/a",
        "enabled": True,
        "interval": 30.0,
        "condition": True,
        "pending_edge": None,
        "failures": 0,
        "offline": None,
        "last_poll": last_poll(time.time() - 12),
        "next_poll": iso_time(time.time() + 18),
        "config_error": None,
        "pending_events": 0,
        "observers": {"local": {"kind": "files", "running": True}},
    }
    data.update(fields)
    return data


def status_document(watches=None, invalid=None, updated=None, pid=4711):
    return {
        "service": {"running": True, "pid": pid, "version": "2026.10.6", "started": iso_time(time.time() - 900),
                    "updated": iso_time(time.time() if updated is None else updated), "config": "x",
                    "config_error": None, "only": []},
        "watches": watches or {},
        "invalid_watches": invalid or {},
        "problems": [],
    }


def publish(paths, document):
    write_json_atomic(paths.status_file, document)


def running(paths):
    """Hold the service lock, so service_running() reports a live service (as tests/test_cli_status.py does)."""
    ensure_private_dir(paths.runtime)
    return hold_lock(paths.service_lock)


def write_records(paths, *records):
    writer = LogWriter(paths.log_file, max_bytes=1_000_000, backups=1)
    for record in records:
        writer.write(record)


def loaded(paths, **kwargs):
    """A Monitor with the log's tail already ingested (start + stop: no follower thread left running)."""
    monitor = Monitor(paths, paths.config_file, **kwargs)
    monitor.start()
    monitor.stop()
    return monitor


def rows_by_name(status):
    return {row.name: row for row in status.watches}


# --- status -----------------------------------------------------------------


def test_a_running_service_lists_every_discovered_watch(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(), "b": entry(enabled=False, condition=False)}))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        status = monitor.status()
    assert status.stale is False and status.stale_since is None
    assert [row.name for row in status.watches] == ["a", "b"]
    assert [row.state for row in status.watches] == ["online", "parked"]
    assert status.service["pid"] == 4711
    assert status.service["version"] == "2026.10.6"
    assert monitor_module.condition_text(status.watches[0]) == "TRUE"
    assert monitor_module.observers_text(status.watches[0]) == "1/1"
    assert monitor_module.last_text(status.watches[0], time.time()).startswith("true ")


def test_a_stopped_service_shows_the_last_known_status(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry()}, updated=time.time() - 7200))
    monitor = Monitor(xdg, xdg.config_file)
    monitor.refresh()  # no lock: the service is not running
    status = monitor.status()
    assert status.stale is True
    assert status.stale_since is not None
    assert abs(status.stale_since - (time.time() - 7200)) < 60
    row = status.watches[0]
    assert row.state == "idle"  # an otherwise-online watch reads idle, never polling
    assert row.condition is True  # last known
    assert row.last_poll is not None
    assert monitor_module.next_text(row, time.time(), stale=True) == "—"


def test_status_older_than_five_seconds_is_stale_while_running(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry()}, updated=time.time() - 60))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        status = monitor.status()
    assert status.stale is True
    assert monitor_module.next_text(status.watches[0], time.time(), stale=True) == "—"


def test_a_live_watch_counts_down_and_an_offline_one_says_retry(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(next_poll=iso_time(time.time() + 90))}))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        row = monitor.status().watches[0]
    assert monitor_module.next_text(row, time.time(), stale=False).startswith("1m")


def test_offline_beats_parked(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(enabled=False)}))
    write_offline(xdg, "a", OfflineMarker(reason="5 consecutive failed polls", since=iso_time(time.time() - 100),
                                          last_failure="on_true failed: boom"))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        row = monitor.status().watches[0]
    assert row.state == "offline"
    assert row.offline is not None
    assert row.offline["reason"] == "5 consecutive failed polls"
    assert monitor_module.note_text(row) == ""


def test_invalid_watches_and_orphaned_state_get_rows(xdg, make_watch):
    make_watch("broken", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(invalid={"broken": "config.toml:4: unknown key 'x'"}, watches={}))
    (xdg.state_home / "watches" / "gone").mkdir(parents=True)
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        rows = rows_by_name(monitor.status())
    assert rows["broken"].state == "invalid"
    assert rows["broken"].note.startswith("invalid: config.toml:4")
    assert rows["gone"].state == "orphaned"
    assert "rename" in rows["gone"].note


def test_notes_cover_config_errors_missing_dirs_events_and_user_offline(xdg, make_watch):
    make_watch("broken-cfg", config='[hooks]\ncheck = ["true"]\n')
    make_watch("user-off", config='[hooks]\ncheck = ["true"]\n')
    make_watch("queued", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={
        "broken-cfg": entry(config_error="a.toml:2: bad\na.toml:3: worse"),
        "gone-dir": entry(),
        "queued": entry(pending_events=3),
    }))
    write_offline(xdg, "user-off", OfflineMarker(reason="disabled by user", since=iso_time(time.time() - 10), by_user=True))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        rows = rows_by_name(monitor.status())
    assert monitor_module.note_text(rows["broken-cfg"]) == "config error: a.toml:2: bad"
    assert monitor_module.note_text(rows["gone-dir"]) == "missing directory"
    assert monitor_module.note_text(rows["queued"]) == "+3 events"
    assert monitor_module.note_text(rows["user-off"]) == "offline by user"


def test_a_broken_global_config_keeps_the_last_discovery(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry()}))
    monitor = Monitor(xdg, xdg.config_file)
    with running(xdg):
        monitor.refresh()
        assert [row.name for row in monitor.status().watches] == ["a"]
        xdg.config_file.write_text("watch_dirs = 3\n", encoding="utf-8")
        monitor.rescan()
        status = monitor.status()
    assert [row.name for row in status.watches] == ["a"]  # the last good discovery
    assert any("global config error" in problem for problem in status.problems)


# --- in-flight polls --------------------------------------------------------


def test_an_unmatched_poll_start_is_a_poll_in_flight(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time() - 300), next_poll=None)}))
    write_records(xdg, make_record("poll.start", watch="a", poll_id="p9", condition=True))
    monitor = loaded(xdg)
    with running(xdg):
        monitor.refresh()
        assert monitor.status().watches[0].state == "polling"
        flight = monitor.in_flight()["a"]
    assert flight.poll_id == "p9" and flight.started <= time.time()


def test_poll_end_closes_and_a_new_poll_start_replaces(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time() - 300), next_poll=None)}))
    write_records(xdg, make_record("poll.start", watch="a", poll_id="p1"), make_record("poll.end", watch="a", poll_id="p1"))
    monitor = loaded(xdg)
    with running(xdg):
        monitor.refresh()
        assert monitor.in_flight() == {}
        monitor._ingest(make_record("poll.start", watch="a", poll_id="p2"))
        assert monitor.in_flight()["a"].poll_id == "p2"
        monitor._ingest(make_record("poll.start", watch="a", poll_id="p3"))
        assert monitor.in_flight()["a"].poll_id == "p3"


def test_a_poll_the_service_already_reported_cannot_still_be_running(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    write_records(xdg, make_record("poll.start", watch="a", poll_id="p9"))
    publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time()), next_poll=None)}))
    monitor = loaded(xdg)
    with running(xdg):
        monitor.refresh()
        assert monitor.in_flight() == {}
        assert monitor.status().watches[0].state == "online"


def test_in_flight_is_cleared_when_the_service_stops_or_the_pid_changes(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time() - 300), next_poll=None)}))
    write_records(xdg, make_record("poll.start", watch="a", poll_id="p9"))
    monitor = loaded(xdg)
    with running(xdg):
        monitor.refresh()
        assert monitor.in_flight() != {}
    monitor.refresh()  # the lock is free: the service stopped, so the open poll is forgotten
    assert monitor.in_flight() == {}
    monitor._ingest(make_record("poll.start", watch="a", poll_id="p10"))
    with running(xdg):
        monitor.refresh()
        assert monitor.in_flight() != {}
        publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time() - 300), next_poll=None)}, pid=999))
        monitor.refresh()  # a new service: the open poll belongs to the old one
        assert monitor.in_flight() == {}


# --- records ----------------------------------------------------------------


def test_backfill_buckets_by_watch_and_respects_the_bounds(xdg):
    records = [make_record("hook.end", watch="a", poll_id=f"p{i}") for i in range(4)]
    records.append(make_record("hook.end", watch="b", poll_id="pb"))
    records.append(make_record("service.start", version="2026.10.6"))
    write_records(xdg, *records)
    monitor = Monitor(xdg, xdg.config_file, backfill=200, per_watch=2, history=3)
    monitor.start()
    monitor.stop()
    assert len(monitor.records()) == 3  # the overall bound
    assert len(monitor.records("a")) == 2  # the per-watch bound
    assert len(monitor.records("b")) == 1
    assert all("a" in line for line in monitor.records("a"))


def test_backfill_reads_only_the_last_records(xdg):
    write_records(xdg, *[make_record("hook.end", watch="a", poll_id=f"p{i}") for i in range(10)])
    monitor = Monitor(xdg, xdg.config_file, backfill=3)
    monitor.start()
    monitor.stop()
    assert len(monitor.records()) == 3


def test_verbose_records_show_captured_output(xdg):
    write_records(xdg, make_record("hook.end", watch="a", poll_id="p1", hook="on_true", status="ok", duration=1.0,
                                  target="watch.py:on_true", stdout="the whole output\n"))
    monitor = Monitor(xdg, xdg.config_file)
    monitor.start()
    monitor.stop()
    assert "the whole output" not in "\n".join(monitor.records())
    assert "the whole output" in "\n".join(monitor.records(verbose=True))


def test_the_version_changes_only_when_a_record_arrives(xdg):
    write_records(xdg, make_record("hook.end", watch="a", poll_id="p1"))
    monitor = Monitor(xdg, xdg.config_file)
    monitor.start()
    monitor.stop()
    version = monitor.version()
    assert version == 1
    monitor.records()
    monitor.status()
    assert monitor.version() == version


def test_the_follower_picks_up_records_written_after_start(xdg):
    monitor = Monitor(xdg, xdg.config_file)
    monitor.start()
    try:
        before = monitor.version()
        write_records(xdg, make_record("hook.end", watch="a", poll_id="p1"))
        deadline = time.monotonic() + 5
        while monitor.version() == before and time.monotonic() < deadline:
            time.sleep(0.05)
        assert monitor.version() == before + 1
    finally:
        monitor.stop()


def test_the_data_layer_does_not_import_textual():
    source = Path(monitor_module.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "textual" not in imported
