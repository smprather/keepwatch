import sys

from keepwatch.alerts import run_alert
from keepwatch.config import Command


def test_no_command_does_nothing():
    records = []
    run_alert(None, event="offline", watch="w", reason="r", environment={}, timeout=5, sink=records.append)
    assert records == []


def test_alert_receives_environment(tmp_path):
    out = tmp_path / "alert.txt"
    records = []
    code = (
        'import os, pathlib; e = os.environ; pathlib.Path(e["OUT"]).write_text('
        '"|".join([e["KEEPWATCH_ALERT_EVENT"], e["KEEPWATCH_WATCH"], e["KEEPWATCH_ALERT_REASON"]]))'
    )
    run_alert(Command(argv=(sys.executable, "-c", code)), event="offline", watch="psg", reason="5 failures",
              environment={"OUT": str(out)}, timeout=10, sink=records.append)
    assert out.read_text() == "offline|psg|5 failures"
    (record,) = records
    assert (record["event"], record["status"], record["alert_event"], record["level"]) == ("alert.end", "ok", "offline", "INFO")


def test_alert_string_runs_in_the_platform_shell():
    records = []
    run_alert(Command(shell="exit 3"), event="online", watch="w", reason="r", environment={}, timeout=10,
              sink=records.append)
    assert (records[0]["status"], records[0]["exit_code"]) == ("failed", 3)


def test_failing_and_slow_alerts_are_recorded_not_raised(tmp_path):
    records = []
    failing = (sys.executable, "-c", 'import sys; sys.stderr.write("bad"); sys.exit(3)')
    run_alert(Command(argv=failing), event="online", watch="w", reason="r", environment={},
              timeout=10, sink=records.append)
    run_alert(Command(argv=(sys.executable, "-c", "import time; time.sleep(5)")), event="online", watch="w",
              reason="r", environment={}, timeout=0.5, sink=records.append)
    run_alert(Command(argv=("/no/such/program",)), event="online", watch="w", reason="r", environment={},
              timeout=5, sink=records.append)
    assert [(r["status"], r["level"]) for r in records] == [("failed", "WARNING"), ("timeout", "WARNING"), ("failed", "WARNING")]
    assert records[0]["stderr"] == "bad" and records[0]["exit_code"] == 3
    assert records[2]["reason"].startswith("cannot start alert_command")


def test_alert_list_programs_are_relative_to_the_config_dir(tmp_path):
    helper = tmp_path / "alert.py"
    helper.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(4)\n")
    helper.chmod(0o755)
    records = []
    run_alert(Command(argv=("./alert.py",)), event="offline", watch="w", reason="r", environment={},
              timeout=10, sink=records.append, base=tmp_path)
    assert (records[0]["status"], records[0]["exit_code"]) == ("failed", 4)
