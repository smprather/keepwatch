import inspect

import click
import pytest
from click.testing import CliRunner

from keepwatch import Ctx, Ledger
from keepwatch.cli import cli
from keepwatch.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, OBSERVER_KEYS, WATCH_KEYS, WATCH_TABLES
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


KEY_FACTS = {
    "python": ["keepwatch.Unknown", "(True, payload)", "1 MiB", "keepwatch_watch", "top-level"],
    "config": ["single-quoted"],
    "executables": ["KEEPWATCH_PAYLOAD_OUT", "KEEPWATCH_PAYLOAD_FILE", "KEEPWATCH_SETTINGS_FILE", "KEEPWATCH_EVENTS_FILE", "[check_exit_codes]", "/bin/sh -c", "Windows PowerShell", ".ps1", "UTF8Encoding"],
    "states": ["| `true` |", "initial_condition", "stop at the first failure", "never overlap"],
    "failures": ["max_failures", "retry_after", "alert_command", "KEEPWATCH_ALERT_EVENT", "offline.json"],
    "storage": ["ctx.data_dir", "ctx.run_dir", "file_key", "LedgerCorrupt", "keepwatch rename", "%LOCALAPPDATA%"],
    "logging": ["keepwatch.jsonl", "poll_id", "hook.end", "check.outcome", "observer.stopped", "extra=", "data_clipped", "observer.connected"],
    "environment": ["/dev/null", "SIGTERM", "SSH_AUTH_SOCK", "BatchMode=yes", "[environment]", "Job Object", "keepwatch stop"],
    "dependencies": ["python_dependencies", "uv run", "--offline", "keepwatch validate"],
    "reload": ["reload_interval", "last valid", "watch.removed", "offline.json"],
    "observers": ["[observe.", "./feed.py", "uv\", \"run\", \"--script", "ctx.events", "KEEPWATCH_EVENTS_FILE", "at-least-once", "heartbeat_timeout", "keepwatch observe", "--events", "wake", "settle", "flush", "ledger", "FileNotFoundError", "64 MiB", "1 second after", "do not restart", "remote_files", "remote_python", "BatchMode", "known_hosts", "observer.connected", "Python 3.6", "ssh-agent"],
}


@pytest.mark.parametrize("topic", sorted(KEY_FACTS))
def test_narrative_topics_cover_their_key_facts(topic):
    text = render_topic(topic)
    missing = [fact for fact in KEY_FACTS[topic] if fact not in text]
    assert not missing, f"{topic} is missing {missing}"


def test_every_config_key_is_documented():
    text = render_topic("config")
    for key in (*WATCH_KEYS, *GLOBAL_KEYS, *LOG_KEYS, *OBSERVER_KEYS):
        assert key.doc.strip(), key.name
        assert f"`{key.name}`" in text, key.name
    for table, doc in (*WATCH_TABLES.items(), *GLOBAL_TABLES.items()):
        assert doc.strip() and f"`[{table}]`" in text, table


def test_every_public_ctx_and_ledger_member_is_documented():
    text = render_topic("ctx")
    for owner, prefix in ((Ctx, "ctx."), (Ledger, "ledger.")):
        for name, member in inspect.getmembers(owner):
            if name.startswith("_"):
                continue
            assert inspect.getdoc(member), f"{owner.__name__}.{name} has no docstring"
            assert f"{prefix}{name}" in text, name


def test_every_command_and_option_is_documented():
    from keepwatch.cli import cli

    text = render_topic("cli")
    ctx = click.Context(cli, info_name="keepwatch")
    for name in cli.list_commands(ctx):
        command = cli.get_command(ctx, name)
        assert command.help and command.help.strip(), name
        assert f"## keepwatch {name}" in text
        for param in command.params:
            if isinstance(param, click.Option):
                assert param.help, f"{name} {param.opts}"
                assert param.opts[-1] in text
    assert "UNSET" not in text and "Sentinel" not in text
