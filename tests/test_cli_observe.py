import json

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.output import format_record
from portable import PY, literal

FEED = """
import json, time
print(json.dumps({"n": 1}))
print(json.dumps({"n": 2}), flush=True)
time.sleep(60)
"""
SILENT = "import time\ntime.sleep(60)\n"
NAMES_WATCH = """
    def check(ctx):
        return True, [event["observer"] + ":" + event["name"] for event in ctx.events]
"""


def run(*args):
    return CliRunner().invoke(cli, list(args))


def observed(make_watch, script, name="feed"):
    config = f"[observe.{name}]\nkind = 'command'\ncommand = [{literal(PY)}, 'source.py']\n"
    return make_watch("w", config=config, files={"source.py": script})


def test_observe_prints_events_as_json_lines(make_watch):
    observed(make_watch, FEED)
    result = run("observe", "w", "--count", "2", "--for", "30s")
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [line["n"] for line in lines] == [1, 2]
    assert all(line["observer"] == "feed" and line["received"] for line in lines)
    assert "observer feed started" in result.stderr


def test_observe_for_a_while(make_watch):
    observed(make_watch, SILENT)
    result = run("observe", "w", "feed", "--for", "1s")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def test_observe_unknown_observer(make_watch):
    observed(make_watch, SILENT)
    result = run("observe", "w", "nope", "--for", "1s")
    assert result.exit_code == 1
    assert "watch 'w' has no observer 'nope'; its observers: feed" in result.output


def test_observe_a_watch_without_observers(make_watch):
    make_watch("w", config="")
    result = run("observe", "w", "--for", "1s")
    assert result.exit_code == 1
    assert "has no observers" in result.output and "keepwatch docs observers" in result.output


def test_observe_bad_options(make_watch):
    observed(make_watch, SILENT)
    assert run("observe", "w", "--for", "soon").exit_code == 2
    assert run("observe", "w", "--count", "0").exit_code == 2


def test_poll_events_option(make_watch):
    make_watch("w", files={"watch.py": NAMES_WATCH})
    result = run("poll", "w", "--events", '[{"name": "a"}, {"name": "b", "observer": "inbox"}]', "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["polls"][0]["payload"] == ["manual:a", "inbox:b"]


def test_poll_events_must_be_an_array_of_objects(make_watch):
    make_watch("w", files={"watch.py": NAMES_WATCH})
    for bad in ('{"name": "a"}', "[1]", "not json"):
        result = run("poll", "w", "--events", bad)
        assert result.exit_code == 2, bad


def test_observer_records_are_formatted():
    stopped = {
        "ts": "2026-10-01T12:00:00.000+00:00",
        "event": "observer.stopped",
        "watch": "w",
        "observer": "feed",
        "reason": "exited",
        "exit_code": 255,
        "stderr_tail": ["ssh: connect to host x port 22: Connection refused"],
    }
    text = format_record(stopped)
    assert "observer feed stopped: exited [exit 255]" in text and "Connection refused" in text
    restarting = {"event": "observer.restarting", "observer": "feed", "delay": 10.0}
    assert "observer feed restarting in 10s" in format_record(restarting)
    started = {"event": "observer.started", "observer": "in", "kind": "files", "path": "/x", "native": True}
    assert "observer in started (files): /x" in format_record(started)


def test_observe_prints_each_event_once(make_watch):
    observed(make_watch, FEED)
    result = run("observe", "w", "--count", "2", "--for", "30s")
    assert result.exit_code == 0, result.output
    assert "event:" not in result.stderr
    assert result.output.count('"n": 1') == 1
