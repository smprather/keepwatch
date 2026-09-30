import json
import re
import threading

from keepwatch.logstore import LogWriter, fan_out, make_record


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
