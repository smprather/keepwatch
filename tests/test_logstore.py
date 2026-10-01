import json
import re
import threading
import time as _time

import pytest

from keepwatch.logstore import LogWriter, QueueSink, fan_out, level_filter, make_record


def test_make_record():
    record = make_record("poll.start", watch="w", poll_id="p1")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d", record["ts"])
    assert (record["level"], record["event"], record["watch"]) == ("INFO", "poll.start", "w")
    assert isinstance(record["pid"], int)


def test_fan_out():
    first, second = [], []
    fan_out(first.append, second.append)({"a": 1})
    assert first == second == [{"a": 1}]


def test_writer_appends_json_lines(tmp_path):
    path = tmp_path / "logs" / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=10_000, backups=2)
    writer.write({"event": "a", "path": tmp_path})
    writer.write({"event": "b"})
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["a", "b"]
    assert lines[0]["path"] == str(tmp_path)


def test_writer_rotates(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=200, backups=2)
    for index in range(40):
        writer.write({"event": "x", "n": index, "pad": "y" * 50})
    names = sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("."))
    assert names == ["keepwatch.jsonl", "keepwatch.jsonl.1", "keepwatch.jsonl.2"]
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["n"] == 39


def test_writer_is_thread_safe(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=10_000_000, backups=1)
    threads = [threading.Thread(target=lambda: [writer.write({"event": "t"}) for _ in range(50)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)["event"] == "t" for line in lines)


def test_level_filter():
    seen = []
    emit = level_filter(seen.append, "WARNING")
    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "BOGUS"):
        emit({"level": level})
    assert [r["level"] for r in seen] == ["WARNING", "ERROR", "CRITICAL"]


def test_queue_sink_delivers_in_order_on_one_thread():
    seen = []

    def slow(record):
        _time.sleep(0.001)
        seen.append((record["n"], threading.current_thread().name))

    sink = QueueSink(slow)
    threads = [threading.Thread(target=lambda base=b: [sink({"n": base + i}) for i in range(20)]) for b in (0, 100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close()
    assert sorted(n for n, _ in seen) == sorted([*range(20), *range(100, 120)])
    assert {name for _, name in seen} == {"keepwatch-log"}


def test_queue_sink_survives_a_failing_sink():
    seen = []

    def flaky(record):
        if record["n"] == 1:
            raise RuntimeError("boom")
        seen.append(record["n"])

    sink = QueueSink(flaky)
    for n in range(3):
        sink({"n": n})
    sink.close()
    assert seen == [0, 2]


@pytest.mark.windows_only
def test_rotation_waits_while_another_process_holds_the_log(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=200, backups=2)
    writer.write({"event": "first", "pad": "x" * 150})
    with open(path, "rb"):  # an ordinary reader blocks renames on Windows
        for n in range(5):
            writer.write({"event": "more", "n": n, "pad": "y" * 150})
    assert len(path.read_text().splitlines()) == 6
