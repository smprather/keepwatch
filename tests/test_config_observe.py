from pathlib import Path

import pytest

from keepwatch.config import DEFAULT_IGNORE, Command, ConfigError, ObserverConfig, load_watch_config
from portable import PY, literal


def load(make_watch, text):
    return load_watch_config(make_watch("w", config=text))


def problems(make_watch, text):
    with pytest.raises(ConfigError) as info:
        load(make_watch, text)
    return [str(problem) for problem in info.value.problems]


def test_no_observers_by_default(make_watch):
    assert load(make_watch, "").observers == {}


def test_command_observer(make_watch):
    config = load(
        make_watch,
        f"[observe.feed]\nkind = 'command'\ncommand = [{literal(PY)}, 'feed.py']\n"
        "stdin = 'hello'\nheartbeat_timeout = '90s'\nwake = false\n",
    )
    assert config.observers["feed"] == ObserverConfig(
        name="feed",
        kind="command",
        wake=False,
        command=Command(argv=(PY, "feed.py")),
        stdin="hello",
        heartbeat_timeout=90.0,
    )


def test_command_observer_may_be_a_string(make_watch):
    config = load(make_watch, "[observe.feed]\nkind = 'command'\ncommand = 'tail -F x.log'\n")
    assert config.observers["feed"].command == Command(shell="tail -F x.log")


def test_files_observer_defaults_and_relative_path(make_watch):
    config = load(make_watch, "[observe.inbox]\nkind = 'files'\npath = 'inbox'\n")
    observer = config.observers["inbox"]
    assert observer.path == config.watch_dir / "inbox"
    assert (observer.pattern, observer.recursive, observer.settle, observer.wake) == ("*", False, 10.0, True)
    assert observer.ignore == DEFAULT_IGNORE == (".*", "*.tmp", "*.part", "*~")


def test_files_path_expands_home(make_watch):
    config = load(make_watch, "[observe.inbox]\nkind = 'files'\npath = '~/drop'\n")
    assert config.observers["inbox"].path == Path.home() / "drop"


def test_files_observer_keys(make_watch):
    config = load(
        make_watch,
        "[observe.inbox]\nkind = 'files'\npath = 'in'\npattern = '*.gz'\nignore = []\nrecursive = true\nsettle = '0s'\n",
    )
    observer = config.observers["inbox"]
    assert (observer.pattern, observer.ignore, observer.recursive, observer.settle) == ("*.gz", (), True, 0.0)


def test_missing_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\npath = 'in'\n")
    assert "[observe.x] needs kind = one of command, files; got missing" in problem
    assert "config.toml:1:" in problem
    assert "keepwatch docs observers" in problem


def test_unknown_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'remote'\n")
    assert "got 'remote'" in problem and "config.toml:2:" in problem


def test_key_of_the_other_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\ncommand = ['a']\npattern = '*.gz'\n")
    assert "'pattern' only applies to kind = \"files\"; this observer is kind = \"command\"" in problem
    assert "config.toml:4:" in problem


def test_unknown_key_suggests(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'files'\npath = 'in'\nsetle = '5s'\n")
    assert "unknown key 'setle' in [observe.x] (did you mean 'settle'?)" in problem


def test_required_key(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\n")
    assert "[observe.x] (kind = \"command\") needs 'command'" in problem


def test_bad_values_are_all_reported(make_watch):
    found = problems(make_watch, "[observe.x]\nkind = 'files'\npath = ''\nsettle = 'soon'\nrecursive = 'yes'\n")
    assert len(found) == 3
    assert any("'path' must not be empty" in problem for problem in found)


def test_heartbeat_timeout_minimum(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\ncommand = ['a']\nheartbeat_timeout = '0s'\n")
    assert "at least 1s" in problem


def test_bad_observer_name(make_watch):
    [problem] = problems(make_watch, "[observe.'a b']\nkind = 'files'\npath = 'in'\n")
    assert "observer name 'a b' may contain only letters, digits, '_' and '-'" in problem


def test_observe_must_be_a_table(make_watch):
    [problem] = problems(make_watch, "observe = 3\n")
    assert "'observe' must be a table of observers" in problem


def test_each_observer_must_be_a_table(make_watch):
    [problem] = problems(make_watch, "[observe]\nx = 3\n")
    assert "'observe.x' must be a table ([observe.x])" in problem
