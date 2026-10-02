import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from keepwatch.service import Service
from portable import PY, literal, toml_path
from scp_server import PASSWORD, ScpServer

pytestmark = [pytest.mark.posix_only, pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")]
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")
RECEIVE = """
    import json


    def check(ctx):
        return bool(ctx.events), ctx.events


    def on_true(ctx):
        with open(ctx.settings["log"], "a", encoding="utf-8") as handle:
            for event in ctx.payload:
                handle.write(json.dumps({"name": event["name"], "sha256": event["sha256"]}) + "\\n")
"""


def wait_for(predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_a_spawned_file_is_swept_to_linux2_and_received_once(tmp_path, make_watch, xdg, monkeypatch):
    linux1, linux2, stage = tmp_path / "linux1", tmp_path / "linux2", tmp_path / "stage"
    for directory in (linux1, linux2, tmp_path / "keys1", tmp_path / "keys2"):
        directory.mkdir()
    source = ScpServer(linux1, tmp_path / "keys1", chroot=False).start()
    dest = ScpServer(linux2, tmp_path / "keys2").start()
    try:
        ssh_config = tmp_path / "ssh_config"
        ssh_config.write_text("", encoding="utf-8")
        isolated = f"['-F', {toml_path(ssh_config)}, '-o', 'IdentityAgent=none'"
        make_watch(
            "relay-pull",
            config=(
                'interval = "1h"\nrecipe = "pull"\n[settings]\nremote = "u@127.0.0.1"\n'
                f"remote_dir = {toml_path(linux1)}\nlocal_dir = {toml_path(stage)}\nsettle = '1s'\n"
                f"port = {source.port}\nidentity = {toml_path(source.client_key)}\n"
                f"known_hosts = {toml_path(source.known_hosts)}\nremote_python = {literal(PY)}\n"
                f"ssh_command = [{literal(PY)}, {toml_path(FAKE_SSH)}]\nssh_options = {isolated}]\n"
                "delete_remote = true\n"
            ),
        )
        monkeypatch.setenv("KW_RELAY_PASSWORD", PASSWORD)
        make_watch(
            "relay-push",
            config=(
                'interval = "1h"\nrecipe = "push"\n[settings]\n'
                f'local_dir = {toml_path(stage)}\ndest = "u@127.0.0.1:"\nsettle = \'1s\'\nport = {dest.port}\n'
                f"password_env = 'KW_RELAY_PASSWORD'\nknown_hosts = {toml_path(dest.known_hosts)}\n"
                f"ssh_options = {isolated}, '-o', 'PubkeyAuthentication=no']\n"
            ),
        )
        received = tmp_path / "received.jsonl"
        make_watch(
            "relay-receive",
            config=(
                'interval = "1h"\n[observe.arrivals]\nkind = "files"\n'
                f"path = {toml_path(linux2)}\npattern = '*.tar.gz'\nmarker = 'sha256'\nsettle = '1s'\n"
                f"[settings]\nlog = {toml_path(received)}\n"
            ),
            files={"watch.py": RECEIVE},
        )
        records = []
        service = Service(paths=xdg, config_path=xdg.config_file, sink=records.append)
        service.start()
        try:
            data = os.urandom(300_000)
            (linux1 / "export-1.tar.gz").write_bytes(data)
            done = wait_for(lambda: received.exists() and not (linux1 / "export-1.tar.gz").exists())
            problems = [r for r in records if r["level"] in ("WARNING", "ERROR", "CRITICAL")]
            assert done, problems
            time.sleep(3.0)  # nothing is received twice
        finally:
            service.stop()
        assert (linux2 / "export-1.tar.gz").read_bytes() == data
        lines = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines()]
        assert lines == [{"name": "export-1.tar.gz", "sha256": hashlib.sha256(data).hexdigest()}]
        assert list(stage.iterdir()) == []
    finally:
        source.stop()
        dest.stop()
