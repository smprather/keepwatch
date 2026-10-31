import hashlib
import os
import shutil
import socket

import pytest

from scp_server import PASSWORD, ScpServer
from watchglass import CommandFailed
from watchglass.transfer import (
    Endpoint,
    ScpOptions,
    TransferAuthFailed,
    TransferFailed,
    TransferMissing,
    _reget_source_missing,
    _source_missing,
    pull,
    push,
    tcp_open,
    upload,
)

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


def test_upload_resumes_a_partial_remote_file(tmp_path, server, ssh_config):
    """The mirror's resumable push: sftp reput starts where the temp file stopped, not at byte zero."""
    data = bytes(range(256)) * 4000  # 1 MiB
    (server.root / "part.bin").write_bytes(data[:300_000])
    local = tmp_path / "part.bin"
    local.write_bytes(data)
    upload(local, REMOTE + "part.bin", options=key_options(server, ssh_config, protocol="sftp"), resume=True)
    assert (server.root / "part.bin").read_bytes() == data
    assert server.writes and server.writes[0] == 300_000


def test_upload_without_resume_starts_over(tmp_path, server, ssh_config):
    local = tmp_path / "part.bin"
    local.write_bytes(b"new content")
    (server.root / "part.bin").write_bytes(b"old content that is longer")
    upload(local, REMOTE + "part.bin", options=key_options(server, ssh_config, protocol="sftp"))
    assert (server.root / "part.bin").read_bytes() == b"new content"


def test_upload_resume_needs_sftp(tmp_path, server, ssh_config):
    local = tmp_path / "part.bin"
    local.write_bytes(b"x")
    with pytest.raises(ValueError, match="sftp"):
        upload(local, REMOTE + "part.bin", options=key_options(server, ssh_config), resume=True)


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


def test_wrong_password_is_a_permanent_rejection(tmp_path, server, ssh_config, monkeypatch):
    local = tmp_path / "a.gz"
    local.write_bytes(b"x")
    options = password_options(server, ssh_config, monkeypatch)
    monkeypatch.setenv("KW_TEST_PASSWORD", "wrong")
    with pytest.raises(TransferAuthFailed, match="Permission denied") as caught:
        push(local, REMOTE, options=options)
    assert caught.value.permanent_reason == "authentication failed"
    assert not (server.root / "a.gz").exists()


def test_unknown_host_key_is_a_permanent_rejection(tmp_path, server, ssh_config):
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
    with pytest.raises(TransferAuthFailed, match="Host key verification failed") as caught:
        push(local, REMOTE, options=options)
    assert caught.value.permanent_reason == "host key verification failed"


def test_missing_remote_file(tmp_path, server, ssh_config):
    with pytest.raises(TransferMissing):
        pull(REMOTE + "nothere.gz", tmp_path / "stage", options=key_options(server, ssh_config))
    assert list((tmp_path / "stage").iterdir()) == []


@pytest.mark.parametrize(
    "stderr",
    [
        "scp: /remote/a.bin: No such file or directory\n",  # OpenSSH's remote scp (classic) and its sftp mode
        "scp: No such file or directory: /remote/a.bin\n",  # the other ordering: asyncssh's server, as in this suite
        "scp: The system cannot find the file specified: C:\\remote\\a.bin\n",  # classic on Windows
        "scp: The system cannot find the path specified: C:\\remote\\a.bin\n",
    ],
)
def test_scp_says_a_source_is_gone(stderr):
    """Both orderings are real: OpenSSH's remote scp prints `<path>: <reason>`, asyncssh's server `<reason>: <path>`."""
    assert _source_missing(stderr, Endpoint(path="/stage/.a.bin.part"), Endpoint(path="/remote/a.bin"))


@pytest.mark.parametrize(
    "stderr",
    [
        "/stage/.a.bin.part: No such file or directory\n",  # the classic protocol's local-side failure
        'scp: open local "/stage/.a.bin.part": No such file or directory\r\n',  # its sftp mode
        "scp: The system cannot find the file specified: C:\\stage\\.a.bin.part\n",  # the local side on Windows
        "scp: /remote/a.bin: Permission denied\n",
    ],
)
def test_other_scp_failures_are_not_a_missing_source(stderr):
    assert not _source_missing(stderr, Endpoint(path="/stage/.a.bin.part"), Endpoint(path="/remote/a.bin"))


