import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from keepwatch import __version__, platform
from keepwatch.protocol import PROTOCOL_VERSION, decode_lines
from keepwatch.worker import interpret_check_return


def watch(tmp_path, source):
    watch_dir = tmp_path / "w"
    watch_dir.mkdir()
    (watch_dir / "watch.py").write_text(textwrap.dedent(source))
    return watch_dir


def request_for(watch_dir, hook="check", **extra):
    request = {
        "watch": watch_dir.name,
        "hook": hook,
        "watch_dir": str(watch_dir),
        "data_dir": str(watch_dir.parent / "data"),
        "run_dir": str(watch_dir.parent / "run"),
        "poll_id": "p1",
        "condition": False,
        "payload": None,
        "settings": {},
        "deadline": time.time() + 30,
        "capture_bytes": 65536,
        "mode": "call",
    }
    request.update(extra)
    return request


def run_worker(request):
    req_r, req_w = os.pipe()
    res_r, res_w = os.pipe()
    process = platform.start_process(
        [sys.executable, "-m", "keepwatch.worker", *platform.pipe_arguments(req_r, res_w)],
        cwd=None, env=None, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        inherit=(req_r, res_w),
    )
    os.close(req_r)
    os.close(res_w)
    with os.fdopen(req_w, "wb") as handle:
        handle.write(json.dumps(request).encode())
    with os.fdopen(res_r, "rb") as handle:
        data = handle.read()
    out, err = process.popen.communicate(timeout=30)
    process.close()
    messages, bad = decode_lines(data)
    assert bad == []
    return messages, out.decode(), err.decode()


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, {"status": "answer", "answer": True, "payload": None}),
        (False, {"status": "answer", "answer": False, "payload": None}),
        (None, {"status": "unknown", "reason": None, "payload": None}),
        ((True, [Path("/a")]), {"status": "answer", "answer": True, "payload": [str(Path("/a"))]}),
        ((None, {"n": 1}), {"status": "unknown", "reason": None, "payload": {"n": 1}}),
    ],
)
def test_interpret_check_return(value, expected):
    assert interpret_check_return(value) == expected


def test_interpret_check_return_rejects_other_values():
    result = interpret_check_return(["a.tar.gz"])
    assert result["status"] == "error"
    assert "check returned list" in result["reason"]
    assert interpret_check_return((True, {1, 2}))["reason"].startswith("payload is not JSON-serializable")


def test_check_with_payload_and_logs(tmp_path):
    watch_dir = watch(tmp_path, '''
        def check(ctx):
            ctx.log.info("found %d", 2, extra={"files": ["a", "b"]})
            print("to stdout")
            return True, ["a", "b"]
    ''')
    messages, out, _ = run_worker(request_for(watch_dir))
    assert messages[0] == {"type": "hello", "version": __version__, "protocol": PROTOCOL_VERSION}
    log = messages[1]
    assert (log["type"], log["level"], log["message"], log["fields"]) == ("log", "INFO", "found 2", {"files": ["a", "b"]})
    assert messages[-1] == {"type": "result", "status": "answer", "answer": True, "payload": ["a", "b"]}
    assert out.splitlines() == ["to stdout"]


def test_unknown_with_reason(tmp_path):
    watch_dir = watch(tmp_path, '''
        import keepwatch
        def check(ctx):
            raise keepwatch.Unknown("VPN is down")
    ''')
    messages, _, _ = run_worker(request_for(watch_dir))
    assert messages[-1] == {"type": "result", "status": "unknown", "reason": "VPN is down", "payload": None}


def test_exception_in_action_is_an_error_with_traceback(tmp_path):
    watch_dir = watch(tmp_path, '''
        def on_true(ctx):
            1 / 0
    ''')
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_true", payload=["x"]))
    result = messages[-1]
    assert result["status"] == "error"
    assert result["reason"] == "ZeroDivisionError: division by zero"
    assert "ZeroDivisionError" in result["exception"]["traceback"]


def test_action_success_and_cwd(tmp_path):
    watch_dir = watch(tmp_path, '''
        import os
        def on_true(ctx):
            (ctx.run_dir / "seen").write_text(f"{os.getcwd()}|{ctx.payload}|{ctx.condition}")
    ''')
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_true", payload=["x"], condition=True))
    assert messages[-1] == {"type": "result", "status": "ok"}
    assert (tmp_path / "run" / "seen").read_text() == f"{watch_dir}|['x']|True"


def test_import_failure_is_an_error(tmp_path):
    watch_dir = watch(tmp_path, "import does_not_exist_xyz\n")
    messages, _, _ = run_worker(request_for(watch_dir))
    assert messages[-1]["status"] == "error"
    assert messages[-1]["reason"].startswith("importing watch.py failed: ModuleNotFoundError")


def test_missing_hook_function(tmp_path):
    watch_dir = watch(tmp_path, "def check(ctx):\n    return True\n")
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_fall"))
    assert messages[-1] == {"type": "result", "status": "error", "reason": "watch.py has no function on_fall()"}


def test_describe_lists_hooks(tmp_path):
    watch_dir = watch(tmp_path, "def check(ctx):\n    pass\n\ndef on_true(ctx):\n    pass\n\ndef helper():\n    pass\n")
    messages, _, _ = run_worker(request_for(watch_dir, mode="describe"))
    assert messages[-1] == {"type": "result", "status": "ok", "hooks": ["check", "on_true"]}
