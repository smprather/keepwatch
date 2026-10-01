import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from keepwatch.logquery import LogFilter, follow_log, log_files, parse_when, read_records, select_records


def write_lines(path, records):
    with open(path, "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_rotated_files_are_read_oldest_first(tmp_path):
    current = tmp_path / "keepwatch.jsonl"
    write_lines(tmp_path / "keepwatch.jsonl.2", [{"n": 1}])
    write_lines(tmp_path / "keepwatch.jsonl.1", [{"n": 2}])
    write_lines(current, [{"n": 3}])
    with open(current, "a") as handle:
        handle.write("not json\n[1]\n")
    assert [p.name for p in log_files(current)] == ["keepwatch.jsonl.2", "keepwatch.jsonl.1", "keepwatch.jsonl"]
    assert [r["n"] for r in read_records(current)] == [1, 2, 3]


def test_parse_when():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert parse_when("1h", now) == now - timedelta(hours=1)
    assert parse_when("2026-09-30T10:00:00+00:00", now) == datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    assert parse_when("2026-09-30", now).tzinfo is not None
    with pytest.raises(ValueError, match="neither a duration"):
        parse_when("soon", now)


def test_filters_and_limit(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [
        {"ts": "2026-09-30T10:00:00+00:00", "level": "INFO", "event": "poll.start", "watch": "a", "poll_id": "aaa1"},
        {"ts": "2026-09-30T10:00:01+00:00", "level": "ERROR", "event": "hook.end", "watch": "a", "poll_id": "aaa1",
         "status": "failed"},
        {"ts": "2026-09-30T11:00:00+00:00", "level": "INFO", "event": "poll.start", "watch": "b", "poll_id": "bbb2"},
        {"ts": "2026-09-30T11:00:01+00:00", "level": "WARNING", "event": "poll.end", "watch": "b", "poll_id": "bbb2",
         "failed": True},
    ])

    def pick(**kwargs):
        return [(r["watch"], r["event"]) for r in select_records(path, LogFilter(**kwargs), 0)]

    assert pick(watch="a") == [("a", "poll.start"), ("a", "hook.end")]
    assert pick(failed=True) == [("a", "hook.end"), ("b", "poll.end")]
    assert pick(events=("poll",)) == [("a", "poll.start"), ("b", "poll.start"), ("b", "poll.end")]
    assert pick(poll_id="bbb") == [("b", "poll.start"), ("b", "poll.end")]
    assert pick(min_level="WARNING") == [("a", "hook.end"), ("b", "poll.end")]
    since = datetime(2026, 9, 30, 10, 30, tzinfo=timezone.utc)
    assert pick(since=since) == [("b", "poll.start"), ("b", "poll.end")]
    assert [r["event"] for r in select_records(path, LogFilter(), 1)] == ["poll.end"]


def follow_until(path, count, action):
    seen = []
    thread = threading.Thread(
        target=follow_log,
        args=(path, seen.append),
        kwargs={"stop": lambda: len(seen) >= count, "interval": 0.05},
    )
    thread.start()
    time.sleep(0.3)
    action()
    thread.join(10)
    return seen


def test_follow_emits_new_records_only(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [{"n": 0}])
    seen = follow_until(path, 2, lambda: write_lines(path, [{"n": 1}, {"n": 2}]))
    assert [r["n"] for r in seen] == [1, 2]


def test_follow_survives_rotation(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    write_lines(path, [{"n": 0}])

    def rotate():
        write_lines(path, [{"n": 1}])
        time.sleep(0.3)
        path.rename(tmp_path / "keepwatch.jsonl.1")
        write_lines(path, [{"n": 2}])

    seen = follow_until(path, 2, rotate)
    assert [r["n"] for r in seen] == [1, 2]