def test_a_gone_source_is_told_apart_when_both_sides_share_a_file_name():
    """`kit copy u@h:/data/report.tar.gz ./staging/report.tar.gz` on a gone source is still a withdrawal.

    The local-side veto compares whole paths: a source and a destination that share a file name must not
    look like the local side failing.
    """
    stderr = "scp: /data/report.tar.gz: No such file or directory\n"
    assert _source_missing(stderr, Endpoint(path="./staging/report.tar.gz"), Endpoint(path="/data/report.tar.gz"))


def test_a_local_side_failure_with_the_same_file_name_is_still_a_command_failure():
    """The other half of it: the local staging path named in the text is never a withdrawal."""
    stderr = "scp: ./staging/report.tar.gz: No such file or directory\n"
    assert not _source_missing(stderr, Endpoint(path="./staging/report.tar.gz"), Endpoint(path="/data/report.tar.gz"))


def test_a_windows_local_side_failure_is_recognised_through_its_separators():
    stderr = "scp: The system cannot find the path specified: C:\\stage\\.a.bin.part\n"
    assert not _source_missing(stderr, Endpoint(path="/stage/.a.bin.part"), Endpoint(path="/remote/a.bin"))


def test_a_gone_reget_source_is_matched_on_the_raw_path():
    """sftp prints the path it parsed, not the quoting the batch was written with."""
    stderr = 'File "/remote/back\\slash.bin" not found.\n'
    assert _reget_source_missing(stderr, Endpoint(path="/remote/back\\slash.bin"))


def test_a_gone_reget_source_with_a_quote_in_its_name_still_matches():
    stderr = "File \"/remote/it's \"quoted\".bin\" not found.\n"
    assert _reget_source_missing(stderr, Endpoint(path="/remote/it's \"quoted\".bin"))


def test_a_gone_reget_source_printed_in_its_escaped_form_still_matches():
    stderr = 'File "/remote/back\\\\slash.bin" not found.\n'
    assert _reget_source_missing(stderr, Endpoint(path="/remote/back\\slash.bin"))


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
    argv, returncode, timed_out, duration, _stdout, _stderr = calls[0]
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
    from watchglass.transfer import TransferMismatch

    (server.root / "b.gz").write_bytes(b"remote data")
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", size=99, options=key_options(server, ssh_config))
    with pytest.raises(TransferMismatch):
        pull(REMOTE + "b.gz", tmp_path / "stage", sha256="0" * 64, options=key_options(server, ssh_config))


def test_push_preserves_the_mtime(tmp_path, server, ssh_config):
    """scp -p carries the source's mtime to the destination (issue 12)."""
    local = tmp_path / "a.tar.gz"
    local.write_bytes(b"payload")
    os.utime(local, (1_700_000_000.0, 1_700_000_000.0))
    push(local, REMOTE, marker="none", options=key_options(server, ssh_config, preserve_mtime=True))
    assert (server.root / "a.tar.gz").stat().st_mtime == 1_700_000_000.0


def test_pull_preserves_the_mtime(tmp_path, server, ssh_config):
    (server.root / "b.gz").write_bytes(b"remote data")
    os.utime(server.root / "b.gz", (1_700_000_000.0, 1_700_000_000.0))
    final = pull(REMOTE + "b.gz", tmp_path / "stage", options=key_options(server, ssh_config, preserve_mtime=True))
    assert final.stat().st_mtime == 1_700_000_000.0


def test_without_preserve_mtime_the_destination_carries_the_transfer_time(tmp_path, server, ssh_config):
    """The default stays exactly what it was: the destination gets the transfer's own time."""
    (server.root / "b.gz").write_bytes(b"remote data")
    os.utime(server.root / "b.gz", (1_700_000_000.0, 1_700_000_000.0))
    final = pull(REMOTE + "b.gz", tmp_path / "stage", options=key_options(server, ssh_config))
    assert final.stat().st_mtime != 1_700_000_000.0
