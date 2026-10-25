"""The systemd user service that starts watchglass at login (spec section 15)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from watchglass.paths import Paths

UNIT_NAME = "watchglass.service"


def unit_dir(paths: Paths) -> Path:
    return paths.config_home.parent / "systemd" / "user"


def find_executable() -> str:
    """Absolute path of the watchglass command that is running now (else the one on PATH)."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "watchglass" and argv0.is_file():
        return os.path.abspath(argv0)
    found = shutil.which("watchglass")
    if found:
        return os.path.abspath(found)
    return os.path.abspath(sys.argv[0])


def _exec_arg(value: str) -> str:
    """`value` as one quoted ExecStart argument.

    systemd expands `%` specifiers and `$` variables inside ExecStart, and a `"` would end the quoting, so a
    path holding any of them silently corrupts the unit (and the failure only shows at the next logon).
    """
    quoted = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{quoted}"'


def _env_value(value: str) -> str:
    """`value` inside Environment="NAME=value": `%` expands there too, `$` does not."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def unit_text(executable: str, path_env: str, config_path: Path | None = None) -> str:
    exec_args = [_exec_arg(executable)]
    if config_path is not None:
        exec_args += ["--config", _exec_arg(str(config_path))]
    exec_args.append("run")
    return (
        "[Unit]\n"
        "Description=watchglass: poll conditions and run actions\n"
        "Documentation=https://github.com/smprather/watchglass\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(exec_args)}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f'Environment="PATH={_env_value(path_env)}"\n'
        'Environment="PYTHONUNBUFFERED=1"\n'
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `systemctl --user ...` ($WATCHGLASS_SYSTEMCTL replaces systemctl, for tests)."""
    program = os.environ.get("WATCHGLASS_SYSTEMCTL", "systemctl")
    return subprocess.run([program, "--user", *args], capture_output=True, text=True, errors="replace")
