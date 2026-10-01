import json

from click.testing import CliRunner

from keepwatch.cli import cli
from portable import toml_path

WATCH_PY = '''
    def check(ctx):
        return True, ["a"]

    def on_true(ctx):
        (ctx.watch_dir / "ran").write_text(repr(ctx.payload))
'''


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_help_is_plain_when_piped(xdg):
    for args in (["--help"], ["poll", "--help"]):
        result = run(*args)
        assert result.exit_code == 0
        assert not any(ch in result.output for ch in "╭╮╰╯│─")
        assert "\x1b[" not in result.output
    assert "Develop" in run("--help").output


def test_poll_runs_check_and_action(xdg, make_watch):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w")
    assert result.exit_code == 0, result.output
    assert "outcome true; condition FALSE → TRUE; actions: on_true" in result.output
    assert (watch_dir / "ran").read_text() == "['a']"
    events = [json.loads(line)["event"] for line in xdg.log_file.read_text().splitlines()]
    assert events[0] == "poll.start" and events[-1] == "poll.end"
    assert not xdg.process_dir(__import__("os").getpid()).exists()


def test_poll_fake_sequence_as_json(xdg, make_watch):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w", "--fake", "true,false", "--payload", '["x"]', "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [poll["outcome"] for poll in data["polls"]] == ["true", "false"]
    assert data["polls"][0]["planned_actions"] == ["on_true"]
    assert data["polls"][0]["payload"] == ["x"]
    assert any(record["event"] == "check.outcome" for record in data["records"])
    assert not (watch_dir / "ran").exists()


def test_initial_condition_option(xdg, make_watch):
    make_watch("w", files={"watch.py": "def check(ctx):\n    return False\n\ndef on_fall(ctx):\n    pass\n"})
    data = json.loads(run("poll", "w", "--initial-condition", "true", "--json").output)
    assert data["polls"][0]["planned_actions"] == ["on_fall"]


def test_usage_errors_exit_2(xdg, make_watch):
    make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w", "--payload", "[1]")
    assert result.exit_code == 2 and "--payload only applies together with --fake" in result.output
    result = run("poll", "w", "--fake", "maybe")
    assert result.exit_code == 2 and "'maybe' is not an outcome" in result.output
    result = run("poll", "w", "--fake", "true", "--payload", "{nope")
    assert result.exit_code == 2 and "not valid JSON" in result.output


def test_unknown_watch_and_bad_config_exit_1(xdg, make_watch):
    make_watch("psg-export", config='intervall = "30s"\n')
    result = run("poll", "psg-exprot")
    assert result.exit_code == 1 and "did you mean 'psg-export'" in result.output
    result = run("poll", "psg-export")
    assert result.exit_code == 1 and "config.toml:1: unknown key 'intervall'" in result.output


def test_failed_poll_exits_1(xdg, make_watch):
    make_watch("w", config='[hooks]\ncheck = "exit 9"\n')
    result = run("poll", "w")
    assert result.exit_code == 1
    assert "check → error" in result.output


def test_config_option_selects_watch_dirs(xdg, tmp_path, make_watch):
    other = tmp_path / "elsewhere"
    make_watch("x", files={"watch.py": WATCH_PY}, base=other)
    config = tmp_path / "custom.toml"
    config.write_text(f"watch_dirs = [{toml_path(other)}]\n")
    assert run("--config", str(config), "poll", "x").exit_code == 0
