import json

from click.testing import CliRunner

from keepwatch.cli import cli


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
        "see: keepwatch docs python"
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


def test_hooks_keepwatch_cannot_see_are_reported(xdg, make_watch):
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
    make_watch("backup", config='[hooks]\ncheck = ["true"]\n')
    make_watch("backup", config='[hooks]\ncheck = ["true"]\n', base=other)
    config = tmp_path / "custom.toml"
    config.write_text(f'watch_dirs = ["{xdg.default_watches_dir}", "{other}"]\n')
    result = run("--config", str(config), "validate", "backup")
    assert result.exit_code == 1
    assert "duplicate watch name 'backup'" in result.output
