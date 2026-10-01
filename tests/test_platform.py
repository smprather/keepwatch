import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from keepwatch import platform


def test_pid_alive():
    assert platform.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert platform.pid_alive(child.pid) is False


@pytest.mark.posix_only
def test_posix_default_dirs():
    config, state, runtime = platform.default_app_dirs({"HOME": "/home/u", "TMPDIR": "/var/tmp"}, uid=7)
    assert (config, state, runtime) == (
        Path("/home/u/.config/keepwatch"),
        Path("/home/u/.local/state/keepwatch"),
        Path("/var/tmp/keepwatch-7"),
    )


@pytest.mark.windows_only
def test_windows_default_dirs():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    config, state, runtime = platform.default_app_dirs(env)
    assert config == Path(r"C:\Users\u\AppData\Roaming\keepwatch")
    assert state == Path(r"C:\Users\u\AppData\Local\keepwatch")
    assert runtime == Path(r"C:\Users\u\AppData\Local\keepwatch\run")


GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


def test_kill_takes_the_whole_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    process = platform.start_process(
        [sys.executable, "-c", GRANDCHILD, str(pid_file)],
        cwd=tmp_path, env=None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline, "the grandchild never started"
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    process.kill()
    process.popen.wait(10)
    process.close()
    assert process.terminated is True
    deadline = time.monotonic() + 10
    while platform.pid_alive(grandchild):
        assert time.monotonic() < deadline, "the grandchild survived"
        time.sleep(0.05)


def test_pipe_arguments_name_the_os_mechanism():
    read_fd, write_fd = os.pipe()
    try:
        args = platform.pipe_arguments(read_fd, write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    expected = ("--request-handle", "--result-handle") if platform.IS_WINDOWS else ("--request-fd", "--result-fd")
    assert (args[0], args[2]) == expected


def test_open_shared_allows_rename(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    with platform.open_shared(path) as handle:
        path.rename(tmp_path / "b.txt")
        assert handle.read() == b"x"
    with pytest.raises(FileNotFoundError):
        platform.open_shared(tmp_path / "missing.txt")


def test_replace(tmp_path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.write_text("new")
    target.write_text("old")
    platform.replace(source, target)
    assert target.read_text() == "new" and not source.exists()


@pytest.mark.windows_only
def test_replace_retries_while_the_target_is_briefly_open(tmp_path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.write_text("new")
    target.write_text("old")
    handle = open(target, "rb")
    threading.Timer(0.3, handle.close).start()
    platform.replace(source, target)
    assert target.read_text() == "new"


def test_shell_argv(tmp_path):
    assert platform.shell_argv("exit 3", ["pwsh", "-Command"]) == ["pwsh", "-Command", "exit 3"]
    argv = platform.shell_argv("exit 3")
    if platform.IS_WINDOWS:
        assert argv[0] == "powershell.exe" and argv[-1].endswith("exit 3") and "UTF8Encoding" in argv[-1]
    else:
        assert argv == ["/bin/sh", "-c", "exit 3"]
    assert platform.absolute_path("relative") is None
    assert platform.absolute_path(str(tmp_path)) == tmp_path
