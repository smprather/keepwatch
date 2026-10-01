import textwrap

import pytest

from keepwatch import platform
from keepwatch.config import Command, ConfigError, ExitCodes, load_watch_config
from keepwatch.hooks import discover_python_hooks, resolve_hooks


def write(watch_dir, text):
    watch_dir.mkdir(parents=True, exist_ok=True)
    (watch_dir / "config.toml").write_text(textwrap.dedent(text))
    return watch_dir


def problems_of(watch_dir, text):
    with pytest.raises(ConfigError) as info:
        load_watch_config(write(watch_dir, text))
    return info.value.problems


def test_empty_config_gives_defaults(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", ""))
    assert cfg.name == "w"
    assert (cfg.interval, cfg.check_timeout, cfg.action_timeout) == (60.0, 60.0, 60.0)
    assert cfg.max_failures == 5
    assert cfg.retry_after is None
    assert cfg.enabled is True and cfg.initial_condition is False
    assert cfg.exit_codes == ExitCodes()
    assert dict(cfg.hooks) == {} and dict(cfg.settings) == {} and dict(cfg.environment) == {}


def test_full_config(tmp_path):
    cfg = load_watch_config(write(tmp_path / "psg-export", '''
        description = "Send exports"
        interval = "30s"
        action_timeout = "15m"
        retry_after = "1h"
        initial_condition = true
        max_failures = 3
        python_dependencies = ["requests>=2.32"]

        [hooks]
        check = "./check.sh"
        on_true = ["expect", "./send.exp"]

        [check_exit_codes]
        unknown = [6, 7, 28]

        [environment]
        SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"

        [settings]
        pattern = "~/incoming/*.tar.gz"
        nested = { a = 1 }
    '''))
    assert cfg.interval == 30.0 and cfg.action_timeout == 900.0 and cfg.retry_after == 3600.0
    assert cfg.initial_condition is True and cfg.max_failures == 3
    assert cfg.python_dependencies == ("requests>=2.32",)
    assert cfg.hooks["check"] == Command(shell="./check.sh")
    assert cfg.hooks["check"].to_argv() == platform.shell_argv("./check.sh")
    assert cfg.hooks["on_true"] == Command(argv=("expect", "./send.exp"))
    assert cfg.hooks["on_true"].to_argv() == ["expect", "./send.exp"]
    assert cfg.hooks["on_true"].display() == "expect ./send.exp"
    assert cfg.exit_codes == ExitCodes(true=(0,), false=(1,), unknown=(6, 7, 28))
    assert cfg.environment["SSH_AUTH_SOCK"] == "/run/user/1000/ssh-agent.socket"
    assert cfg.settings == {"pattern": "~/incoming/*.tar.gz", "nested": {"a": 1}}
    assert cfg.config_file == tmp_path / "psg-export" / "config.toml"


def test_defaults_apply_and_file_wins(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", 'interval = "10s"\n'), {"interval": 120.0, "max_failures": 9})
    assert cfg.interval == 10.0
    assert cfg.max_failures == 9


def test_typo_reports_line_and_suggestion(tmp_path):
    (problem,) = problems_of(tmp_path / "w", 'description = "x"\nintervall = "30s"\n')
    assert problem.line == 2
    assert problem.message == "unknown key 'intervall' (did you mean 'interval'?)"
    assert str(problem).endswith(
        "config.toml:2: unknown key 'intervall' (did you mean 'interval'?); see: keepwatch docs config"
    )


def test_all_problems_reported_at_once(tmp_path):
    problems = problems_of(tmp_path / "w", 'interval = "soon"\nmax_failures = -1\nenabled = "yes"\n')
    assert [p.line for p in problems] == [1, 2, 3]
    assert 'expected a duration like "30s"' in problems[0].message
    assert problems[1].message == "'max_failures' must be a whole number >= 0, got -1"
    assert problems[2].message == "'enabled' must be true or false, got 'yes'"


def test_interval_minimum(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "interval = 0.5\n")
    assert problem.message == "'interval' must be at least 1s, got 0.5"


def test_invalid_toml_reports_line(tmp_path):
    (problem,) = problems_of(tmp_path / "w", 'interval = "30s"\ncheck_timeout =\n')
    assert problem.line == 2
    assert problem.message.startswith("invalid TOML:")


def test_hooks_table_problems(tmp_path):
    problems = problems_of(tmp_path / "w", '[hooks]\non_ture = "./x"\ncheck = []\n')
    assert (problems[0].line, problems[0].message) == (
        2,
        "unknown key 'on_ture' in [hooks] (did you mean 'on_true'?)",
    )
    assert problems[1].line == 3
    assert problems[1].message.startswith("'check' must be a command")
    assert problems[1].topic == "executables"


def test_exit_code_overlap(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "[check_exit_codes]\nunknown = [1]\n")
    assert problem.line == 1
    assert problem.message == "exit code 1 is listed under both 'false' and 'unknown'"


def test_missing_config_file(tmp_path):
    (tmp_path / "w").mkdir()
    with pytest.raises(ConfigError, match="missing config.toml"):
        load_watch_config(tmp_path / "w")


def test_discover_python_hooks(tmp_path):
    (tmp_path / "watch.py").write_text(
        "def check(ctx):\n    return True\n\n"
        "def helper():\n    pass\n\n"
        "async def on_true(ctx):\n    pass\n\n"
        "class on_fall:\n    pass\n"
    )
    assert discover_python_hooks(tmp_path) == frozenset({"check"})


def test_discover_without_watch_py(tmp_path):
    assert discover_python_hooks(tmp_path) == frozenset()


def test_resolve_hooks_reports_syntax_error_and_conflicts(tmp_path):
    (tmp_path / "watch.py").write_text("def check(ctx):\n    return (\n")
    hooks, problem = resolve_hooks(tmp_path, {"on_true"})
    assert hooks == frozenset({"on_true"})
    assert problem.startswith("watch.py line 2: syntax error")

    (tmp_path / "watch.py").write_text("def check(ctx):\n    return True\n")
    hooks, problem = resolve_hooks(tmp_path, {"check", "on_true"})
    assert hooks == frozenset({"check", "on_true"})
    assert problem == "defined both in watch.py and in [hooks] of config.toml: check (keep one)"

    hooks, problem = resolve_hooks(tmp_path, {"on_true"})
    assert (hooks, problem) == (frozenset({"check", "on_true"}), None)


def test_infinite_timeout_is_rejected(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "check_timeout = inf\n")
    assert problem.line == 1
    assert "must be a finite number" in problem.message


def test_settings_dates_become_iso_strings(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", '''
        [settings]
        since = 2024-01-01
        at = 2024-01-01T10:00:00Z
        clock = 07:30:00
        nested = { when = 2024-02-03 }
    '''))
    assert cfg.settings == {
        "since": "2024-01-01",
        "at": "2024-01-01T10:00:00+00:00",
        "clock": "07:30:00",
        "nested": {"when": "2024-02-03"},
    }


def test_settings_reject_nan(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "[settings]\nx = nan\n")
    assert problem.line == 1
    assert "nan or inf" in problem.message


@pytest.mark.parametrize("key", ["check_timeout", "action_timeout", "retry_after"])
def test_zero_durations_are_rejected(tmp_path, key):
    (problem,) = problems_of(tmp_path / "w", f'{key} = "0s"\n')
    assert problem.message == f"'{key}' must be at least 1s, got '0s'"
