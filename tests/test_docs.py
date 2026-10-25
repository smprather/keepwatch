import inspect
import os
import subprocess
import sys

import click
import pytest
from click.testing import CliRunner

from watchglass import Ctx, Ledger
from watchglass.cli import cli
from watchglass.config import GLOBAL_KEYS, GLOBAL_TABLES, LOG_KEYS, OBSERVER_KEYS, WATCH_KEYS, WATCH_TABLES
from watchglass.reference import TOPICS, render_all, render_topic, topic_index


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


@pytest.mark.usefixtures("xdg")
def test_docs_without_topic_lists_topics():
    result = run("docs")
    assert result.exit_code == 0
    assert "watchglass docs agent" in result.output


@pytest.mark.usefixtures("xdg")
def test_docs_topic_is_raw_markdown_when_piped():
    result = run("docs", "agent")
    assert result.exit_code == 0
    assert result.output == render_topic("agent") + "\n"
    assert "\x1b[" not in result.output


@pytest.mark.usefixtures("xdg")
def test_unknown_topic_suggests():
    result = run("docs", "agnet")
    assert result.exit_code == 1
    assert "did you mean 'agent'" in result.output


@pytest.mark.usefixtures("xdg")
def test_help_points_agents_at_the_docs():
    assert "watchglass docs agent" in run("--help").output


KEY_FACTS = {
    "tui": ["watchglass tui", "read-only", "polling", "space", "status.json", "logs --json", "countdown", "orphaned", "in_flight", "format"],
    "python": ["watchglass.Unknown", "(True, payload)", "1 MiB", "watchglass_watch", "top-level"],
    "config": ["single-quoted"],
    "executables": ["WATCHGLASS_PAYLOAD_OUT", "WATCHGLASS_PAYLOAD_FILE", "WATCHGLASS_SETTINGS_FILE", "WATCHGLASS_EVENTS_FILE", "[check_exit_codes]", "/bin/sh -c", "Windows PowerShell", ".ps1", "UTF8Encoding", "watchglass kit"],
    "states": ["| `true` |", "initial_condition", "stop at the first failure", "never overlap"],
    "failures": ["max_failures", "retry_after", "alert_command", "WATCHGLASS_ALERT_EVENT", "offline.json", "retry_base", "Trial polls"],
    "storage": ["ctx.data_dir", "ctx.run_dir", "file_key", "LedgerCorrupt", "watchglass rename", "%LOCALAPPDATA%"],
    "logging": ["watchglass.jsonl", "poll_id", "hook.end", "check.outcome", "observer.stopped", "extra=", "data_clipped", "observer.connected"],
    "environment": ["/dev/null", "SIGTERM", "SSH_AUTH_SOCK", "BatchMode=yes", "[environment]", "Job Object", "watchglass stop", "Command Processor"],
    "dependencies": ["python_dependencies", "uv run", "--offline", "watchglass validate"],
    "reload": ["reload_interval", "last valid", "watch.removed", "offline.json"],
    "observers": ["[observe.", "./feed.py", "uv\", \"run\", \"--script", "ctx.events", "WATCHGLASS_EVENTS_FILE", "at-least-once", "heartbeat_timeout", "watchglass observe", "--events", "wake", "settle", "flush", "ledger", "FileNotFoundError", "64 MiB", "1 second after", "do not restart", "remote_files", "remote_python", "BatchMode", "known_hosts", "observer.connected", "Python 3.6", "ssh-agent", "not valid UTF-8", "IdentitiesOnly", "expire", "marker = \"sha256\""],
    "transfers": ["ctx.transfer", "watchglass kit", "password_env", "SSH_ASKPASS", "setx", "known_hosts", "-O", "scp -3", "protocol = \"sftp\"", ".part", ".sha256", "sha256sum -c", "tcp_open", "TransferFailed", "CommandFailed", "NumberOfPasswordPrompts", "must print nothing", "icacls", "TimeoutExpired", "ssh_options", "OWNER RIGHTS", "Windows' scp", "TransferMismatch", "password_mode", "conpty", "SFTPAuthorizedUserKeys", "reput", "refuses to overwrite"],
    "recipes": ["recipe = \"pull\"", "recipe = \"push\"", "pulled", "pushed", "skip_ledger", "unknown", "reachable_host", "staging", "at-least-once", "on_conflict", "delete_remote", "delete_retry", "to_delete", "interval", "the same size and modification time", "file_timeout", "equal share", "resume", "part_max_age"],
    "relay": ["relay-pull", "relay-push", "staging", "uv tool install watchglass", "ssh-keygen", "authorized_keys", "known_hosts", "setx RELAY_PASSWORD", "sha256sum -c", "$?prompt", "watchglass observe relay-pull", "scp -3", "unknown", "the same path", "relay-receive", "delete_remote = true", "Receiving on linux2"],
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


def test_every_recipe_setting_is_documented():
    from watchglass.config import RECIPE_SETTINGS

    text = render_topic("recipes")
    for recipe, keys in RECIPE_SETTINGS.items():
        assert f"## `{recipe}` settings" in text
        for key in keys:
            assert key.doc.strip() and f"`{key.name}`" in text, (recipe, key.name)


def test_every_public_ctx_and_ledger_member_is_documented():
    from watchglass.transfer import Transfer

    text = render_topic("ctx")
    for owner, prefix in ((Ctx, "ctx."), (Ledger, "ledger."), (Transfer, "ctx.transfer.")):
        for name, member in inspect.getmembers(owner):
            if name.startswith("_"):
                continue
            assert inspect.getdoc(member), f"{owner.__name__}.{name} has no docstring"
            assert f"{prefix}{name}" in text, name


def test_every_command_and_option_is_documented():
    from watchglass.cli import cli
    from watchglass.reference import walk_commands

    text = render_topic("cli")
    root = click.Context(cli, info_name="watchglass")
    names = []
    for name, command, _ in walk_commands(cli, root, "watchglass"):
        names.append(name)
        assert command.help and command.help.strip(), name
        assert f"## {name}" in text
        for param in command.params:
            if isinstance(param, click.Option):
                assert param.help, f"{name} {param.opts}"
                assert param.opts[-1] in text
    assert "watchglass kit pull" in names and "watchglass run" in names
    assert "UNSET" not in text and "Sentinel" not in text


def test_docs_write_utf8_to_a_cp1252_pipe(tmp_path):
    """A Windows pipe is encoded with the console code page, which cannot write '→' (issue 2)."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, "-m", "watchglass", "docs", "relay"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "→".encode("utf-8") in result.stdout
