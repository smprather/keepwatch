import os
import subprocess
import sys
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
