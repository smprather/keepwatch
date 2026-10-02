import time

import pytest

from keepwatch import observers
from keepwatch.config import load_watch_config
from keepwatch.observers import EventQueue, build_observer, parse_event
from portable import PY, literal

EMITTER = """
import json, sys, time
print(json.dumps({"event": "file", "name": "a"}))
print("plain text")
print(json.dumps({"event": "heartbeat"}))
print("to stderr", file=sys.stderr, flush=True)
time.sleep(60)
"""
SILENT = "import time\ntime.sleep(60)\n"
BEATER = """
import json, time
while True:
    print(json.dumps({"event": "heartbeat"}), flush=True)
    time.sleep(0.3)
"""
ECHO_STDIN = """
import sys, time
for line in sys.stdin:
    print(line.strip(), flush=True)
print("eof", flush=True)
time.sleep(60)
"""
SHOW_ENV = """
import json, os, time
names = ("KEEPWATCH_WATCH", "KEEPWATCH_OBSERVER", "KEEPWATCH_DATA_DIR", "FROM_WATCH")
print(json.dumps({name: os.environ.get(name) for name in names}), flush=True)
time.sleep(60)
"""
NOISY = """
import sys
for number in range(10):
    print(number, file=sys.stderr)
"""
LONG_LINE = """
import time
print("x" * 300)
print("ok", flush=True)
time.sleep(60)
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(observers, "BACKOFF_START", 0.2)
    monkeypatch.setattr(observers, "BACKOFF_CAP", 0.4)


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def kinds(records, name):
    return [record for record in records if record["event"] == name]


def command_observer(make_watch, xdg, script=None, *, argv=None, extra=""):
    command = argv or f"[{literal(PY)}, 'source.py']"
    config = f"[observe.src]\nkind = 'command'\ncommand = {command}\n{extra}"
    watch_dir = make_watch("w", config=config, files={"source.py": script} if script else None)
    watch = load_watch_config(watch_dir)
    events, records = [], []
    observer = build_observer(
        watch,
        watch.observers["src"],
        deliver=events.append,
        sink=records.append,
        environment={},
        data_dir=xdg.watch_data_dir("w"),
    )
    return observer, events, records


def stop(observer):
    observer.stop()
    assert observer.join(15)


def test_parse_event():
    assert parse_event('{"event": "file", "name": "a"}\n') == {"event": "file", "name": "a"}
    assert parse_event("hello world\n") == {"line": "hello world"}
    assert parse_event("[1, 2]\r\n") == {"line": "[1, 2]"}
    assert parse_event('{"event": "heartbeat"}\n') is None
    assert parse_event("  \n") is None


def test_event_queue_ack_keeps_later_events():
    queue = EventQueue(cap=3)
    queue.put({"n": 1})
    queue.put({"n": 2})
    mark, pending = queue.pending()
    assert pending == [{"n": 1}, {"n": 2}]
    queue.put({"n": 3})
    queue.ack(mark)
    assert queue.pending()[1] == [{"n": 3}] and len(queue) == 1


def test_event_queue_drops_the_oldest():
    queue = EventQueue(cap=2)
    assert queue.put({"n": 1}) == 0
    queue.put({"n": 2})
    assert queue.put({"n": 3}) == 1
    assert queue.pending()[1] == [{"n": 2}, {"n": 3}] and queue.dropped == 1


def test_command_observer_delivers_events(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, EMITTER)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 2 and kinds(records, "observer.output"))
        assert observer.running
    finally:
        stop(observer)
    assert [event.get("name") or event.get("line") for event in events] == ["a", "plain text"]
    assert all(event["observer"] == "src" and event["received"] for event in events)
    assert observer.last_event is not None
    assert kinds(records, "observer.output")[0]["text"] == "to stderr"
    assert kinds(records, "observer.started")[0]["kind"] == "command"
    [stopped] = kinds(records, "observer.stopped")
    assert stopped["reason"] == "stopped" and stopped["level"] == "INFO"
    assert not kinds(records, "observer.restarting")
    assert len(kinds(records, "observer.event")) == 2


def test_command_observer_restarts_after_exit(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv=f"[{literal(PY)}, '-c', 'print(1)']")
    observer.start()
    try:
        assert wait_for(lambda: len(events) >= 2)
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["reason"] == "exited" and first["exit_code"] == 0 and first["level"] == "WARNING"
    assert kinds(records, "observer.restarting")[0]["delay"] == 0.2
    assert observer.restarts >= 1
    assert events[0]["line"] == "1"


def test_restart_delays_double_up_to_the_cap(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv=f"[{literal(PY)}, '-c', 'pass']")
    observer.start()
    try:
        assert wait_for(lambda: len(kinds(records, "observer.restarting")) >= 3)
    finally:
        stop(observer)
    assert [record["delay"] for record in kinds(records, "observer.restarting")][:3] == [0.2, 0.4, 0.4]


def test_a_program_that_cannot_start_is_retried(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, argv="['./does-not-exist']")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.restarting"))
    finally:
        stop(observer)
    first = kinds(records, "observer.stopped")[0]
    assert first["reason"].startswith("cannot start") and first["exit_code"] is None
    assert not kinds(records, "observer.started")


def test_heartbeat_timeout_restarts_a_silent_program(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SILENT, extra="heartbeat_timeout = '1s'\n")
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.stopped"))
    finally:
        stop(observer)
    assert "heartbeat_timeout" in kinds(records, "observer.stopped")[0]["reason"]


def test_heartbeats_keep_a_program_alive_and_are_not_delivered(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, BEATER, extra="heartbeat_timeout = '1s'\n")
    observer.start()
    try:
        time.sleep(2.5)
        assert not kinds(records, "observer.stopped")
    finally:
        stop(observer)
    assert events == []


def test_stdin_is_written_then_closed(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, ECHO_STDIN, extra='stdin = "one\\ntwo\\n"\n')
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 3)
    finally:
        stop(observer)
    assert [event["line"] for event in events] == ["one", "two", "eof"]


def test_observer_environment(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SHOW_ENV, extra="[environment]\nFROM_WATCH = 'yes'\n")
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    seen = events[0]
    assert (seen["KEEPWATCH_WATCH"], seen["KEEPWATCH_OBSERVER"], seen["FROM_WATCH"]) == ("w", "src", "yes")
    assert seen["KEEPWATCH_DATA_DIR"] == str(xdg.watch_data_dir("w"))
    assert xdg.watch_data_dir("w").is_dir()


def test_stderr_records_are_rate_limited(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "OUTPUT_RECORDS_PER_MINUTE", 3)
    observer, events, records = command_observer(make_watch, xdg, NOISY)
    observer.start()
    try:
        assert wait_for(lambda: kinds(records, "observer.stopped"))
    finally:
        stop(observer)
    outputs = kinds(records, "observer.output")
    assert [record["text"] for record in outputs[:3]] == ["0", "1", "2"]
    assert outputs[3]["suppressed"] == 7 and outputs[3]["level"] == "WARNING"
    assert kinds(records, "observer.stopped")[0]["stderr_tail"] == ["5", "6", "7", "8", "9"]


def test_overlong_stdout_lines_are_dropped(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "MAX_LINE", 100)
    observer, events, records = command_observer(make_watch, xdg, LONG_LINE)
    observer.start()
    try:
        assert wait_for(lambda: events)
    finally:
        stop(observer)
    assert events[0]["line"] == "ok" and len(events) == 1
    assert any("longer than 100 bytes" in record["text"] for record in kinds(records, "observer.output"))


def test_event_queue_is_capped_by_bytes():
    queue = EventQueue(cap=100, max_bytes=250)
    big = {"line": "x" * 100}
    assert [queue.put(dict(big, n=n)) for n in range(4)] == [0, 0, 1, 1]
    assert [event["n"] for event in queue.pending()[1]] == [2, 3]
    assert queue.dropped == 2 and queue.bytes <= 250
    mark, _ = queue.pending()
    queue.ack(mark)
    assert len(queue) == 0 and queue.bytes == 0


def test_event_queue_keeps_an_oversized_newest_event():
    queue = EventQueue(cap=100, max_bytes=10)
    queue.put({"n": 1})
    assert queue.put({"line": "x" * 100}) == 1
    assert queue.pending()[1] == [{"line": "x" * 100}]


def test_large_events_are_logged_clipped(make_watch, xdg, monkeypatch):
    monkeypatch.setattr(observers, "EVENT_LOG_BYTES", 50)
    script = 'import json, time\nprint(json.dumps({"n": 1}))\nprint(json.dumps({"line2": "y" * 200}), flush=True)\ntime.sleep(60)\n'
    observer, events, records = command_observer(make_watch, xdg, script)
    observer.start()
    try:
        assert wait_for(lambda: len(events) == 2)
    finally:
        stop(observer)
    small, large = kinds(records, "observer.event")
    assert small["data"] == {"n": 1} and "data_clipped" not in small
    assert "data" not in large and len(large["data_clipped"]) < 120
    assert events[1]["line2"] == "y" * 200


def test_a_stopped_observer_delivers_nothing(make_watch, xdg):
    observer, events, records = command_observer(make_watch, xdg, SILENT)
    observer.stop()
    observer.emit({"n": 1})
    assert events == [] and not kinds(records, "observer.event")


def test_events_that_cannot_be_utf8_become_lines():
    text = '{"event": "file", "name": "caf\\udce9.gz"}\n'
    assert parse_event(text) == {"line": text.rstrip("\n")}
