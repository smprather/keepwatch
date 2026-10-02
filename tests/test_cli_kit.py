import shutil
import socket

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from scp_server import ScpServer

needs_scp = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


def run(*args):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def common(server, tmp_path):
    return [
        "--port",
        str(server.port),
        "--known-hosts",
        str(server.known_hosts),
        "--identity",
        str(server.client_key),
        "--ssh-option",
        "IdentityAgent=none",
    ]


@needs_scp
def test_kit_push_then_pull(tmp_path, server, xdg):
    (tmp_path / "a.gz").write_bytes(b"x")
    pushed = run("kit", "push", str(tmp_path / "a.gz"), "u@127.0.0.1:", *common(server, tmp_path))
    assert pushed.exit_code == 0, pushed.output
    assert pushed.stdout.strip() == "u@127.0.0.1:a.gz"
    assert (server.root / "a.gz.sha256").exists()
    pulled = run("kit", "pull", "u@127.0.0.1:a.gz", str(tmp_path / "stage"), "--size", "1", *common(server, tmp_path))
    assert pulled.exit_code == 0, pulled.output
    assert pulled.stdout.strip() == str(tmp_path / "stage" / "a.gz")


@needs_scp
def test_kit_failure_exits_1_with_the_reason(tmp_path, server, xdg):
    result = run("kit", "pull", "u@127.0.0.1:nothere.gz", str(tmp_path / "stage"), *common(server, tmp_path))
    assert result.exit_code == 1
    assert "scp" in result.stderr


def test_kit_tcp_open(xdg):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        opened = run("kit", "tcp-open", "127.0.0.1", "--port", str(port))
    assert (opened.exit_code, opened.stdout.strip()) == (0, "open")
    closed = run("kit", "tcp-open", "127.0.0.1", "--port", str(port), "--timeout", "2s")
    assert (closed.exit_code, closed.stdout.strip()) == (1, "closed")


def test_kit_bad_usage(xdg, tmp_path):
    assert run("kit", "push", str(tmp_path / "a.gz"), "u@h:", "--protocol", "ftp").exit_code == 2
    assert run("kit", "copy", "a", "b").exit_code == 2  # no remote endpoint
    assert run("kit", "pull", "u@h:a.gz", "stage", "--timeout", "soon").exit_code == 2
