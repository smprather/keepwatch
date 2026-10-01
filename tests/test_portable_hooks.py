import sys
from pathlib import Path

import pytest

from keepwatch.runner import Runner

UNKNOWN_3 = "[check_exit_codes]\nunknown = [3]\n"


def test_string_hooks_use_the_platform_shell(make_watch, call_for):
    watch_dir = make_watch("s", config='[hooks]\ncheck = "exit 3"\n' + UNKNOWN_3)
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_shell_key_overrides_the_platform_shell(make_watch, call_for):
    exe = Path(sys.executable).as_posix()
    watch_dir = make_watch(
        "s", config=f"shell = ['{exe}', '-c']\n[hooks]\ncheck = 'import sys; sys.exit(3)'\n" + UNKNOWN_3
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_relative_python_program_runs_from_the_watch_dir(make_watch, call_for):
    watch_dir = make_watch(
        "s",
        config='[hooks]\ncheck = ["./helper.py", "3"]\n' + UNKNOWN_3,
        files={"helper.py": f"#!{sys.executable}\nimport sys\nsys.exit(int(sys.argv[1]))\n"},
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_shell_must_be_a_non_empty_list(tmp_path):
    from keepwatch.config import ConfigError, load_watch_config

    watch_dir = tmp_path / "w"
    watch_dir.mkdir()
    (watch_dir / "config.toml").write_text("shell = []\n")
    with pytest.raises(ConfigError, match="'shell' must be a non-empty list of strings"):
        load_watch_config(watch_dir)


@pytest.mark.windows_only
def test_powershell_and_cmd_scripts(make_watch, call_for):
    watch_dir = make_watch(
        "s",
        config='[hooks]\ncheck = ["./check.ps1"]\non_true = ["./act.cmd"]\n' + UNKNOWN_3,
        files={"check.ps1": "exit 3\n", "act.cmd": "@echo off\r\nexit /b 0\r\n"},
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"
    assert Runner().run(call_for(watch_dir, hook="on_true")).status == "ok"


@pytest.mark.windows_only
def test_string_hook_output_is_utf8_on_windows(make_watch, call_for):
    watch_dir = make_watch("s", config="[hooks]\ncheck = \"Write-Output 'café'\"\n")
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout.strip() == "café"


def test_python_hook_ctx_run_uses_the_watch_shell(make_watch, call_for):
    exe = Path(sys.executable).as_posix()
    watch_dir = make_watch("s", config=f"shell = ['{exe}', '-c']\n", files={"watch.py": '''
        def check(ctx):
            return ctx.run("import sys; sys.exit(6)", check=False).returncode == 6
    '''})
    assert Runner().run(call_for(watch_dir)).status == "true"
