"""The systemd user service that starts keepwatch at login (spec section 15)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from keepwatch.paths import Paths

UNIT_NAME = "keepwatch.service"


def unit_dir(paths: Paths) -> Path:
    return paths.config_home.parent / "systemd" / "user"


def find_executable() -> str:
    """Absolute path of the keepwatch command that is running now."""
    found = shutil.which("keepwatch")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def unit_text(executable: str, path_env: str) -> str:
    return (
        "[Unit]\n"
        "Description=keepwatch: poll conditions and run actions\n"
        "Documentation=https://github.com/smprather/keepwatch\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'ExecStart="{executable}" run\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f'Environment="PATH={path_env}"\n'
        'Environment="PYTHONUNBUFFERED=1"\n'
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `systemctl --user ...` ($KEEPWATCH_SYSTEMCTL replaces systemctl, for tests)."""
    program = os.environ.get("KEEPWATCH_SYSTEMCTL", "systemctl")
    return subprocess.run([program, "--user", *args], capture_output=True, text=True, errors="replace")
