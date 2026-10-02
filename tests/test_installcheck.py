import os
import sys
from pathlib import Path

from click.testing import CliRunner

from keepwatch import __version__, installcheck
from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_transient_reason(tmp_path):
    cache, temp, home = tmp_path / "cache", tmp_path / "temp", tmp_path / "home"
    for directory in (cache / "archive-v0" / "x", temp / "env", home / ".local" / "share" / "uv" / "tools" / "keepwatch"):
        directory.mkdir(parents=True)
    kwargs = {"cache_dirs": [cache], "temp_dir": temp}
    assert "uv's cache" in installcheck.transient_reason(cache / "archive-v0" / "x", **kwargs)
    assert "temporary directory" in installcheck.transient_reason(temp / "env", **kwargs)
    assert installcheck.transient_reason(home / ".local" / "share" / "uv" / "tools" / "keepwatch", **kwargs) is None


def test_uv_cache_dirs_include_the_variable(tmp_path):
    dirs = installcheck.uv_cache_dirs({"UV_CACHE_DIR": str(tmp_path / "c"), "PATH": "", "HOME": str(tmp_path)})
    assert tmp_path / "c" in dirs


def test_verify_command():
    good = [sys.executable, "-c", f"print('keepwatch, version {__version__}')"]
    assert installcheck.verify_command(good, {**__import__("os").environ}) is None
    wrong = [sys.executable, "-c", "print('keepwatch, version 1999.1.1')"]
    assert "without reporting keepwatch" in installcheck.verify_command(wrong, {**__import__("os").environ})
    store = [sys.executable, "-c", "import sys; print('Python was not found; run without arguments to install from the Microsoft Store'); sys.exit(9009)"]
    problem = installcheck.verify_command(store, {**__import__("os").environ})
    code = 9009 if os.name == "nt" else 9009 & 0xFF  # POSIX exit statuses are 8-bit
    assert f"exited {code}" in problem and "Microsoft Store" in problem
    assert "cannot run" in installcheck.verify_command([str(Path("/no/such/program"))], {})


def test_the_service_command_reports_this_version():
    import os

    assert installcheck.verify_command(installcheck.service_check_argv(), os.environ) is None


def test_install_refuses_a_transient_environment(xdg, monkeypatch):
    monkeypatch.setattr(installcheck, "transient_reason", lambda prefix, **kwargs: "this keepwatch runs from uv's cache (x)")
    result = run("install", "--dry-run")
    assert result.exit_code == 1
    assert "uv tool install keepwatch" in result.output and "--force" in result.output
    assert run("install", "--dry-run", "--force").exit_code == 0


def test_install_refuses_a_broken_service_command(xdg, monkeypatch):
    monkeypatch.setattr(installcheck, "transient_reason", lambda prefix, **kwargs: None)
    monkeypatch.setattr(installcheck, "verify_command", lambda argv, env: "python.exe exited 9009 …")
    result = run("install", "--dry-run", "--force")
    assert result.exit_code == 1
    assert "the login service would run" in result.output and "exited 9009" in result.output
