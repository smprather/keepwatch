"""watchglass's terminal monitor (see `watchglass docs tui`). Importing this package imports Textual.

`cli.py` imports it inside the `tui` command only, so no other command (and no hook) pays for Textual.
"""

from __future__ import annotations

from pathlib import Path

from watchglass.paths import Paths
from watchglass.tui.app import WatchglassApp
from watchglass.tui.monitor import Monitor

__all__ = ["run"]


def run(paths: Paths, config_path: Path) -> None:
    """Run the monitor until the user quits. Read-only: nothing here writes to watchglass's state."""
    monitor = Monitor(paths, config_path)
    monitor.start()
    try:
        WatchglassApp(monitor).run()
    finally:
        monitor.stop()
