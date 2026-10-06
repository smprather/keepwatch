"""`keepwatch tui`: the command itself (the app is exercised in tests/test_tui_app.py)."""

from click.testing import CliRunner

from keepwatch.cli import cli


def invoke(*args):
    return CliRunner().invoke(cli, list(args))


def test_tui_refuses_without_a_terminal(xdg):
    result = invoke("tui")
    assert result.exit_code == 1
    assert "needs a terminal" in result.output
    assert "keepwatch logs --json" in result.output


def test_tui_sits_in_the_inspect_group(xdg):
    assert "tui" in invoke("--help").output


def test_tui_is_documented(xdg):
    assert "## keepwatch tui" in invoke("docs", "cli").output
