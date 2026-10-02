import hashlib
import shutil
import socket

import pytest

from keepwatch import CommandFailed
from keepwatch.transfer import ScpOptions, TransferFailed, pull, push, tcp_open
from scp_server import PASSWORD, ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")
REMOTE = "u@127.0.0.1:"


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


@pytest.fixture
def ssh_config(tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")  # keep the developer's ~/.ssh/config out of the tests
    return config


def password_options(server, ssh_config, monkeypatch, **extra):
    monkeypatch.setenv("KW_TEST_PASSWORD", PASSWORD)
    return ScpOptions(
        port=server.port,
        known_hosts=server.known_hosts,
        password_env="KW_TEST_PASSWORD",
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none", "-o", "PubkeyAuthentication=no"),
        timeout=60,
        **extra,
    )


def key_options(server, ssh_config, **extra):
    return ScpOptions(
        port=server.port,
        known_hosts=server.known_hosts,
        identity=server.client_key,
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none"),
        timeout=60,
        **extra,
    )


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_push_with_a_password_writes_the_file_and_its_marker(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    remote = push(local, REMOTE, options=password_options(server, ssh_config, monkeypatch))
    assert remote == "u@127.0.0.1:a.tar.gz"
    assert (server.root / "a.tar.gz").read_bytes() == b"payload"
    assert (server.root / "a.tar.gz.sha256").read_text(encoding="utf-8") == f"{sha(b'payload')}  a.tar.gz\n"


def test_push_without_a_marker(tmp_path, server, ssh_config):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    push(local, REMOTE, marker="none", options=key_options(server, ssh_config))
    assert sorted(path.name for path in server.root.iterdir()) == ["a.tar.gz"]


def test_pull_verifies_then_renames(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    final = pull(REMOTE + "b.gz", stage, size=11, sha256=sha(b"remote data"), options=key_options(server, ssh_config))
    assert final == stage / "b.gz" and final.read_bytes() == b"remote data"
    assert sorted(path.name for path in stage.iterdir()) == ["b.gz"]


def test_pull_with_a_wrong_checksum_leaves_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    with pytest.raises(TransferFailed, match="sha256"):
        pull(REMOTE + "b.gz", stage, sha256="0" * 64, options=key_options(server, ssh_config))
    assert list(stage.iterdir()) == []


def test_pull_with_a_wrong_size_leaves_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    with pytest.raises(TransferFailed, match="expected 99"):
        pull(REMOTE + "b.gz", stage, size=99, options=key_options(server, ssh_config))
    assert list(stage.iterdir()) == []


def test_pulling_an_identical_file_again_does_nothing(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    stage = tmp_path / "stage"
    calls = []
    options = key_options(server, ssh_config)
    pull(REMOTE + "b.gz", stage, sha256=sha(b"remote data"), options=options, report=lambda *a: calls.append(a))
    pull(REMOTE + "b.gz", stage, sha256=sha(b"remote data"), options=options, report=lambda *a: calls.append(a))
    assert len(calls) == 1


def test_pull_conflicts(tmp_path, server, ssh_config):
    (server.root / "b.tar.gz").write_bytes(b"new")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "b.tar.gz").write_bytes(b"old")
    options = key_options(server, ssh_config)
    with pytest.raises(TransferFailed, match="already exists"):
        pull(REMOTE + "b.tar.gz", stage, options=options)
    assert pull(REMOTE + "b.tar.gz", stage, on_conflict="rename", options=options) == stage / "b-1.tar.gz"
    assert pull(REMOTE + "b.tar.gz", stage, on_conflict="overwrite", options=options) == stage / "b.tar.gz"
    assert (stage / "b.tar.gz").read_bytes() == b"new" and (stage / "b-1.tar.gz").read_bytes() == b"new"


def test_wrong_password_fails(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    options = password_options(server, ssh_config, monkeypatch)
    monkeypatch.setenv("KW_TEST_PASSWORD", "wrong")
    with pytest.raises(CommandFailed, match="Permission denied"):
        push(local, REMOTE, options=options)
    assert not (server.root / "a.gz").exists()


def test_unknown_host_key_is_refused(tmp_path, server, ssh_config):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    empty = tmp_path / "empty_known_hosts"
    empty.write_text("", encoding="utf-8")
    options = ScpOptions(
        port=server.port,
        known_hosts=empty,
        identity=server.client_key,
        ssh_options=("-F", str(ssh_config), "-o", "IdentityAgent=none"),
        timeout=60,
    )
    with pytest.raises(CommandFailed, match="Host key verification failed"):
        push(local, REMOTE, options=options)


def test_missing_remote_file(tmp_path, server, ssh_config):
    with pytest.raises(CommandFailed):
        pull(REMOTE + "nothere.gz", tmp_path / "stage", options=key_options(server, ssh_config))
    assert list((tmp_path / "stage").iterdir()) == []


def test_unset_password_variable(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    options = password_options(server, ssh_config, monkeypatch)
    monkeypatch.delenv("KW_TEST_PASSWORD")
    with pytest.raises(TransferFailed, match="KW_TEST_PASSWORD, which is not set"):
        push(local, REMOTE, options=options)


def test_tcp_open(server):
    assert tcp_open("127.0.0.1", server.port, timeout=5) is True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    assert tcp_open("127.0.0.1", closed, timeout=5) is False


def test_reports_each_scp_run(tmp_path, server, ssh_config):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    calls = []
    push(local, REMOTE, options=key_options(server, ssh_config), report=lambda *call: calls.append(call))
    assert len(calls) == 2  # the file, then its marker
    argv, returncode, timed_out, duration, stdout, stderr = calls[0]
    assert argv[0] == "scp" and returncode == 0 and timed_out is False and duration >= 0


def test_pull_a_name_with_a_space(tmp_path, server, ssh_config):
    (server.root / "a b.tar.gz").write_bytes(b"spaced")
    final = pull(REMOTE + "a b.tar.gz", tmp_path / "stage", size=6, options=key_options(server, ssh_config))
    assert final.name == "a b.tar.gz" and final.read_bytes() == b"spaced"


def test_push_a_name_with_a_space(tmp_path, server, ssh_config):
    local = tmp_path / "c d.txt"
    local.write_bytes(b"up")
    assert push(local, REMOTE, options=key_options(server, ssh_config)) == "u@127.0.0.1:c d.txt"
    assert (server.root / "c d.txt").read_bytes() == b"up"
    assert (server.root / "c d.txt.sha256").exists()


def test_a_retried_rename_reuses_the_identical_copy(tmp_path, server, ssh_config):
    (server.root / "b.tar.gz").write_bytes(b"new")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "b.tar.gz").write_bytes(b"old")
    options = key_options(server, ssh_config)
    first = pull(REMOTE + "b.tar.gz", stage, size=3, sha256=sha(b"new"), on_conflict="rename", options=options)
    again = pull(REMOTE + "b.tar.gz", stage, size=3, sha256=sha(b"new"), on_conflict="rename", options=options)
    assert first == again == stage / "b-1.tar.gz"
    assert sorted(path.name for path in stage.iterdir()) == ["b-1.tar.gz", "b.tar.gz"]


def test_a_failed_upload_sends_no_marker(tmp_path, server, ssh_config):
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    calls = []
    with pytest.raises(CommandFailed):
        push(local, REMOTE + "no/such/dir/", options=key_options(server, ssh_config), report=lambda *call: calls.append(call))
    assert len(calls) == 1  # the data failed, so its marker was never sent
    assert not list(server.root.rglob("*.sha256"))


def test_pull_mismatches_raise_transfer_mismatch(tmp_path, server, ssh_config):
    from keepwatch.transfer import TransferMismatch

    (server.root / "b.gz").write_bytes(b"remote data")
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", size=99, options=key_options(server, ssh_config))
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", sha256="0" * 64, options=key_options(server, ssh_config))
