import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from keepwatch import askpass, transfer
from keepwatch.transfer import (
    SCP_DEFAULTS,
    Endpoint,
    ScpOptions,
    TransferFailed,
    copy,
    escape_remote_path,
    known_hosts_option,
    parse_endpoint,
    parse_openssh_version,
    pull,
    remote_path_arg,
    scp_argv,
    write_askpass,
)

OPTS = list(SCP_DEFAULTS)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/data/a.gz", Endpoint(path="/data/a.gz")),
        ("a.gz", Endpoint(path="a.gz")),
        ("C:\\data\\a.gz", Endpoint(path="C:\\data\\a.gz")),
        ("C:/data/a.gz", Endpoint(path="C:/data/a.gz")),
        ("./odd:name", Endpoint(path="./odd:name")),
        ("me@linux1:/data/a.gz", Endpoint(path="/data/a.gz", host="linux1", user="me")),
        ("linux1:out/", Endpoint(path="out/", host="linux1")),
        ("me@linux1:", Endpoint(path="", host="linux1", user="me")),
        ("scp://me@linux2:2222/incoming/a.gz", Endpoint(path="incoming/a.gz", host="linux2", user="me", port=2222, uri=True)),
        ("scp://linux2/x", Endpoint(path="x", host="linux2", uri=True)),
    ],
)
def test_parse_endpoint(text, expected):
    endpoint = parse_endpoint(text)
    assert endpoint == expected
    assert endpoint.scp_arg() == text


def test_bad_endpoints():
    for text in ("", "scp://me@/x", "scp://host"):
        with pytest.raises(ValueError):
            parse_endpoint(text)


def test_endpoint_name_and_child():
    assert parse_endpoint("me@h:/data/a.tar.gz").name == "a.tar.gz"
    assert parse_endpoint("h:in/").child("a.gz").scp_arg() == "h:in/a.gz"
    assert parse_endpoint("h:").child("a.gz").scp_arg() == "h:a.gz"
    assert parse_endpoint("host:/").child("a.gz").scp_arg() == "host:/a.gz"  # "h:/" would be drive H:
    assert parse_endpoint("scp://h:2222/in").child("a.gz").scp_arg() == "scp://h:2222/in/a.gz"
    assert parse_endpoint("out").child("a.gz").path == str(Path("out") / "a.gz")


def test_parse_openssh_version():
    assert parse_openssh_version("OpenSSH_10.5p1, OpenSSL 3.6.5 29 Sep 2026") == (10, 5)
    assert parse_openssh_version("OpenSSH_for_Windows_9.5p1, LibreSSL 3.8.2") == (9, 5)
    assert parse_openssh_version("OpenSSH_8.0p1, OpenSSL 1.1.1k  FIPS 25 Mar 2021") == (8, 0)
    assert parse_openssh_version("something else") is None


def argv_for(source, destination, version=(10, 5), **options):
    return scp_argv(parse_endpoint(source), parse_endpoint(destination), ScpOptions(**options), version)


def test_scp_argv_defaults():
    assert argv_for("a.gz", "me@h:in/") == ["scp", *OPTS, "-o", "BatchMode=yes", "-O", "--", "a.gz", "me@h:in/"]


def test_classic_protocol_needs_dash_o_only_on_openssh_9():
    assert "-O" not in argv_for("a.gz", "h:", version=(8, 9))
    assert "-O" not in argv_for("a.gz", "h:", version=None)


def test_scp_argv_with_a_password():
    argv = argv_for("a.gz", "h:", password_env="RELAY_PASSWORD")
    assert ["-o", "NumberOfPasswordPrompts=1"] == argv[len(OPTS) + 1 : len(OPTS) + 3]
    assert "BatchMode=yes" not in argv and "RELAY_PASSWORD" not in " ".join(argv)


def test_scp_argv_options_order():
    argv = argv_for(
        "a.gz", "h:", ssh_options=["-F", "cfg"], identity="k", port=2222, known_hosts="C:/my keys/kh"
    )
    assert argv == [
        "scp",
        "-F",
        "cfg",
        *OPTS,
        "-o",
        "BatchMode=yes",
        "-o",
        'UserKnownHostsFile="C:/my keys/kh"',
        "-i",
        "k",
        "-o",
        "IdentitiesOnly=yes",
        "-P",
        "2222",
        "-O",
        "--",
        "a.gz",
        "h:",
    ]


def test_sftp_protocol():
    assert "-O" not in argv_for("a.gz", "h:", protocol="sftp")
    with pytest.raises(TransferFailed, match="needs OpenSSH 9.0"):
        argv_for("a.gz", "h:", version=(8, 9), protocol="sftp")


