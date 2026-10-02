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
    assert "[observe.x] needs kind = one of command, files, remote_files; got missing" in problem
    assert "config.toml:1:" in problem
    assert "keepwatch docs observers" in problem


def test_unknown_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'remote'\n")
    assert "got 'remote'" in problem and "config.toml:2:" in problem


def test_key_of_the_other_kind(make_watch):
    [problem] = problems(make_watch, "[observe.x]\nkind = 'command'\ncommand = ['a']\npattern = '*.gz'\n")
    assert "'pattern' only applies to kind = \"files\" or \"remote_files\"; this observer is kind = \"command\"" in problem
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


REMOTE = "[observe.r]\nkind = 'remote_files'\nremote = 'me@linux1'\ndir = '/data/out'\n"


def test_remote_files_observer(make_watch):
    config = load(
        make_watch,
        REMOTE + "pattern = '*.tar.gz'\nport = 2222\nidentity = '~/.ssh/id_relay'\n"
        "ssh_options = ['-o', 'ProxyJump=bastion']\n",
    )
    observer = config.observers["r"]
    assert (observer.kind, observer.remote, observer.dir, observer.pattern, observer.port) == (
        "remote_files",
        "me@linux1",
        "/data/out",
        "*.tar.gz",
        2222,
    )
    assert observer.identity == Path.home() / ".ssh" / "id_relay"
    assert observer.ssh_options == ("-o", "ProxyJump=bastion")
    assert (observer.interval, observer.checksum, observer.heartbeat) == (2.0, True, 30.0)
    assert (observer.remote_python, observer.ssh_command, observer.heartbeat_timeout) == ("auto", None, None)
    assert (observer.settle, observer.ignore) == (10.0, DEFAULT_IGNORE)


def test_remote_dir_is_not_expanded_locally(make_watch):
    config = load(make_watch, "[observe.r]\nkind = 'remote_files'\nremote = 'h'\ndir = '~/out'\n")
    assert config.observers["r"].dir == "~/out"


def test_remote_files_needs_remote_and_dir(make_watch):
    found = problems(make_watch, "[observe.r]\nkind = 'remote_files'\n")
    assert len(found) == 2
    assert any("needs 'remote'" in problem for problem in found)
    assert any("needs 'dir'" in problem for problem in found)


def test_remote_files_rejects_local_keys(make_watch):
    [problem] = problems(make_watch, REMOTE + "path = 'x'\n")
    assert "'path' only applies to kind = \"files\"; this observer is kind = \"remote_files\"" in problem


def test_remote_files_port_range(make_watch):
    [problem] = problems(make_watch, REMOTE + "port = 70000\n")
    assert "'port' must be between 1 and 65535, got 70000" in problem


def test_remote_files_empty_values(make_watch):
    found = problems(make_watch, "[observe.r]\nkind = 'remote_files'\nremote = ''\ndir = ''\nremote_python = ''\n")
    assert len(found) == 3 and all("must not be empty" in problem for problem in found)


def test_remote_files_ssh_command(make_watch):
    config = load(make_watch, REMOTE + f"ssh_command = [{literal(PY)}, 'fake_ssh.py']\nheartbeat_timeout = '5m'\n")
    observer = config.observers["r"]
    assert observer.ssh_command == (PY, "fake_ssh.py") and observer.heartbeat_timeout == 300.0


def test_remote_heartbeat_timeout_must_exceed_heartbeat(make_watch):
    [problem] = problems(make_watch, REMOTE + "heartbeat = '60s'\nheartbeat_timeout = '30s'\n")
    assert "'heartbeat_timeout' (30s) must be longer than 'heartbeat' (1m)" in problem


def test_remote_heartbeat_timeout_is_checked_against_the_default_heartbeat(make_watch):
    [problem] = problems(make_watch, REMOTE + "heartbeat_timeout = '20s'\n")
    assert "must be longer than 'heartbeat' (30s)" in problem
