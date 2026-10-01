import json

from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import write_json_atomic


def test_write_json_atomic(tmp_path):
    target = tmp_path / "a" / "b.json"
    write_json_atomic(target, {"x": 1})
    assert json.loads(target.read_text()) == {"x": 1}
    assert [p.name for p in target.parent.iterdir()] == ["b.json"]


def test_iso_time_round_trips():
    marker = OfflineMarker(reason="r", since=iso_time(1_000_000.5))
    assert abs(marker.since_epoch() - 1_000_000.5) < 0.001


def test_offline_marker_round_trip(xdg):
    assert read_offline(xdg, "w") is None
    marker = OfflineMarker(reason="5 consecutive failed polls", since=iso_time(1_000_000), last_failure="on_true failed: x")
    write_offline(xdg, "w", marker)
    assert read_offline(xdg, "w") == marker
    assert json.loads(xdg.offline_file("w").read_text())["by_user"] is False
    assert clear_offline(xdg, "w") is True
    assert clear_offline(xdg, "w") is False
    assert read_offline(xdg, "w") is None


def test_unreadable_marker_counts_as_offline_by_user(xdg):
    path = xdg.offline_file("w")
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    marker = read_offline(xdg, "w")
    assert marker.by_user is True
    assert str(path) in marker.reason and "keepwatch enable w" in marker.reason
