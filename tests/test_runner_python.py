import os
import time
from pathlib import Path

from keepwatch import runner as runner_module
from keepwatch.runner import Runner
from keepwatch.state import Outcome


def gone(pid, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def test_check_answers_with_payload_logs_and_output(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            ctx.log.warning("two files")
            print("hello from check")
            return True, ["a", "b"]
    '''})
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.kind, result.target) == ("true", "python", "watch.py:check")
    assert result.outcome() is Outcome.TRUE
    assert result.payload == ["a", "b"]
    assert result.stdout == "hello from check\n"
    assert [m["message"] for m in result.messages if m["type"] == "log"] == ["two files"]
    record = result.to_record()
    assert "messages" not in record and record["status"] == "true"


def test_false_and_unknown(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        import os
        def check(ctx):
            return None if os.environ.get("ANSWER") == "none" else False
    '''})
    assert Runner().run(call_for(watch_dir)).status == "false"
    assert Runner().run(call_for(watch_dir, environment={"ANSWER": "none"})).status == "unknown"


def test_check_exception_is_error_and_action_exception_is_failed(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            1 / 0
        def on_true(ctx):
            raise RuntimeError("scp broke")
    '''})
    check = Runner().run(call_for(watch_dir))
    assert check.status == "error" and "ZeroDivisionError" in check.exception["traceback"]
    action = Runner().run(call_for(watch_dir, hook="on_true"))
    assert action.status == "failed" and action.reason == "RuntimeError: scp broke"
    assert action.succeeded is False


def test_action_success(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "ok" and result.succeeded


def test_import_error_is_reported(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "import does_not_exist_xyz\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("importing watch.py failed: ModuleNotFoundError")


def test_timeout_kills_the_whole_process_group(xdg, make_watch, call_for):
    watch_dir = make_watch("slow", files={"watch.py": '''
        import subprocess, time
        def check(ctx):
            child = subprocess.Popen(["sleep", "60"])
            (ctx.run_dir / "child.pid").write_text(str(child.pid))
            time.sleep(60)
    '''})
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.5))
    assert result.status == "timeout"
    assert result.reason == "timed out after 1.5s"
    assert result.duration < 10
    child_pid = int((xdg.run_dir(os.getpid(), "slow") / "child.pid").read_text())
    assert gone(child_pid)


def test_python_dependencies_not_supported_yet(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["requests"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert "python_dependencies is not supported" in result.reason


def test_version_mismatch_is_reported(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "_EXPECTED_VERSION", "0.0.0-test")
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    return True\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("keepwatch version mismatch")


def test_describe_mode(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    pass\n\ndef on_true(ctx):\n    pass\n"})
    result = Runner().run(call_for(watch_dir, mode="describe"))
    assert result.status == "ok" and result.hooks == ["check", "on_true"]


def test_keepwatch_environment_reaches_python_hooks(make_watch, call_for):
    watch_dir = make_watch("w", config='[environment]\nFROM_WATCH = "1"\n', files={"watch.py": '''
        import os
        def check(ctx):
            return True, {k: os.environ.get(k) for k in ("KEEPWATCH_WATCH", "KEEPWATCH_HOOK", "FROM_WATCH")}
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.payload == {"KEEPWATCH_WATCH": "w", "KEEPWATCH_HOOK": "check", "FROM_WATCH": "1"}
