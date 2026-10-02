import ast
from pathlib import Path

from keepwatch import observers, remote
from keepwatch.config import ObserverConfig

SOURCE = Path(remote.__file__)


def test_ssh_argv_matches_the_observer_command():
    config = ObserverConfig(
        name="r",
        kind="remote_files",
        remote="me@h",
        dir="/d",
        port=2222,
        identity=Path("/keys/id"),
        ssh_options=("-o", "ProxyJump=b"),
        remote_python="/opt/py/bin/python3",
        ssh_command=("ssh.exe",),
    )
    assert observers.remote_command(config) == remote.ssh_argv(
        "me@h",
        port=2222,
        identity=Path("/keys/id"),
        ssh_options=("-o", "ProxyJump=b"),
        ssh_command=("ssh.exe",),
        remote_python="/opt/py/bin/python3",
    )


def test_ssh_argv_defaults():
    assert remote.ssh_argv("me@h") == ["ssh", *remote.SSH_DEFAULTS, "--", "me@h", remote.AUTO_PYTHON]


def test_remote_is_stdlib_only():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    modules |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert modules <= {"__future__", "collections", "importlib", "json", "os", "shlex", "typing", "keepwatch"}


def test_with_known_hosts():
    assert remote.with_known_hosts(["-o", "X=1"], None) == ["-o", "X=1"]
    assert remote.with_known_hosts([], "C:/k h/kh") == ["-o", 'UserKnownHostsFile="C:/k h/kh"']