def test_two_remote_endpoints_need_sftp_and_keys():
    with pytest.raises(TransferFailed, match="needs protocol = \"sftp\""):
        argv_for("a:x", "b:y")
    with pytest.raises(TransferFailed, match="key authentication"):
        argv_for("a:x", "b:y", protocol="sftp", password_env="PW")
    argv = argv_for("a:x", "b:y", protocol="sftp")
    assert "-3" in argv and "-O" not in argv


def test_a_transfer_needs_a_remote_endpoint():
    with pytest.raises(ValueError, match="at least one remote endpoint"):
        argv_for("a", "b")


def test_bad_options():
    for bad in ({"protocol": "ftp"}, {"password_env": "1BAD"}, {"port": 0}, {"timeout": 0}):
        with pytest.raises(ValueError):
            ScpOptions(**bad)


def test_askpass_prints_the_variable(monkeypatch, capfd):
    monkeypatch.setenv("KW_TEST_PW", "s3 cr'et\"$")
    assert askpass.main(["KW_TEST_PW", "u@h's password: "]) == 0
    assert capfd.readouterr().out == "s3 cr'et\"$\n"


def test_askpass_refuses_host_key_questions(monkeypatch, capfd):
    monkeypatch.setenv("KW_TEST_PW", "x")
    assert askpass.main(["KW_TEST_PW", "Are you sure you want to continue connecting (yes/no/[fingerprint])? "]) == 1
    captured = capfd.readouterr()
    assert captured.out == "" and "host keys must already be known" in captured.err


def test_askpass_without_the_variable(monkeypatch, capfd):
    monkeypatch.delenv("KW_TEST_PW", raising=False)
    assert askpass.main(["KW_TEST_PW", "password: "]) == 1
    assert "KW_TEST_PW is not set" in capfd.readouterr().err


def test_askpass_launcher_runs_the_helper(tmp_path):
    launcher = write_askpass(tmp_path, "KW_TEST_PW")
    env = {**os.environ, "KW_TEST_PW": "pa ss'\"$"}
    completed = subprocess.run([str(launcher), "u@h's password: "], env=env, capture_output=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.decode("utf-8").rstrip("\r\n") == "pa ss'\"$"
    assert "pa ss" not in launcher.read_text(encoding="utf-8")


def test_a_one_letter_host_with_a_slash_is_a_drive():
    assert parse_endpoint("h:/a.gz") == Endpoint(path="h:/a.gz")
    assert parse_endpoint("me@h:/a.gz").remote and parse_endpoint("scp://h/a.gz").remote


def test_escape_remote_path():
    assert escape_remote_path("/data/out/a.tar.gz") == "/data/out/a.tar.gz"
    assert escape_remote_path("/data/a b.tar.gz") == "/data/a\\ b.tar.gz"
    assert escape_remote_path("it's $HOME;(x)") == "it\\'s\\ \\$HOME\\;\\(x\\)"
    assert escape_remote_path("~/incoming/a b") == "~/incoming/a\\ b"
    assert escape_remote_path("~") == "~"
    assert escape_remote_path("dir/~x") == "dir/\\~x"


def test_scp_arg_is_the_plain_form():
    assert parse_endpoint("me@h:/out/a b.gz").scp_arg() == "me@h:/out/a b.gz"
    assert parse_endpoint("/local/a b.gz").scp_arg() == "/local/a b.gz"
    assert parse_endpoint("me@h:in/").child("a b.gz").scp_arg() == "me@h:in/a b.gz"
    assert parse_endpoint("scp://h:2222/in/a b.gz").scp_arg() == "scp://h:2222/in/a b.gz"


def test_remote_path_arg():
    # SFTP: the plain name, on every OS.
    assert remote_path_arg("/out/a b.gz", classic=False, source=True, windows=False) == "/out/a b.gz"
    assert remote_path_arg("/out/a b.gz", classic=False, source=False, windows=True) == "/out/a b.gz"
    # Classic protocol on POSIX: backslash escapes, both directions.
    assert remote_path_arg("/out/a b.gz", classic=True, source=True, windows=False) == "/out/a\\ b.gz"
    assert remote_path_arg("~/in/c d.txt", classic=True, source=False, windows=False) == "~/in/c\\ d.txt"
    # Classic protocol on Windows: '?' for a pull, quotes for a push.
    assert remote_path_arg("/out/a b.gz", classic=True, source=True, windows=True) == "/out/a?b.gz"
    assert remote_path_arg("~/in/c d.txt", classic=True, source=False, windows=True) == "~/'in/c d.txt'"
    assert remote_path_arg("in/it's.txt", classic=True, source=False, windows=True) == '"in/it\'s.txt"'
    assert remote_path_arg("in/plain.txt", classic=True, source=False, windows=True) == "in/plain.txt"
    with pytest.raises(TransferFailed, match="Windows"):
        remote_path_arg("in/it's \"x\".txt", classic=True, source=False, windows=True)


def test_uri_endpoints_become_dash_p():
    argv = argv_for("scp://me@h:2222/in/a.gz", "a.gz")
    assert argv[-2:] == ["me@h:in/a.gz", "a.gz"] and argv[argv.index("-P") + 1] == "2222"
    with pytest.raises(TransferFailed, match="different ports"):
        argv_for("scp://a:2222/x", "scp://b:2223/y", protocol="sftp")


@pytest.mark.posix_only
def test_a_timeout_stops_scp_and_its_ssh_child(tmp_path):
    fake = tmp_path / "scp"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])  # holds our stderr\n"
        "signal.signal(signal.SIGTERM, lambda *args: (child.kill(), sys.exit(1)))  # what scp does for its ssh\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(TransferFailed, match="timed out"):
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(scp_command=[str(fake)], timeout=1))
    assert time.monotonic() - started < 20


