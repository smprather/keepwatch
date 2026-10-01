import json

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.logstore import LogWriter

RECORDS = [
    {"ts": "2026-09-30T10:00:00.000+00:00", "level": "INFO", "event": "poll.start", "watch": "a",
     "poll_id": "aaa111", "condition": False, "faked": False, "dry_run": False},
    {"ts": "2026-09-30T10:00:01.000+00:00", "level": "ERROR", "event": "hook.end", "watch": "a",
     "poll_id": "aaa111", "hook": "on_true", "status": "failed", "duration": 1.0, "target": "watch.py:on_true",
     "reason": "boom"},
    {"ts": "2026-09-30T11:00:00.000+00:00", "level": "WARNING", "event": "poll.end", "watch": "b",
     "poll_id": "bbb222", "failed": True, "failures": 1, "dry_run": False},
]


def run(*args):
    return CliRunner().invoke(cli, list(args))


def seed(xdg):
    writer = LogWriter(xdg.log_file, max_bytes=10_000_000, backups=2)
    for record in RECORDS:
        writer.write(record)


def test_logs_json_and_filters(xdg):
    seed(xdg)

    def lines(*args):
        result = run("logs", "--json", *args)
        assert result.exit_code == 0, result.output
        return [json.loads(line) for line in result.output.splitlines()]

    assert len(lines()) == 3
    assert [r["event"] for r in lines("a")] == ["poll.start", "hook.end"]
    assert [r["event"] for r in lines("--failed")] == ["hook.end", "poll.end"]
    assert [r["poll_id"] for r in lines("--poll", "bbb")] == ["bbb222"]
    assert [r["event"] for r in lines("--event", "hook")] == ["hook.end"]
    assert [r["event"] for r in lines("-n", "1")] == ["poll.end"]
    assert [r["event"] for r in lines("--since", "2026-09-30T10:30:00+00:00")] == ["poll.end"]
    assert [r["event"] for r in lines("--level", "error")] == ["hook.end"]


def test_logs_human_output(xdg):
    seed(xdg)
    result = run("logs", "a")
    assert "on_true → failed (1.00s) watch.py:on_true: boom" in result.output


def test_logs_rejects_a_bad_since(xdg):
    result = run("logs", "--since", "soon")
    assert result.exit_code == 2
    assert "neither a duration" in result.output
