from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.reference import TOPICS, render_all, render_topic, topic_index


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_every_topic_renders():
    for name, summary in TOPICS:
        text = render_topic(name)
        assert text.startswith("# "), name
        assert summary
    assert all(f"`{name}`" in topic_index() for name, _ in TOPICS)


def test_render_all_contains_every_topic_in_order():
    text = render_all()
    positions = [text.index("\n" + render_topic(name).splitlines()[0]) for name, _ in TOPICS]
    assert positions == sorted(positions)


def test_docs_without_topic_lists_topics(xdg):
    result = run("docs")
    assert result.exit_code == 0
    assert "keepwatch docs agent" in result.output


def test_docs_topic_is_raw_markdown_when_piped(xdg):
    result = run("docs", "agent")
    assert result.exit_code == 0
    assert result.output == render_topic("agent") + "\n"
    assert "\x1b[" not in result.output


def test_unknown_topic_suggests(xdg):
    result = run("docs", "agnet")
    assert result.exit_code == 1
    assert "did you mean 'agent'" in result.output


def test_help_points_agents_at_the_docs(xdg):
    assert "keepwatch docs agent" in run("--help").output
