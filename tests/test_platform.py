import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from watchglass import platform


def test_pid_alive():
    assert platform.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert platform.pid_alive(child.pid) is False


@pytest.mark.posix_only
def test_posix_default_dirs():
    config, state, runtime = platform.default_app_dirs({"HOME": "/home/u", "TMPDIR": "/var/tmp"}, uid=7)
    assert (config, state, runtime) == (
        Path("/home/u/.config/watchglass"),
        Path("/home/u/.local/state/watchglass"),
        Path("/var/tmp/watchglass-7"),
    )


@pytest.mark.windows_only
def test_windows_default_dirs():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    config, state, runtime = platform.default_app_dirs(env)
    assert config == Path(r"C:\Users\u\AppData\Roaming\watchglass")
    assert state == Path(r"C:\Users\u\AppData\Local\watchglass")
    assert runtime == Path(r"C:\Users\u\AppData\Local\watchglass\run")


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


def test_windows_script_hooks_never_go_through_cmd_autorun(monkeypatch, tmp_path):
    r"""cmd.exe reads HKCU\...\Command Processor\AutoRun before every /c it runs; /d skips it (issue 7)."""
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    cmd = tmp_path / "check.cmd"
    cmd.write_text("@echo off\r\n", encoding="utf-8")
    assert platform.command_argv([str(cmd)], tmp_path) == ["cmd.exe", "/d", "/c", str(cmd)]
    assert platform.command_argv([str(tmp_path / "check.bat")], tmp_path)[:3] == ["cmd.exe", "/d", "/c"]
    assert platform.command_argv([str(tmp_path / "check.ps1")], tmp_path)[:1] == ["powershell.exe"]
    # a string hook is PowerShell's -Command, so cmd.exe never runs it either
    argv = platform.shell_argv("exit 3")
    assert argv[0] == "powershell.exe" and "-Command" in argv and argv[-1].endswith("exit 3")


class _FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, data):
        self.data = data

    def OpenKey(self, hive, path):
        return _FakeKey(self.data[(hive, path)])

    def QueryInfoKey(self, key):
        return (0, len(key.values), 0)

    def EnumValue(self, key, index):
        return key.values[index]


def test_hot_environment_is_empty_off_windows(monkeypatch):
    monkeypatch.setattr(platform, "IS_WINDOWS", False)
    assert platform.hot_environment() == {}


def test_hot_environment_reads_both_hives_and_expands(monkeypatch):
    import sys
    import types

    machine = [
        ("WG_ROOT", r"C:\base", 1),
        ("WG_BIN", r"%WG_ROOT%\bin", 2),
        ("WG_PROC", r"%PROC_ROOT%\x", 2),
        ("PATH", r"C:\machine", 1),
    ]
    user = [
        ("RELAY_PASSWORD", "s3cret", 1),
        ("Path", r"C:\user", 1),
        ("WG_USER", r"%WG_ROOT%\user", 2),
    ]
    fake = _FakeWinreg({
        (1, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"): machine,
        (2, "Environment"): user,
    })
    fake_module = types.ModuleType("winreg")
    for name, value in {
        "HKEY_LOCAL_MACHINE": 1,
        "HKEY_CURRENT_USER": 2,
        "REG_SZ": 1,
        "REG_EXPAND_SZ": 2,
        "OpenKey": fake.OpenKey,
        "QueryInfoKey": fake.QueryInfoKey,
        "EnumValue": fake.EnumValue,
    }.items():
        setattr(fake_module, name, value)
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    monkeypatch.setenv("PROC_ROOT", r"C:\process")
    monkeypatch.setitem(sys.modules, "winreg", fake_module)

    env = platform.hot_environment()

    assert env["WG_BIN"] == r"C:\base\bin"
    assert env["WG_USER"] == r"C:\base\user"
    assert env["WG_PROC"] == r"C:\process\x"  # process vars expand too
    assert env["RELAY_PASSWORD"] == "s3cret"
    assert env["Path"] == r"C:\machine;C:\user" and "PATH" not in env  # Windows combines the halves


@pytest.mark.windows_only
def test_hot_environment_reads_the_real_registry():
    env = platform.hot_environment()
    assert env
    assert any(name.lower() == "path" for name in env)
