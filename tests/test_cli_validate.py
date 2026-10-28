import json

import pytest
from click.testing import CliRunner

from portable import TRUE, toml_path
from watchglass.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_valid_watches(xdg, make_watch):
    make_watch("a", files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    result = run("validate")
    assert result.exit_code == 0, result.output
    assert "ok   a" in result.output and "ok   b" in result.output


def test_every_problem_is_reported(xdg, make_watch):
    make_watch("typo", config='intervall = "30s"\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("importerr", files={"watch.py": "import does_not_exist_xyz\n\ndef check(ctx):\n    return True\n"})
    make_watch("nocheck", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    make_watch("badcmd", config='[hooks]\ncheck = ["./missing.sh"]\non_true = ["no-such-program-xyz"]\n')
    result = run("validate", "--json")
    assert result.exit_code == 1
    data = json.loads(result.output)
    by_name = {w["name"]: w for w in data["watches"]}
    assert "unknown key 'intervall'" in by_name["typo"]["problems"][0]
    assert "importing watch.py failed: ModuleNotFoundError" in by_name["importerr"]["problems"][0]
    assert by_name["nocheck"]["problems"][0].endswith(
        "the watch has no check: define check(ctx) in watch.py or set check in [hooks] of config.toml; "
        "see: watchglass docs python"
    )
    badcmd = " ".join(by_name["badcmd"]["problems"])
    assert "./missing.sh does not exist" in badcmd and "'no-such-program-xyz' is not on PATH" in badcmd
    assert data["ok"] is False


def test_names_filter_and_unknown_name(xdg, make_watch):
    make_watch("good", files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("bad", config="interval = 0\n")
    assert run("validate", "good").exit_code == 0
    result = run("validate", "goood")
    assert result.exit_code == 1 and "did you mean 'good'" in result.output


def test_missing_watches_directory_is_a_problem(xdg):
    result = run("validate")
    assert result.exit_code == 1
    assert "watches directory does not exist" in result.output


def test_dates_in_settings_do_not_crash(xdg, make_watch):
    make_watch(
        "dated",
        config="[settings]\nsince = 2024-01-01\n",
        files={"watch.py": "def check(ctx):\n    return ctx.settings['since'] == '2024-01-01'\n"},
    )
    assert run("validate", "dated").exit_code == 0
    result = run("poll", "dated")
    assert result.exit_code == 0, result.output
    assert "check → true" in result.output


def test_hooks_watchglass_cannot_see_are_reported(xdg, make_watch):
    make_watch("hidden", files={
        "helpers.py": "def on_rise(ctx):\n    pass\n",
        "watch.py": "from helpers import on_rise\n\ndef check(ctx):\n    return True\n\nasync def on_true(ctx):\n    pass\n",
    })
    result = run("validate", "--json")
    assert result.exit_code == 1
    (watch,) = json.loads(result.output)["watches"]
    assert "on_rise, on_true" in watch["problems"][0]
    assert "top-level" in watch["problems"][0]


def test_named_duplicate_is_reported(xdg, tmp_path, make_watch):
    other = tmp_path / "other"
    make_watch("backup", config=f"[hooks]\ncheck = {TRUE}\n")
    make_watch("backup", config=f"[hooks]\ncheck = {TRUE}\n", base=other)
    config = tmp_path / "custom.toml"
    config.write_text(f"watch_dirs = [{toml_path(xdg.default_watches_dir)}, {toml_path(other)}]\n")
    result = run("--config", str(config), "validate", "backup")
    assert result.exit_code == 1
    assert "duplicate watch name 'backup'" in result.output


def test_validate_reports_missing_uv(xdg, make_watch, monkeypatch):
    make_watch("dep", config='python_dependencies = ["six"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    monkeypatch.setenv("PATH", "/nonexistent")
    result = run("validate", "dep")
    assert result.exit_code == 1
    assert "python_dependencies needs uv on PATH" in result.output


@pytest.mark.posix_only
def test_a_hook_program_that_is_not_executable_says_how_to_fix_it(xdg, make_watch):
    """The pre-flight for the login service: a hook the service cannot run has to be caught here."""
    watch_dir = make_watch("noexec", config='[hooks]\ncheck = ["./check.sh"]\n')
    (watch_dir / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    (watch_dir / "check.sh").chmod(0o644)
    result = run("validate", "--json")
    assert result.exit_code == 1
    problems = json.loads(result.output)["watches"][0]["problems"]
    assert any("is not executable (run: chmod +x" in problem for problem in problems), problems


def test_a_recipe_watch_without_ssh_on_path_says_so(xdg, make_watch, monkeypatch):
    """A recipe watch whose ssh is invisible to the service is the commonest production failure."""
    make_watch("relay", config='recipe = "pull"\n[settings]\nremote = "u@h"\nremote_dir = "/d"\nlocal_dir = "stage"\n')
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr("watchglass.platform.hot_environment", lambda: {})  # keep the test's PATH authoritative
    result = run("validate", "--json")
    assert result.exit_code == 1
    problems = json.loads(result.output)["watches"][0]["problems"]
    assert any("needs OpenSSH 'ssh' on PATH" in problem for problem in problems), problems


@pytest.mark.posix_only
def test_validate_uses_the_environment_file_path(xdg, make_watch, tmp_path):
    """A hook program on the environment_file's PATH must not be reported missing."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    helper = bindir / "helper"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    env_file = tmp_path / "env"
    env_file.write_text(f"PATH={bindir}\n", encoding="utf-8")
    env_file.chmod(0o600)
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(f'environment_file = "{env_file}"\n', encoding="utf-8")
    make_watch("w", config='[hooks]\ncheck = ["helper"]\n')
    result = run("validate", "w")
    assert result.exit_code == 0, result.output
    assert "ok   w" in result.output


@pytest.mark.posix_only
def test_recipe_validation_uses_the_environment_file_path(xdg, make_watch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    scp = bindir / "scp"
    scp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    scp.chmod(0o755)
    env_file = tmp_path / "env"
    env_file.write_text(f"PATH={bindir}\n", encoding="utf-8")
    env_file.chmod(0o600)
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(f'environment_file = "{env_file}"\n', encoding="utf-8")
    make_watch(
        "relay",
        config='recipe = "pull"\n[settings]\nremote = "u@h"\nremote_dir = "/d"\nlocal_dir = "stage"\n'
        'ssh_command = ["true"]\n',
    )
    result = run("validate", "relay")
    assert result.exit_code == 0, result.output
