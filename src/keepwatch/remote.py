"""Running keepwatch's remote helper on another host over ssh. Stdlib only.

The `remote_files` observer and the `pull` recipe (which runs in the Python worker, where watchdog is not
available) both build their ssh command lines here.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

AUTO_PYTHON = (
    "sh -c 'if [ -x /usr/bin/python3 ]; then exec /usr/bin/python3 \"$@\"; else exec python3 \"$@\"; fi' sh -u -"
)
SSH_DEFAULTS = (
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    # No connection sharing: a ControlPersist master would outlive the ssh keepwatch stops and hold its pipes.
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)


def ssh_argv(
    remote: str,
    *,
    port: int | None = None,
    identity: str | os.PathLike[str] | None = None,
    ssh_options: Sequence[str] = (),
    ssh_command: Sequence[str] | None = None,
    remote_python: str = "auto",
) -> list[str]:
    """The ssh command line that runs the remote helper (its source comes on stdin). User ssh_options come first."""
    argv = [*(ssh_command or ("ssh",)), *ssh_options, *SSH_DEFAULTS]
    if port is not None:
        argv += ["-p", str(port)]
    if identity is not None:
        argv += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    python = AUTO_PYTHON if remote_python == "auto" else f"{shlex.quote(remote_python)} -u -"
    return [*argv, "--", remote, python]


def watcher_source(options: Mapping[str, Any]) -> str:
    """The remote helper's source with its options prepended as an assignment (ASCII: json escapes the rest)."""
    source = resources.files("keepwatch").joinpath("remote_watcher.py").read_text(encoding="utf-8")
    return f"KEEPWATCH_REMOTE_ARGS = {json.dumps(json.dumps(options))}\n{source}"
