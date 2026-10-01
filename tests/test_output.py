import io

from keepwatch.output import format_record, plain_output

BASE = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "INFO", "watch": "psg", "poll_id": "abc123"}


def test_plain_output_detection(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert plain_output(io.StringIO()) is True
    monkeypatch.setenv("NO_COLOR", "1")

    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert plain_output(Tty()) is True
    monkeypatch.delenv("NO_COLOR")
    assert plain_output(Tty()) is False


def test_format_poll_start():
    line = format_record({**BASE, "event": "poll.start", "condition": False, "faked": True, "dry_run": False})
    assert line == "10:11:12 psg poll abc123 start: condition FALSE [fake]"


def test_format_hook_end_shows_output_on_failure():
    record = {
        **BASE,
        "event": "hook.end",
        "hook": "on_true",
        "status": "failed",
        "duration": 1.234,
        "target": "watch.py:on_true",
        "reason": "CommandFailed: command exited 1: scp a b",
        "exit_code": None,
        "signal": None,
        "stdout": "",
        "stderr": "Permission denied\n",
        "exception": None,
    }
    text = format_record(record)
    assert text.startswith("10:11:12 psg on_true → failed (1.23s) watch.py:on_true: CommandFailed")
    assert "  stderr:\n    Permission denied" in text


def test_format_check_outcome():
    record = {
        **BASE,
        "event": "check.outcome",
        "outcome": "true",
        "reason": None,
        "payload": ["a"],
        "condition_before": False,
        "condition_after": True,
        "actions": ["on_rise", "on_true"],
    }
    assert format_record(record) == "10:11:12 psg outcome true; condition FALSE → TRUE; actions: on_rise, on_true"
    assert '  payload: ["a"]' in format_record(record, verbose=True)


def test_format_poll_end_and_unknown_event():
    end = {**BASE, "event": "poll.end", "failed": True, "failures": 2, "dry_run": False}
    assert format_record(end) == "10:11:12 psg poll FAILED; failures 2"
    other = format_record({**BASE, "event": "custom.thing", "x": 1})
    assert other.startswith("10:11:12 psg custom.thing ")


def test_failed_statuses_are_defined_once():
    from keepwatch import output, pollengine, runner

    assert runner.FAILED_STATUSES == frozenset({"error", "failed", "timeout"})
    assert not hasattr(output, "_FAILED_STATUSES")
    assert not hasattr(pollengine, "_FAILED")
