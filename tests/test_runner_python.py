import dataclasses
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from keepwatch import runner as runner_module
from keepwatch.config import load_watch_config
from keepwatch.runner import Runner, make_shim
from keepwatch.state import Outcome

network = pytest.mark.skipif(
    os.environ.get("KEEPWATCH_SKIP_NETWORK") == "1" or shutil.which("uv") is None,
    reason="needs uv and network access",
)


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


def test_dependencies_without_uv_are_reported(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["six"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    runner = Runner()
    runner.uv = None
    result = runner.run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("python_dependencies needs uv on PATH")
    assert runner.prepare_environment(load_watch_config(watch_dir)).startswith("python_dependencies needs uv on PATH")


def test_make_shim_links_the_running_package(tmp_path):
    import keepwatch

    shim = make_shim(tmp_path / "lib")
    assert (shim / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent
    assert make_shim(tmp_path / "lib") == shim


@pytest.mark.network
@network
def test_dependencies_are_installed_and_importable(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["six==1.16.0"]\n', files={"watch.py": '''
        import six

        def check(ctx):
            return True, six.__version__
    '''})
    runner = Runner()
    assert runner.prepare_environment(load_watch_config(watch_dir)) is None
    result = runner.run(call_for(watch_dir, timeout=120))
    assert result.status == "true", (result.reason, result.stderr)
    assert result.payload == "1.16.0"


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


def test_huge_log_volume_is_capped(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "RESULT_LIMIT", 100_000)
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            for _ in range(2000):
                ctx.log.info("x" * 200)
            return True
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    notes = [m["message"] for m in result.messages if m.get("logger") == "keepwatch.worker"]
    assert any("beyond the 100000-byte limit" in note for note in notes)
    assert sum(1 for m in result.messages if m.get("type") == "log") < 1000


def test_unexpected_runner_errors_become_failed_results(make_watch, call_for, monkeypatch):
    watch_dir = make_watch("w", config='[hooks]\non_true = "true"\n')

    def boom(self, call, command):
        raise RuntimeError("boom")

    monkeypatch.setattr(Runner, "_run_command", boom)
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason == "keepwatch internal error: RuntimeError: boom"
    assert "RuntimeError" in result.exception["traceback"]


def test_one_giant_message_is_dropped_not_buffered(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "RESULT_LIMIT", 100_000)
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            ctx.log.info("y" * 300_000)
            ctx.log.info("after")
            return True
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    logs = [m["message"] for m in result.messages if m.get("type") == "log"]
    assert "after" in logs
    assert any("beyond the 100000-byte limit" in message for message in logs)


def test_messages_are_delivered_while_the_hook_runs(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        import time

        def check(ctx):
            ctx.log.info("early")
            time.sleep(2)
            return True
    '''})
    arrivals = []
    call = dataclasses.replace(call_for(watch_dir), on_message=lambda m: arrivals.append((time.monotonic(), m)))
    result = Runner().run(call)
    finished = time.monotonic()
    assert result.status == "true"
    assert [message["message"] for _, message in arrivals] == ["early"]
    assert finished - arrivals[0][0] >= 1.5
    assert result.messages == []


@pytest.mark.posix_only
def test_make_shim_is_safe_across_threads_and_repairs_dangling_links(tmp_path):
    import keepwatch

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "keepwatch").symlink_to(tmp_path / "gone")
    errors = []

    def build():
        try:
            make_shim(lib)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (lib / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent


@pytest.mark.windows_only
def test_make_shim_repairs_a_wrong_junction_across_threads(tmp_path):
    import _winapi

    import keepwatch

    lib = tmp_path / "lib"
    lib.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _winapi.CreateJunction(str(other), str(lib / "keepwatch"))
    errors = []

    def build():
        try:
            make_shim(lib)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (lib / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent
