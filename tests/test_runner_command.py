import json
import os
import threading
import time
from pathlib import Path

import pytest

from keepwatch.platform import IS_WINDOWS
from keepwatch.runner import Runner
from portable import exits, py, script

ACT_PY = '''
    import json, os, pathlib
    env = os.environ
    seen = {
        "condition": env["KEEPWATCH_CONDITION"],
        "hook": env["KEEPWATCH_HOOK"],
        "dest": env["KEEPWATCH_SETTING_DEST"],
        "extra": env["EXTRA"],
        "global": env["GLOBAL_ONLY"],
        "payload": json.loads(pathlib.Path(env["KEEPWATCH_PAYLOAD_FILE"]).read_text()),
        "settings": json.loads(pathlib.Path(env["KEEPWATCH_SETTINGS_FILE"]).read_text()),
        "cwd": os.getcwd(),
    }
    pathlib.Path(env["KEEPWATCH_RUN_DIR"], "seen.json").write_text(json.dumps(seen))
'''

SLEEP = py("import time; time.sleep(30)")


def test_exit_codes_map_to_outcomes(make_watch, call_for):
    check = py('import os, sys; sys.exit(int(os.environ["CODE"]))')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n[check_exit_codes]\nunknown = [3]\n")
    for code, status in ((0, "true"), (1, "false"), (3, "unknown"), (7, "error")):
        result = Runner().run(call_for(watch_dir, environment={"CODE": str(code)}))
        assert (result.status, result.exit_code, result.kind) == (status, code, "command")
    assert "exit code 7 is not listed in [check_exit_codes]" in result.reason


def test_string_check_runs_in_the_platform_shell(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "exit 3"\n[check_exit_codes]\nunknown = [3]\n')
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_check_passes_a_payload(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.py"]\n', files={"check.py": script('''
        import os, pathlib
        pathlib.Path(os.environ["KEEPWATCH_PAYLOAD_OUT"]).write_text('["a.tar.gz", "b.tar.gz"]')
    ''')})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.payload == ["a.tar.gz", "b.tar.gz"]
    assert result.target == "./check.py"


def test_invalid_payload_json_is_an_error(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.py"]\n', files={"check.py": script('''
        import os, pathlib
        pathlib.Path(os.environ["KEEPWATCH_PAYLOAD_OUT"]).write_text("not json")
    ''')})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON")


def test_action_sees_environment_payload_and_settings(xdg, make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        on_true = ["./act.py"]
        [environment]
        EXTRA = "watch-level"
        [settings]
        dest = "foo@bar:/in/"
    ''', files={"act.py": script(ACT_PY)})
    result = Runner().run(call_for(watch_dir, hook="on_true", condition=True, payload=["a.tar.gz"],
                                   environment={"GLOBAL_ONLY": "yes"}))
    assert result.status == "ok", (result.reason, result.stderr)
    run_dir = xdg.run_dir(os.getpid(), "cmd")
    seen = json.loads((run_dir / "seen.json").read_text())
    assert Path(seen.pop("cwd")) == watch_dir
    assert seen == {
        "condition": "true",
        "hook": "on_true",
        "dest": "foo@bar:/in/",
        "extra": "watch-level",
        "global": "yes",
        "payload": ["a.tar.gz"],
        "settings": {"dest": "foo@bar:/in/"},
    }
    assert sorted(p.name for p in run_dir.iterdir()) == ["seen.json"]


def test_failed_action(make_watch, call_for):
    action = py('import sys; sys.stderr.write("oops"); sys.exit(4)')
    watch_dir = make_watch("cmd", config=f"[hooks]\non_false = {action}\n")
    result = Runner().run(call_for(watch_dir, hook="on_false"))
    assert (result.status, result.exit_code, result.stderr) == ("failed", 4, "oops")
    assert result.reason == "exit code 4"


def test_missing_executable(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = ["./nope.exe"]\n')
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason.startswith("cannot start command")


@pytest.mark.posix_only
def test_killed_by_signal(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "kill -TERM $$"\n')
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.signal, result.reason) == ("error", 15, "killed by signal SIGTERM")


def test_timeout(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.0))
    assert result.status == "timeout" and result.duration < 10


def test_background_child_holding_stdout_does_not_hang(make_watch, call_for):
    check = py(
        'import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
        'print("started")'
    )
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout.strip() == "started"
    assert result.duration < 10


def test_binary_output_is_decoded_with_replacement(make_watch, call_for):
    check = py('import sys; sys.stdout.buffer.write(bytes([255]) + b"ok")')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "\ufffdok"


def test_output_is_clipped(make_watch, call_for):
    check = py('import sys; sys.stdout.write("x" * 10000)')
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {check}\n")
    result = Runner().run(call_for(watch_dir, capture_bytes=1000))
    assert result.stdout_truncated is True
    assert "bytes omitted" in result.stdout
    assert len(result.stdout) < 1200


def test_unwritable_run_dir_is_a_failed_hook(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\non_true = {exits(0)}\n")
    call = call_for(watch_dir, hook="on_true")
    call.run_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    call.run_dir.parent.write_text("not a directory")
    result = Runner().run(call)
    assert result.status == "failed"
    assert result.reason.startswith("cannot prepare the hook's files in")


def test_terminate_all_stops_running_hooks(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    runner = Runner()
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(1.0)
    runner.terminate_all()
    thread.join(10)
    assert results and results[0].status == "error"
    if IS_WINDOWS:
        assert results[0].reason == "terminated by keepwatch"
    else:
        assert results[0].signal == 15


@pytest.mark.posix_only
def test_close_refuses_new_hooks_and_kill_all_stops_stubborn_ones(make_watch, call_for):
    watch_dir = make_watch("cmd", config="[hooks]\ncheck = 'trap \"\" TERM; sleep 30'\n")
    runner = Runner(kill_grace=30)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(0.5)
    runner.close()
    thread.join(1.0)
    assert thread.is_alive()
    runner.kill_all()
    thread.join(10)
    assert results[0].signal == 9
    refused = runner.run(call_for(watch_dir))
    assert (refused.status, refused.reason) == ("error", "keepwatch is shutting down")


@pytest.mark.windows_only
def test_close_terminates_at_once_and_refuses_new_hooks_on_windows(make_watch, call_for):
    watch_dir = make_watch("cmd", config=f"[hooks]\ncheck = {SLEEP}\n")
    runner = Runner(kill_grace=30)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(1.0)
    runner.close()
    thread.join(10)
    assert not thread.is_alive()
    assert results[0].reason == "terminated by keepwatch"
    refused = runner.run(call_for(watch_dir))
    assert (refused.status, refused.reason) == ("error", "keepwatch is shutting down")
