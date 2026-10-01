import json
import os
import signal
import subprocess
import sys
import time

from click.testing import CliRunner

from keepwatch.cli import cli

KEEPWATCH = [sys.executable, "-c", "from keepwatch.cli import main; main()"]

COUNTER = '''
    def check(ctx):
        return True

    def on_true(ctx):
        path = ctx.watch_dir / "count"
        count = int(path.read_text()) if path.exists() else 0
        path.write_text(str(count + 1))
'''


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_run_polls_and_stops_cleanly(xdg, make_watch):
    watch_dir = make_watch("w", config='interval = "1s"\n', files={"watch.py": COUNTER})
    count = watch_dir / "count"
    proc = subprocess.Popen([*KEEPWATCH, "run"], env=dict(os.environ), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert wait_for(lambda: count.exists() and int(count.read_text()) >= 2)
        second = subprocess.run([*KEEPWATCH, "run"], env=dict(os.environ), capture_output=True, text=True, timeout=30)
        assert second.returncode == 1
        assert "already running" in second.stdout + second.stderr
    finally:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, out + err
    events = [json.loads(line)["event"] for line in xdg.log_file.read_text().splitlines()]
    assert "service.start" in events
    assert events[-1] == "service.stop"
    assert json.loads(xdg.status_file.read_text())["service"]["running"] is False


def test_run_refuses_a_broken_global_config(xdg):
    xdg.config_home.mkdir(parents=True)
    xdg.config_file.write_text("watch_dirs = 5\n")
    result = CliRunner().invoke(cli, ["run"])
    assert result.exit_code == 1
    assert "'watch_dirs' must be a non-empty list" in result.output


def test_help_lists_the_run_panel(xdg):
    output = CliRunner().invoke(cli, ["--help"]).output
    assert "Run" in output and "run " in output
