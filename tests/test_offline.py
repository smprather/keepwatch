import json
import os
import time

from watchglass.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from watchglass.paths import write_json_atomic


def test_write_json_atomic(tmp_path):
    target = tmp_path / "a" / "b.json"
    write_json_atomic(target, {"x": 1})
    assert json.loads(target.read_text()) == {"x": 1}
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_write_json_atomic_fsyncs_the_new_document_before_the_rename(tmp_path, monkeypatch):
    """offline.json is authoritative: a marker lost to a power cut parks the watch as if a user disabled it."""
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    target = tmp_path / "offline.json"
    write_json_atomic(target, {"x": 1})
    assert len(synced) == 1
    assert json.loads(target.read_text()) == {"x": 1}


def test_iso_time_round_trips():
    marker = OfflineMarker(reason="r", since=iso_time(1_000_000.5))
    assert abs(marker.since_epoch() - 1_000_000.5) < 0.001


def test_offline_marker_round_trip(xdg):
    assert read_offline(xdg, "w") is None
    marker = OfflineMarker(reason="5 consecutive failed polls", since=iso_time(1_000_000), last_failure="on_true failed: x")
    write_offline(xdg, "w", marker)
    assert read_offline(xdg, "w") == marker
    assert json.loads(xdg.offline_file("w").read_text())["by_user"] is False
    permanent = OfflineMarker(reason="authentication failed; fix the credential", since=iso_time(1_000_000),
                              last_failure="on_true failed: Permission denied", permanent=True)
    write_offline(xdg, "w", permanent)
    assert read_offline(xdg, "w") == permanent
    assert json.loads(xdg.offline_file("w").read_text())["permanent"] is True
    assert clear_offline(xdg, "w") is True
    assert clear_offline(xdg, "w") is False
    assert read_offline(xdg, "w") is None


def test_unreadable_marker_counts_as_offline_by_user(xdg):
    path = xdg.offline_file("w")
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    marker = read_offline(xdg, "w")
    assert marker.by_user is True
    assert str(path) in marker.reason and "watchglass enable w" in marker.reason


def test_iso_time_renders_a_time_past_the_calendar_instead_of_raising():
    """A huge interval must not make status.json impossible to write for every watch."""
    assert isinstance(iso_time(time.time() + 8.64e13), str)
