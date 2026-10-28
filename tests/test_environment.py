"""The hot environment: Windows registry values and the Linux environment_file (no restart to rotate a password)."""


import pytest

from watchglass import platform
from watchglass.config import EnvironmentFileError, load_environment_file, load_watch_config
from watchglass.runner import base_environment


def write_env(tmp_path, text, mode=0o600):
    path = tmp_path / "env"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_environment_file_is_read_fresh_for_every_hook(tmp_path, make_watch):
    """The file is the hot source: a rewrite is visible to the next hook, with no restart."""
    path = write_env(tmp_path, "RELAY_PASSWORD=old\n")
    watch = load_watch_config(make_watch("w"))
    assert base_environment({}, watch, path)["RELAY_PASSWORD"] == "old"
    path.write_text("RELAY_PASSWORD=new\n", encoding="utf-8")
    assert base_environment({}, watch, path)["RELAY_PASSWORD"] == "new"


def test_environment_file_syntax_and_quotes(tmp_path):
    path = write_env(
        tmp_path,
        "# a comment\n"
        "export RELAY_PASSWORD=\"p a s s#word\"\n"
        "PLAIN=value # inline comment\n"
        "HASH=value#kept\n"
        "EMPTY=\n"
        "SPACES = around \n",
    )
    values = load_environment_file(path)
    assert values == {
        "RELAY_PASSWORD": "p a s s#word",
        "PLAIN": "value",
        "HASH": "value#kept",
        "EMPTY": "",
        "SPACES": "around",
    }


def test_environment_file_precedence(tmp_path, make_watch, monkeypatch):
    monkeypatch.setenv("RELAY_PASSWORD", "process")
    path = write_env(tmp_path, "RELAY_PASSWORD=file\n")
    watch = load_watch_config(make_watch("w", config='[environment]\nRELAY_PASSWORD = "watch"\n'))
    assert base_environment({"RELAY_PASSWORD": "global"}, watch, path)["RELAY_PASSWORD"] == "watch"
    plain = load_watch_config(make_watch("plain"))
    assert base_environment({"RELAY_PASSWORD": "global"}, plain, path)["RELAY_PASSWORD"] == "global"
    assert base_environment({}, plain, path)["RELAY_PASSWORD"] == "file"
    assert base_environment({}, plain)["RELAY_PASSWORD"] == "process"


def test_windows_registry_values_win_case_insensitively(tmp_path, make_watch, monkeypatch):
    monkeypatch.setattr(platform, "hot_environment", lambda: {"Path": "C:/new", "RELAY_PASSWORD": "hot"})
    monkeypatch.setenv("PATH", "/old")
    monkeypatch.setenv("RELAY_PASSWORD", "old")
    watch = load_watch_config(make_watch("w"))
    env = base_environment({}, watch)
    assert env["Path"] == "/old;C:/new" and "PATH" not in env  # process-only entries survive
    assert env["RELAY_PASSWORD"] == "hot"


def test_environment_file_beats_the_registry(tmp_path, make_watch, monkeypatch):
    monkeypatch.setattr(platform, "hot_environment", lambda: {"RELAY_PASSWORD": "registry"})
    path = write_env(tmp_path, "RELAY_PASSWORD=file\n")
    watch = load_watch_config(make_watch("w"))
    assert base_environment({}, watch, path)["RELAY_PASSWORD"] == "file"


def test_watchglass_variables_do_not_leak_from_the_hot_sources(tmp_path, make_watch, monkeypatch):
    monkeypatch.setattr(platform, "hot_environment", lambda: {"WATCHGLASS_LEAK": "registry"})
    path = write_env(tmp_path, "WATCHGLASS_LEAK=file\n")
    watch = load_watch_config(make_watch("w"))
    env = base_environment({}, watch, path)
    assert "WATCHGLASS_LEAK" not in env


@pytest.mark.posix_only
def test_environment_file_must_be_private(tmp_path):
    path = write_env(tmp_path, "RELAY_PASSWORD=secret\n", mode=0o644)
    with pytest.raises(EnvironmentFileError, match="accessible by other users"):
        load_environment_file(path)


def test_a_malformed_environment_file_names_the_line(tmp_path):
    path = write_env(tmp_path, "RELAY_PASSWORD=ok\nnot a line\n")
    with pytest.raises(EnvironmentFileError, match=r":2:"):
        load_environment_file(path)


def test_a_missing_environment_file_is_a_clear_error(tmp_path):
    with pytest.raises(EnvironmentFileError, match="cannot read"):
        load_environment_file(tmp_path / "missing")