def test_local_names_with_a_colon_get_dot_slash():
    def last(local):
        return scp_argv(parse_endpoint("me@h:a.gz"), Endpoint(path=local), ScpOptions(), (10, 5))[-1]

    assert last(".run-12:00.txt.part") == "./.run-12:00.txt.part"
    assert last("C:/x/a.gz") == "C:/x/a.gz"
    assert last("/abs/a:b") == "/abs/a:b"


def test_domain_users():
    assert parse_endpoint("CORP\\me@winhost:C:/data/a.gz") == Endpoint(path="C:/data/a.gz", host="winhost", user="CORP\\me")
    assert parse_endpoint("a\\b:c") == Endpoint(path="a\\b:c")


def test_an_endpoint_port_becomes_dash_p():
    argv = scp_argv(Endpoint(path="in/a.gz", host="h", port=2222), parse_endpoint("a.gz"), ScpOptions(), (10, 5))
    assert argv[argv.index("-P") + 1] == "2222"


def test_known_hosts_option():
    assert known_hosts_option("C:/my keys/kh") == ["-o", 'UserKnownHostsFile="C:/my keys/kh"']


def test_ssh_options_must_be_a_list_of_arguments():
    with pytest.raises(ValueError, match="not a string"):
        ScpOptions(ssh_options="-oProxyJump=b")
    with pytest.raises(ValueError, match="must start with an option"):
        ScpOptions(ssh_options=["ProxyJump=b"])


def test_unknown_versions_are_not_cached(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise OSError("busy")
        return subprocess.CompletedProcess(argv, 0, "", "OpenSSH_10.5p1, OpenSSL 3")

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)
    assert transfer.openssh_version("fake-ssh-for-test") is None
    assert transfer.openssh_version("fake-ssh-for-test") == (10, 5)
    assert transfer.openssh_version("fake-ssh-for-test") == (10, 5)
    assert len(calls) == 2


def test_pull_rejects_a_malformed_sha256(tmp_path):
    with pytest.raises(ValueError, match="64 hex"):
        pull("me@h:a.gz", tmp_path, sha256="abc123")


def test_a_past_deadline_stops_a_transfer_before_scp(tmp_path):
    reports = []
    with pytest.raises(TransferFailed, match="no time left"):
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(deadline=time.time() - 1), report=lambda *a: reports.append(a))
    assert reports == []


@pytest.mark.posix_only
def test_scp_runs_in_the_callers_process_group(tmp_path):
    probe = tmp_path / "scp"
    probe.write_text(f"#!{sys.executable}\nimport os, sys\nsys.stderr.write(str(os.getpgid(0)))\nsys.exit(1)\n")
    probe.chmod(0o755)
    with pytest.raises(transfer.CommandFailed) as info:
        copy(tmp_path / "a.gz", "me@h:", options=ScpOptions(scp_command=[str(probe)]))
    assert info.value.stderr.strip() == str(os.getpgid(0))


@pytest.mark.windows_only
def test_pull_refuses_names_windows_cannot_store(tmp_path):
    with pytest.raises(TransferFailed, match="Windows"):
        pull("me@h:/x/a:b.gz", tmp_path)


def test_extra_sources_follow_the_first():
    argv = scp_argv(parse_endpoint("a.gz"), parse_endpoint("me@h:in/"), ScpOptions(), (10, 5), extra_sources=[parse_endpoint("a.gz.sha256")])
    assert argv[-3:] == ["a.gz", "a.gz.sha256", "me@h:in/"]
