import shutil
import time

import pytest

from keepwatch import Ctx, TransferFailed
from scp_server import ScpServer

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def make_ctx(tmp_path, records, hook="on_true", deadline=None):
    return Ctx(
        watch="w",
        hook=hook,
        poll_id="p1",
        condition=True,
        payload=None,
        settings={},
        watch_dir=tmp_path,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        deadline=deadline or time.time() + 60,
        emit=records.append,
    )


def key_kwargs(server, tmp_path):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return {
        "port": server.port,
        "known_hosts": server.known_hosts,
        "identity": server.client_key,
        "ssh_options": ("-F", str(config), "-o", "IdentityAgent=none"),
    }


def test_ctx_transfer_logs_like_ctx_run(tmp_path, server):
    (tmp_path / "a.gz").write_bytes(b"x")
    records = []
    ctx = make_ctx(tmp_path, records)
    assert ctx.transfer.push("a.gz", "u@127.0.0.1:", marker="none", **key_kwargs(server, tmp_path)) == "u@127.0.0.1:a.gz"
    [record] = records
    assert (record["type"], record["exit_code"], record["shell"], record["argv"][0]) == ("command", 0, False, "scp")
    assert (server.root / "a.gz").read_bytes() == b"x"


def test_relative_local_paths_start_at_the_watch_dir(tmp_path, server):
    (server.root / "b.gz").write_bytes(b"y")
    ctx = make_ctx(tmp_path, [])
    final = ctx.transfer.pull("u@127.0.0.1:b.gz", "stage", **key_kwargs(server, tmp_path))
    assert final == tmp_path / "stage" / "b.gz"


def test_a_check_cannot_transfer(tmp_path, server):
    ctx = make_ctx(tmp_path, [], hook="check")
    with pytest.raises(TransferFailed, match="a check must not transfer files"):
        ctx.transfer.pull("u@127.0.0.1:b.gz", "stage", **key_kwargs(server, tmp_path))
    assert ctx.transfer.tcp_open("127.0.0.1", server.port) is True


def test_the_hook_deadline_caps_the_timeout(tmp_path):
    ctx = make_ctx(tmp_path, [], deadline=time.time() - 1)
    with pytest.raises(TransferFailed, match="no time left"):
        ctx.transfer.copy("a.gz", "u@127.0.0.1:")


def test_unknown_option_names_are_errors(tmp_path):
    with pytest.raises(TypeError):
        make_ctx(tmp_path, []).transfer.copy("a.gz", "u@h:", pasword_env="X")
