import stat
from pathlib import Path

import pytest

from keepwatch.paths import (
    PathError,
    Paths,
    ensure_private_dir,
    remove_stale_process_dirs,
    resolve_paths,
)


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
    assert paths == Paths(Path("/cfg/keepwatch"), Path("/st/keepwatch"), Path("/run/user/1000/keepwatch"))


def test_resolve_paths_defaults_and_ignores_relative_values():
    paths = resolve_paths({"HOME": "/home/u", "XDG_CONFIG_HOME": "relative", "TMPDIR": "/var/tmp"}, uid=1234)
    assert paths.config_home == Path("/home/u/.config/keepwatch")
    assert paths.state_home == Path("/home/u/.local/state/keepwatch")
    assert paths.runtime == Path("/var/tmp/keepwatch-1234")


def test_runtime_fallback_without_tmpdir():
    assert resolve_paths({"HOME": "/h"}, uid=7).runtime == Path("/tmp/keepwatch-7")


def test_derived_paths():
    paths = Paths(Path("/c"), Path("/s"), Path("/r"))
    assert paths.config_file == Path("/c/config.toml")
    assert paths.default_watches_dir == Path("/c/watches")
    assert paths.log_file == Path("/s/logs/keepwatch.jsonl")
    assert paths.status_file == Path("/s/status.json")
    assert paths.service_lock == Path("/r/service.lock")
    assert paths.watch_data_dir("w") == Path("/s/watches/w/data")
    assert paths.offline_file("w") == Path("/s/watches/w/offline.json")
    assert paths.watch_lock("w") == Path("/r/locks/w.lock")
    assert paths.run_dir(42, "w") == Path("/r/42/w")


def test_ensure_private_dir_creates_mode_700(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_private_dir_rejects_shared_directory(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)
    with pytest.raises(PathError, match="chmod 700"):
        ensure_private_dir(target)


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
