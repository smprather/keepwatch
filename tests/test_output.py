import io
import sys

from keepwatch.output import format_record, plain_output, record_level, use_utf8_output

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
    assert '  payload: ["a"]' in format_record(record, level=2)


def test_format_poll_end_and_unknown_event():
    end = {**BASE, "event": "poll.end", "failed": True, "failures": 2, "dry_run": False}
    assert format_record(end) == "10:11:12 psg poll FAILED; failures 2"
    other = format_record({**BASE, "event": "custom.thing", "x": 1})
    assert other.startswith("10:11:12 psg custom.thing ")


def test_a_headline_event_is_a_plain_line_not_an_indented_log():
    record = {**BASE, "event": "plugin.event", "level": "INFO",
              "message": "found a.bin (4 KiB) on host", "fields": {"size": 4096}}
    assert format_record(record) == "10:11:12 psg found a.bin (4 KiB) on host"


def test_record_level_hides_routine_polls_before_normal_and_detail_before_verbose():
    def level(event, **fields):
        return record_level({**BASE, "event": event, **fields})

    assert level("poll.start", condition=False) == 2
    assert level("poll.end", failed=False) == 2
    assert level("poll.end", failed=True) == 0
    assert level("command", exit_code=0) == 2
    assert level("command", exit_code=1) == 0
    assert level("check.outcome", condition_before=False, condition_after=False) == 2
    assert level("check.outcome", condition_before=False, condition_after=True) == 1
    assert level("hook.end", status="ok") == 1
    assert level("hook.end", status="failed") == 0
    assert level("plugin.log", level="DEBUG") == 2
    assert level("plugin.log", level="INFO") == 1
    assert level("plugin.log", level="WARNING") == 0
    assert level("plugin.event", level="INFO") == 1
    assert level("plugin.event", level="WARNING") == 0
    assert level("watch.offline") == 0 and level("watch.online") == 0
    assert level("watch.added") == 1 and level("config.loaded") == 1
    assert level("observer.event") == 2 and level("observer.dropped") == 0
    assert level("alert.end", status="ok") == 0
    assert level("custom.thing") == 2


def test_failed_statuses_are_defined_once():
    from keepwatch import output, pollengine, runner

    assert runner.FAILED_STATUSES == frozenset({"error", "failed", "timeout"})
    assert not hasattr(output, "_FAILED_STATUSES")
    assert not hasattr(pollengine, "_FAILED")


def test_lifecycle_events_are_readable():
    service = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "INFO"}
    watch = {**service, "watch": "psg"}

    def fmt(record, level=1):
        return format_record(record, level=level)

    assert fmt({**service, "event": "service.start", "version": "1", "config": "/c.toml"}) == (
        "10:11:12 service started (version 1, config /c.toml)"
    )
    assert fmt({**service, "event": "service.stop"}) == "10:11:12 service stopped"
    assert fmt({**service, "event": "config.loaded", "path": "/c.toml"}) == "10:11:12 config reloaded: /c.toml"
    assert fmt({**watch, "event": "config.error", "error": "a.toml:2: bad\nb.toml:3: worse",
                "running_previous": True}) == (
        "10:11:12 psg config error (still running the previous config): a.toml:2: bad\n"
        "  error:\n    a.toml:2: bad\n    b.toml:3: worse"
    )
    assert fmt({**service, "event": "config.error", "error": "x: y"}) == "10:11:12 config error: x: y"
    assert fmt({**watch, "event": "watch.added", "enabled": False}) == "10:11:12 psg watch added (parked: enabled = false)"
    assert fmt({**watch, "event": "watch.added", "enabled": True}) == "10:11:12 psg watch added"
    assert fmt({**watch, "event": "watch.changed"}) == "10:11:12 psg config changed; applies from the next poll"
    assert fmt({**watch, "event": "watch.removed"}) == "10:11:12 psg watch removed"
    assert fmt({**watch, "event": "watch.offline", "reason": "5 consecutive failed polls",
                "last_failure": "on_true failed: boom"}) == (
        "10:11:12 psg OFFLINE: 5 consecutive failed polls; last failure: on_true failed: boom"
    )
    assert fmt({**watch, "event": "watch.online", "reason": "enabled by user"}) == (
        "10:11:12 psg back online: enabled by user"
    )
    assert fmt({**watch, "event": "watch.crash", "error": "KeyError: 'x'", "traceback": "Traceback\n  boom"}) == (
        "10:11:12 psg keepwatch internal error: KeyError: 'x'\n  traceback:\n    Traceback\n      boom"
    )
    assert fmt({**watch, "event": "alert.end", "alert_event": "offline", "status": "ok", "duration": 0.5}) == (
        "10:11:12 psg alert_command (offline) → ok (0.50s)"
    )
    assert fmt({**watch, "event": "alert.end", "alert_event": "online", "status": "failed", "duration": 0.1,
                "reason": "exit code 3", "stderr": "nope\n"}) == (
        "10:11:12 psg alert_command (online) → failed (0.10s): exit code 3\n  stderr:\n    nope"
    )


def test_service_error_is_readable():
    record = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "ERROR", "event": "service.error",
              "error": "OSError: disk full", "traceback": "Traceback"}
    assert format_record(record) == "10:11:12 service error: OSError: disk full"


def test_cli_error_is_readable():
    record = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "CRITICAL", "event": "cli.error", "error": "x: y"}
    assert format_record(record) == "10:11:12 command failed: x: y"


def test_use_utf8_output_reconfigures_a_code_page_stream(monkeypatch):
    """A Windows pipe is encoded with the console code page (cp1252), which cannot write keepwatch's arrows."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline=""))
    use_utf8_output()
    stream.write("hook x → ok")
    stream.flush()
    assert buffer.getvalue() == "hook x → ok".encode("utf-8")


def test_use_utf8_output_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # a test harness: no encoding, no reconfigure
    monkeypatch.setattr(sys, "stderr", None)  # pythonw: no streams at all
    use_utf8_output()


def test_format_record_neutralises_escape_sequences_from_captured_output():
    """Hook output is untrusted: ESC/OSC must never reach the terminal, but must stay diagnosable."""
    record = {
        **BASE,
        "event": "hook.end",
        "hook": "on_true",
        "status": "failed",
        "duration": 0.5,
        "target": "watch.py:on_true",
        "stderr": "\x1b]52;c;QQ==\x1b\\ retitled\x07 and \x1b[2Jcleared\n",
    }
    line = format_record(record)
    assert "\x1b" not in line and "\x07" not in line
    assert "\\x1b]52" in line  # shown as text, not acted on
    assert line.endswith("\n") or "cleared" in line  # the trailing newline still renders as a block


def test_format_record_neutralises_bidi_overrides_in_a_file_name():
    """A right-to-left override can reorder a line so it reads as something else entirely."""
    record = {**BASE, "event": "hook.end", "hook": "on_true", "status": "ok", "duration": 0.1,
              "target": "/tmp/a\u202egpj.exe", "stdout": ""}
    line = format_record(record)
    assert "\u202e" not in line and "\\u202e" in line


def test_verbose_blocks_keep_their_newlines():
    """Sanitizing must not flatten the indented blocks the formatter builds."""
    record = {**BASE, "event": "hook.end", "hook": "on_true", "status": "failed", "duration": 0.1,
              "target": "t", "stderr": "one\ntwo\n"}
    line = format_record(record, level=2)
    assert "\n    one\n    two" in line
