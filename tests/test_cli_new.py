import json
import os
import stat

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_python_template_validates_and_polls(xdg):
    result = run("new", "fresh")
    assert result.exit_code == 0, result.output
    watch_dir = xdg.default_watches_dir / "fresh"
    assert sorted(p.name for p in watch_dir.iterdir()) == ["config.toml", "watch.py"]
    assert "keepwatch validate fresh" in result.output
    assert run("validate", "fresh").exit_code == 0
    data = json.loads(run("poll", "fresh", "--json").output)
    assert data["polls"][0]["outcome"] == "false"


@pytest.mark.posix_only
def test_shell_template_validates_and_scripts_are_executable(xdg):
    assert run("new", "sh-watch", "--template", "shell").exit_code == 0
    watch_dir = xdg.default_watches_dir / "sh-watch"
    for script in ("check.sh", "on_true.sh"):
        assert os.stat(watch_dir / script).st_mode & stat.S_IXUSR
    assert run("validate", "sh-watch").exit_code == 0
    data = json.loads(run("poll", "sh-watch", "--fake", "true", "--json").output)
    assert data["polls"][0]["results"] == [{"hook": "on_true", "status": "ok", "reason": None}]


def test_expect_template_files(xdg):
    assert run("new", "exp", "--template", "expect").exit_code == 0
    watch_dir = xdg.default_watches_dir / "exp"
    assert sorted(p.name for p in watch_dir.iterdir()) == ["check.sh", "config.toml", "on_true.exp"]
    assert (watch_dir / "on_true.exp").read_text().startswith("#!/usr/bin/env expect")


def test_new_into_another_directory(xdg, tmp_path):
    target = tmp_path / "elsewhere"
    assert run("new", "x", "--dir", str(target)).exit_code == 0
    assert (target / "x" / "watch.py").exists()


def test_new_refuses_existing_and_invalid_names(xdg):
    assert run("new", "dup").exit_code == 0
    (xdg.default_watches_dir / "dup" / "watch.py").write_text("# mine\n")
    result = run("new", "dup")
    assert result.exit_code == 1 and "already exists" in result.output
    assert (xdg.default_watches_dir / "dup" / "watch.py").read_text() == "# mine\n"
    result = run("new", "bad name")
    assert result.exit_code == 1 and "not a valid watch name" in result.output
