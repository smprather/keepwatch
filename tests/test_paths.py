import os
import stat
from contextlib import nullcontext
from pathlib import Path

import pytest

from watchglass.paths import (
    PathError,
    Paths,
    ensure_private_dir,
    remove_stale_process_dirs,
    resolve_paths,
)


@pytest.mark.posix_only  # POSIX-style absolute paths; Windows needs a drive letter
def test_resolve_paths_uses_xdg_variables():
    paths = resolve_paths(
        {
            "HOME": "/home/u",
            "XDG_CONFIG_HOME": "/cfg",
            "XDG_STATE_HOME": "/st",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        uid=1000,
    )
    assert paths == Paths(Path("/cfg/watchglass"), Path("/st/watchglass"), Path("/run/user/1000/watchglass"))


@pytest.mark.posix_only
def test_resolve_paths_defaults_and_ignores_relative_values():
    paths = resolve_paths({"HOME": "/home/u", "XDG_CONFIG_HOME": "relative", "TMPDIR": "/var/tmp"}, uid=1234)
    assert paths.config_home == Path("/home/u/.config/watchglass")
    assert paths.state_home == Path("/home/u/.local/state/watchglass")
    assert paths.runtime == Path("/var/tmp/watchglass-1234")


@pytest.mark.posix_only
def test_runtime_fallback_without_tmpdir():
    assert resolve_paths({"HOME": "/h"}, uid=7).runtime == Path("/tmp/watchglass-7")


def test_derived_paths():
    paths = Paths(Path("/c"), Path("/s"), Path("/r"))
    assert paths.config_file == Path("/c/config.toml")
    assert paths.default_watches_dir == Path("/c/watches")
    assert paths.log_file == Path("/s/logs/watchglass.jsonl")
    assert paths.status_file == Path("/s/status.json")
    assert paths.service_lock == Path("/r/service.lock")
    assert paths.watch_data_dir("w") == Path("/s/watches/w/data")
    assert paths.offline_file("w") == Path("/s/watches/w/offline.json")
    assert paths.watch_lock("w") == Path("/r/locks/w.lock")
    assert paths.run_dir(42, "w") == Path("/r/42/w")


@pytest.mark.posix_only
def test_ensure_private_dir_creates_mode_700(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@pytest.mark.posix_only
def test_ensure_private_dir_rejects_shared_directory(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)
    with pytest.raises(PathError, match="chmod 700"):
        ensure_private_dir(target)


@pytest.mark.posix_only
def test_ensure_private_dir_rejects_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(PathError, match="not a directory"):
        ensure_private_dir(link)


def test_remove_stale_process_dirs(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "r")
    for name in ("111", "222", "notes"):
        (paths.runtime / name).mkdir(parents=True)
    removed = remove_stale_process_dirs(paths, alive=lambda pid: pid == 111)
    assert removed == [paths.runtime / "222"]
    assert sorted(p.name for p in paths.runtime.iterdir()) == ["111", "notes"]


@pytest.mark.windows_only
def test_xdg_variables_override_windows_defaults(tmp_path):
    paths = resolve_paths({"XDG_CONFIG_HOME": str(tmp_path / "c"), "XDG_STATE_HOME": str(tmp_path / "s"),
                           "XDG_RUNTIME_DIR": str(tmp_path / "r"), "APPDATA": r"C:\x", "LOCALAPPDATA": r"C:\y"})
    assert paths == Paths(tmp_path / "c" / "watchglass", tmp_path / "s" / "watchglass", tmp_path / "r" / "watchglass")


# --- the keepwatch -> watchglass rename ---------------------------------------


def legacy_env(tmp_path):
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }


def test_resolve_paths_moves_the_old_config_and_state_once(tmp_path, capsys):
    env = legacy_env(tmp_path)
    old_config = tmp_path / "config" / "keepwatch"
    old_state = tmp_path / "state" / "keepwatch"
    (old_config / "watches" / "a").mkdir(parents=True)
    (old_config / "config.toml").write_text("reload_interval = '1m'\n", encoding="utf-8")
    (old_state / "logs").mkdir(parents=True)
    (old_state / "logs" / "keepwatch.jsonl").write_text("{}\n", encoding="utf-8")
    (old_state / "logs" / "keepwatch.jsonl.1").write_text("{}\n", encoding="utf-8")

    paths = resolve_paths(env, uid=1000)

    assert (paths.config_home / "config.toml").read_text(encoding="utf-8") == "reload_interval = '1m'\n"
    assert (paths.state_home / "logs" / "watchglass.jsonl").exists()
    assert (paths.state_home / "logs" / "watchglass.jsonl.1").exists()  # history stays readable
    assert not old_config.exists() and not old_state.exists()
    assert "moved" in capsys.readouterr().err
    assert paths.runtime == tmp_path / "run" / "watchglass"  # run-only data is never moved


def test_resolve_paths_does_not_overwrite_a_new_directory(tmp_path):
    env = legacy_env(tmp_path)
    old_config = tmp_path / "config" / "keepwatch"
    new_config = tmp_path / "config" / "watchglass"
    old_config.mkdir(parents=True)
    new_config.mkdir(parents=True)
    (old_config / "old.toml").write_text("old", encoding="utf-8")
    (new_config / "new.toml").write_text("new", encoding="utf-8")

    paths = resolve_paths(env, uid=1000)

    assert (paths.config_home / "new.toml").read_text(encoding="utf-8") == "new"
    assert (old_config / "old.toml").exists()  # left for the user to reconcile


def test_resolve_paths_refuses_to_move_under_a_running_old_service(tmp_path):
    from watchglass.locks import hold_lock

    env = legacy_env(tmp_path)
    old_config = tmp_path / "config" / "keepwatch"
    old_config.mkdir(parents=True)
    old_lock = tmp_path / "run" / "keepwatch" / "service.lock"
    with hold_lock(old_lock):
        with pytest.raises(PathError, match="old keepwatch service is running"):
            resolve_paths(env, uid=1000)
    assert old_config.exists()


def test_resolve_paths_detects_a_running_old_service_from_status(tmp_path):
    """The old runtime path can differ (cron, a stripped environment); status.json still names a live pid."""
    env = legacy_env(tmp_path)
    old_state = tmp_path / "state" / "keepwatch"
    old_state.mkdir(parents=True)
    (old_state / "status.json").write_text(
        '{"service": {"running": true, "pid": %d}}' % os.getpid(), encoding="utf-8"
    )
    with pytest.raises(PathError, match="old keepwatch service is running"):
        resolve_paths(env, uid=1000)


def test_resolve_paths_leaves_both_trees_alone_even_with_a_running_old_service(tmp_path):
    """Nothing would move, so a leftover old service must not block every watchglass command."""
    from watchglass.locks import hold_lock

    env = legacy_env(tmp_path)
    (tmp_path / "config" / "keepwatch").mkdir(parents=True)
    (tmp_path / "config" / "watchglass").mkdir(parents=True)
    (tmp_path / "state" / "keepwatch").mkdir(parents=True)
    (tmp_path / "state" / "watchglass").mkdir(parents=True)
    with hold_lock(tmp_path / "run" / "keepwatch" / "service.lock"):
        paths = resolve_paths(env, uid=1000)
    assert paths.config_home.exists() and (tmp_path / "config" / "keepwatch").exists()


def test_windows_migration_does_not_carry_the_run_directory(tmp_path, monkeypatch):
    """On Windows the runtime lives inside the state tree; it must be discarded, not moved."""
    from watchglass import paths as paths_module
    from watchglass import platform as platform_module

    monkeypatch.setattr(platform_module, "IS_WINDOWS", True)
    monkeypatch.setattr(paths_module, "probe_lock", lambda _path: nullcontext(False))  # POSIX locks in this test
    monkeypatch.setattr(paths_module, "pid_alive", lambda _pid: False)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }
    old_config = tmp_path / "roaming" / "keepwatch"
    old_state = tmp_path / "local" / "keepwatch"
    old_config.mkdir(parents=True)
    (old_state / "logs").mkdir(parents=True)
    (old_state / "logs" / "keepwatch.jsonl").write_text("{}\n", encoding="utf-8")
    (old_state / "run" / "locks").mkdir(parents=True)
    (old_state / "run" / "service.lock").write_text("", encoding="utf-8")
    (old_state / "run" / "12345" / "w").mkdir(parents=True)

    paths = resolve_paths(env)

    assert paths.config_home == tmp_path / "roaming" / "watchglass"
    assert paths.state_home == tmp_path / "local" / "watchglass"
    assert paths.runtime == tmp_path / "local" / "watchglass" / "run"
    assert not paths.runtime.exists()  # old locks and process dirs are gone, not carried over
    assert (paths.state_home / "logs" / "watchglass.jsonl").exists()


def test_resolve_paths_detects_a_live_manual_poll(tmp_path):
    """A manual keepwatch poll holds no service lock; its live run directory still means do not move."""
    env = legacy_env(tmp_path)
    (tmp_path / "config" / "keepwatch").mkdir(parents=True)
    (tmp_path / "run" / "keepwatch" / str(os.getpid()) / "w").mkdir(parents=True)
    with pytest.raises(PathError, match="old keepwatch service is running"):
        resolve_paths(env, uid=1000)
