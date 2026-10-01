import json
import os
import threading
import time

from keepwatch.runner import Runner

ACT_SH = '''
    #!/bin/sh
    {
      echo "condition=$KEEPWATCH_CONDITION"
      echo "hook=$KEEPWATCH_HOOK"
      echo "dest=$KEEPWATCH_SETTING_DEST"
      echo "extra=$EXTRA"
      echo "global=$GLOBAL_ONLY"
      echo "payload=$(cat "$KEEPWATCH_PAYLOAD_FILE")"
      echo "settings=$(cat "$KEEPWATCH_SETTINGS_FILE")"
      echo "cwd=$(pwd)"
    } > "$KEEPWATCH_RUN_DIR/seen.txt"
'''


def test_exit_codes_map_to_outcomes(make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        check = "exit $CODE"
        [check_exit_codes]
        unknown = [3]
    ''')
    for code, status in ((0, "true"), (1, "false"), (3, "unknown"), (7, "error")):
        result = Runner().run(call_for(watch_dir, environment={"CODE": str(code)}))
        assert (result.status, result.exit_code, result.kind) == (status, code, "command")
    assert "exit code 7 is not listed in [check_exit_codes]" in result.reason


def test_check_passes_a_payload(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.sh"]\n', files={"check.sh": '''
        #!/bin/sh
        printf '["a.tar.gz", "b.tar.gz"]' > "$KEEPWATCH_PAYLOAD_OUT"
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.payload == ["a.tar.gz", "b.tar.gz"]
    assert result.target == "./check.sh"


def test_invalid_payload_json_is_an_error(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.sh"]\n', files={"check.sh": '''
        #!/bin/sh
        echo "not json" > "$KEEPWATCH_PAYLOAD_OUT"
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON")


def test_action_sees_environment_payload_and_settings(xdg, make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        on_true = ["./act.sh"]
        [environment]
        EXTRA = "watch-level"
        [settings]
        dest = "foo@bar:/in/"
    ''', files={"act.sh": ACT_SH})
    result = Runner().run(call_for(watch_dir, hook="on_true", condition=True, payload=["a.tar.gz"],
                                   environment={"GLOBAL_ONLY": "yes"}))
    assert result.status == "ok"
    run_dir = xdg.run_dir(os.getpid(), "cmd")
    seen = (run_dir / "seen.txt").read_text().splitlines()
    assert seen == [
        "condition=true",
        "hook=on_true",
        "dest=foo@bar:/in/",
        "extra=watch-level",
        "global=yes",
        'payload=["a.tar.gz"]',
        "settings=" + json.dumps({"dest": "foo@bar:/in/"}),
        f"cwd={watch_dir}",
    ]
    assert sorted(p.name for p in run_dir.iterdir()) == ["seen.txt"]


def test_failed_action(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_false = "echo oops >&2; exit 4"\n')
    result = Runner().run(call_for(watch_dir, hook="on_false"))
    assert (result.status, result.exit_code, result.stderr) == ("failed", 4, "oops\n")
    assert result.reason == "exit code 4"


def test_missing_executable(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = ["./nope.sh"]\n')
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason.startswith("cannot start command")


def test_killed_by_signal(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "kill -TERM $$"\n')
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.signal, result.reason) == ("error", 15, "killed by signal SIGTERM")


def test_timeout(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30"\n')
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.0))
    assert result.status == "timeout" and result.duration < 10


def test_background_child_holding_stdout_does_not_hang(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30 & echo started"\n')
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "started\n"
    assert result.duration < 10


def test_binary_output_is_decoded_with_replacement(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./bin.sh"]\n', files={"bin.sh": '''
        #!/bin/sh
        printf '\\377ok'
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "\ufffdok"


def test_output_is_clipped(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "yes x | head -c 10000"\n')
    result = Runner().run(call_for(watch_dir, capture_bytes=1000))
    assert result.stdout_truncated is True
    assert "bytes omitted" in result.stdout
    assert len(result.stdout) < 1200


def test_unwritable_run_dir_is_a_failed_hook(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = "true"\n')
    call = call_for(watch_dir, hook="on_true")
    call.run_dir.parent.parent.mkdir(parents=True, exist_ok=True)
    call.run_dir.parent.write_text("not a directory")
    result = Runner().run(call)
    assert result.status == "failed"
    assert result.reason.startswith("cannot prepare the hook's files in")


def test_terminate_all_stops_running_hooks(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30"\n')
    runner = Runner()
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(call_for(watch_dir))))
    thread.start()
    time.sleep(0.5)
    runner.terminate_all()
    thread.join(10)
    assert results and results[0].status == "error"
    assert results[0].signal == 15
