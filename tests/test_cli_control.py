from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.locks import hold_lock
from keepwatch.offline import read_offline
from keepwatch.paths import ensure_private_dir


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_disable_then_enable(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    result = run("disable", "a")
    assert result.exit_code == 0 and "a disabled" in result.output
    marker = read_offline(xdg, "a")
    assert marker.by_user is True and marker.reason == "disabled by user"
    assert "already disabled" in run("disable", "a").output
    result = run("enable", "a")
    assert result.exit_code == 0 and "a enabled" in result.output
    assert read_offline(xdg, "a") is None
    result = run("enable", "a")
    assert result.exit_code == 0 and "was not offline" in result.output


def test_unknown_names(xdg, make_watch):
    make_watch("psg-export", config='[hooks]\ncheck = ["true"]\n')
    for command in ("enable", "disable"):
        result = run(command, "psg-exprot")
        assert result.exit_code == 1 and "did you mean 'psg-export'" in result.output


def test_rename_moves_the_watch_and_its_state(xdg, make_watch):
    old_dir = make_watch("old", config='[hooks]\ncheck = ["true"]\n')
    ledger = xdg.watch_data_dir("old") / "ledgers" / "sent.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"version": 1, "entries": {}}')
    result = run("rename", "old", "new")
    assert result.exit_code == 0, result.output
    assert not old_dir.exists() and (old_dir.parent / "new" / "config.toml").exists()
    assert (xdg.watch_data_dir("new") / "ledgers" / "sent.json").exists()
    assert not xdg.watch_state_dir("old").exists()


def test_rename_refusals(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    result = run("rename", "a", "b")
    assert result.exit_code == 1 and "already exists" in result.output
    result = run("rename", "a", "bad name")
    assert result.exit_code == 1 and "not a valid watch name" in result.output
    xdg.watch_state_dir("c").mkdir(parents=True)
    result = run("rename", "a", "c")
    assert result.exit_code == 1 and "state left by an earlier watch" in result.output
    ensure_private_dir(xdg.runtime)
    with hold_lock(xdg.watch_lock("a")):
        result = run("rename", "a", "d")
    assert result.exit_code == 1 and "being polled" in result.output


def test_help_shows_every_panel(xdg):
    output = run("--help").output
    for panel in ("Run", "Develop", "Inspect", "Control"):
        assert panel in output
    for command in ("run", "validate", "poll", "status", "logs", "enable", "disable", "rename"):
        assert f" {command} " in output
