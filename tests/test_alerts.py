from keepwatch.alerts import run_alert
from keepwatch.config import Command


def test_no_command_does_nothing():
    records = []
    run_alert(None, event="offline", watch="w", reason="r", environment={}, timeout=5, sink=records.append)
    assert records == []


def test_alert_receives_environment(tmp_path):
    out = tmp_path / "alert.txt"
    records = []
    command = Command(shell='echo "$KEEPWATCH_ALERT_EVENT|$KEEPWATCH_WATCH|$KEEPWATCH_ALERT_REASON" > "$OUT"')
    run_alert(command, event="offline", watch="psg", reason="5 failures", environment={"OUT": str(out)},
              timeout=5, sink=records.append)
    assert out.read_text() == "offline|psg|5 failures\n"
    (record,) = records
    assert (record["event"], record["status"], record["alert_event"], record["level"]) == ("alert.end", "ok", "offline", "INFO")


def test_failing_and_slow_alerts_are_recorded_not_raised(tmp_path):
    records = []
    run_alert(Command(shell="echo bad >&2; exit 3"), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    run_alert(Command(argv=("sleep", "5")), event="online", watch="w", reason="r", environment={},
              timeout=0.5, sink=records.append)
    run_alert(Command(argv=("/no/such/program",)), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    assert [(r["status"], r["level"]) for r in records] == [("failed", "WARNING"), ("timeout", "WARNING"), ("failed", "WARNING")]
    assert records[0]["stderr"] == "bad\n" and records[0]["exit_code"] == 3
    assert records[2]["reason"].startswith("cannot start alert_command")
