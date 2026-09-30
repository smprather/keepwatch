import os
import textwrap
from pathlib import Path

import pytest

from keepwatch.config import (
    Command,
    ConfigError,
    GlobalConfig,
    LogSettings,
    WatchNotFound,
    discover_watches,
    find_watch,
    load_global_config,
    load_watch_config,
)


def write_global(xdg, text):
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(textwrap.dedent(text))


def test_missing_global_config_uses_defaults(xdg):
    cfg = load_global_config(xdg.config_file, xdg)
    assert cfg.watch_dirs == (xdg.default_watches_dir,)
    assert cfg.reload_interval == 5.0
    assert cfg.alert_command is None
    assert cfg.log == LogSettings()
    assert dict(cfg.defaults) == {} and dict(cfg.environment) == {}


def test_full_global_config(xdg, tmp_path, monkeypatch):
    monkeypatch.setenv("KW_TEST_DIR", str(tmp_path / "x"))
    write_global(xdg, '''
        watch_dirs = ["~/w1", "more", "$KW_TEST_DIR/w3"]
        reload_interval = "10s"
        alert_command = ["notify-send", "keepwatch"]

        [defaults]
        interval = "2m"
        max_failures = 8

        [environment]
        SSH_AUTH_SOCK = "/tmp/agent"

        [log]
        max_bytes = 5000
        backups = 2
        capture_bytes = 1024
    ''')
    cfg = load_global_config(xdg.config_file, xdg)
    home = Path(os.environ["HOME"])
    assert cfg.watch_dirs == (home / "w1", xdg.config_home / "more", tmp_path / "x" / "w3")
    assert cfg.reload_interval == 10.0
    assert cfg.alert_command == Command(argv=("notify-send", "keepwatch"))
    assert dict(cfg.defaults) == {"interval": 120.0, "max_failures": 8}
    assert dict(cfg.environment) == {"SSH_AUTH_SOCK": "/tmp/agent"}
    assert cfg.log == LogSettings(max_bytes=5000, backups=2, capture_bytes=1024)


def test_defaults_rejects_non_defaultable_key(xdg):
    write_global(xdg, '[defaults]\ndescription = "x"\n')
    with pytest.raises(ConfigError) as info:
        load_global_config(xdg.config_file, xdg)
    (problem,) = info.value.problems
    assert problem.line == 2
    assert problem.message.startswith("'description' cannot be set in [defaults]")


def test_global_unknown_key_suggests(xdg):
    write_global(xdg, "watch_dir = []\n")
    with pytest.raises(ConfigError, match="did you mean 'watch_dirs'"):
        load_global_config(xdg.config_file, xdg)


def test_watch_dirs_must_be_a_nonempty_list(xdg):
    write_global(xdg, "watch_dirs = []\n")
    with pytest.raises(ConfigError, match="'watch_dirs' must be a non-empty list"):
        load_global_config(xdg.config_file, xdg)


def test_log_values_must_be_positive(xdg):
    write_global(xdg, "[log]\nmax_bytes = 0\n")
    with pytest.raises(ConfigError, match="'max_bytes' must be at least 1"):
        load_global_config(xdg.config_file, xdg)


def test_defaults_flow_into_watch_config(xdg, make_watch):
    write_global(xdg, '[defaults]\ninterval = "2m"\n')
    watch_dir = make_watch("w")
    cfg = load_global_config(xdg.config_file, xdg)
    assert load_watch_config(watch_dir, cfg.defaults).interval == 120.0


def test_discover_watches(xdg, tmp_path, make_watch):
    second = tmp_path / "second"
    make_watch("a")
    make_watch("dup")
    make_watch("dup", base=second)
    make_watch("b", base=second)
    (xdg.default_watches_dir / ".git").mkdir()
    (xdg.default_watches_dir / "_parked").mkdir()
    (xdg.default_watches_dir / "notes.txt").write_text("")
    (xdg.default_watches_dir / "bad name").mkdir()
    cfg = GlobalConfig(path=xdg.config_file, watch_dirs=(xdg.default_watches_dir, second, tmp_path / "missing"))
    found = discover_watches(cfg)
    assert list(found.watches) == ["a", "dup", "b"]
    assert found.watches["dup"] == xdg.default_watches_dir / "dup"
    messages = [problem.message for problem in found.problems]
    assert any(message.startswith("duplicate watch name 'dup'") for message in messages)
    assert any("does not exist" in message for message in messages)
    assert any("may contain only" in message for message in messages)
    assert len(messages) == 3


def test_find_watch_suggests_a_close_name(xdg, make_watch):
    make_watch("psg-export")
    found = discover_watches(load_global_config(xdg.config_file, xdg))
    assert find_watch(found, "psg-export") == xdg.default_watches_dir / "psg-export"
    with pytest.raises(WatchNotFound, match="did you mean 'psg-export'"):
        find_watch(found, "psg-exprot")
