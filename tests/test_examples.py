import hashlib
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.config import load_watch_config
from keepwatch.reference import example_dirs, render_topic


def run(*args):
    return CliRunner().invoke(cli, list(args))


def install(xdg, name):
    source = next(d for d in example_dirs() if d.name == name)
    target = xdg.default_watches_dir / name
    shutil.copytree(source, target)
    return target


def test_examples_are_in_the_docs():
    text = render_topic("examples")
    assert [d.name for d in example_dirs()] == ["disk-space-ps", "greet-once", "psg-export", "relay-pull", "relay-push", "relay-receive", "site-down"]
    for directory in example_dirs():
        for path in directory.iterdir():
            if path.is_file():
                assert f"{directory.name}/{path.name}" in text


def test_psg_export_example_validates_and_polls(xdg):
    install(xdg, "psg-export")
    assert run("validate", "psg-export").exit_code == 0
    data = json.loads(run("poll", "psg-export", "--dry-run", "--json").output)
    assert data["polls"][0]["outcome"] == "false"


@pytest.mark.posix_only
def test_site_down_example_validates_and_polls(xdg):
    install(xdg, "site-down")
    assert run("validate", "site-down").exit_code == 0
    data = json.loads(run("poll", "site-down", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]


@pytest.mark.skipif(shutil.which("expect") is None, reason="expect is not installed")
def test_expect_example_runs_once(xdg):
    install(xdg, "greet-once")
    assert run("validate", "greet-once").exit_code == 0
    first = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert first["outcome"] == "true" and first["results"][0]["status"] == "ok"
    second = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert second["outcome"] == "false"


@pytest.mark.windows_only
def test_disk_space_example_runs_on_windows(xdg):
    install(xdg, "disk-space-ps")
    assert run("validate", "disk-space-ps").exit_code == 0
    data = json.loads(run("poll", "disk-space-ps", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]
    real = json.loads(run("poll", "disk-space-ps", "--json").output)["polls"][0]
    assert real["outcome"] in ("true", "false")


needs_openssh = pytest.mark.skipif(shutil.which("scp") is None or shutil.which("ssh") is None, reason="needs OpenSSH")


@needs_openssh
def test_relay_examples_validate(xdg):
    install(xdg, "relay-pull")
    install(xdg, "relay-push")
    result = run("validate", "relay-pull", "relay-push")
    assert result.exit_code == 0, result.output


@needs_openssh
def test_relay_push_example_waits_when_linux2_is_away(xdg):
    install(xdg, "relay-push")
    staging = Path.home() / "relay" / "staging"
    staging.mkdir(parents=True)
    data = json.loads(run("poll", "relay-push", "--json").output)
    poll = data["polls"][0]
    assert poll["outcome"] == "unknown" and poll["failed"] is False
    assert "linux2.example" in poll["reason"]


def test_relay_examples_share_the_staging_folder(xdg):
    pulled = load_watch_config(install(xdg, "relay-pull")).settings["local_dir"]
    pushed = load_watch_config(install(xdg, "relay-push")).settings["local_dir"]
    assert pulled == pushed == str(Path.home() / "relay" / "staging")


def test_relay_receive_example_processes_a_verified_arrival(xdg):
    watch_dir = install(xdg, "relay-receive")
    incoming = Path.home() / "incoming"
    incoming.mkdir(parents=True)
    (incoming / "a.tar.gz").write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    info = (incoming / "a.tar.gz").stat()
    event = {
        "event": "file",
        "path": str(incoming / "a.tar.gz"),
        "name": "a.tar.gz",
        "size": info.st_size,
        "mtime": info.st_mtime,
        "sha256": digest,
        "marker": str(incoming / "a.tar.gz.sha256"),
    }
    (incoming / "a.tar.gz.sha256").write_text(f"{digest}  a.tar.gz\n", encoding="utf-8")
    assert run("validate", "relay-receive").exit_code == 0
    result = run("poll", "relay-receive", "--events", json.dumps([event]), "--json")
    assert json.loads(result.output)["polls"][0]["failed"] is False, result.output
    done = Path.home() / "incoming" / "done"
    assert (done / "a.tar.gz").read_bytes() == b"payload" and (done / "a.tar.gz.sha256").exists()
    assert watch_dir.exists()


def test_relay_receive_example_leaves_a_file_that_changed_since_its_report(xdg):
    install(xdg, "relay-receive")
    incoming = Path.home() / "incoming"
    incoming.mkdir(parents=True)
    digest = hashlib.sha256(b"payload").hexdigest()
    (incoming / "a.tar.gz").write_bytes(b"payload, then more")  # a new upload under the same name has begun
    (incoming / "a.tar.gz.sha256").write_text(f"{digest}  a.tar.gz\n", encoding="utf-8")
    event = {
        "event": "file",
        "path": str(incoming / "a.tar.gz"),
        "name": "a.tar.gz",
        "size": len(b"payload"),
        "mtime": 1.5,
        "sha256": digest,
        "marker": str(incoming / "a.tar.gz.sha256"),
    }
    result = run("poll", "relay-receive", "--events", json.dumps([event]), "--json")
    assert json.loads(result.output)["polls"][0]["failed"] is False, result.output
    assert (incoming / "a.tar.gz").exists() and not (incoming / "done" / "a.tar.gz").exists()
