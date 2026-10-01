"""offline.json: a watch stopped by the failure limit or by `keepwatch disable` (spec 8.3)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from keepwatch.paths import Paths, write_json_atomic


def iso_time(epoch: float) -> str:
    """Local time as RFC 3339 with offset and milliseconds."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class OfflineMarker:
    reason: str
    since: str
    by_user: bool = False
    last_failure: str | None = None

    def since_epoch(self) -> float:
        try:
            return datetime.fromisoformat(self.since).timestamp()
        except ValueError:
            return 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_offline(paths: Paths, watch: str) -> OfflineMarker | None:
    path = paths.offline_file(watch)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return OfflineMarker(
            reason=str(data["reason"]),
            since=str(data["since"]),
            by_user=bool(data.get("by_user", False)),
            last_failure=data.get("last_failure"),
        )
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        return OfflineMarker(
            reason=f"{path} is unreadable; run: keepwatch enable {watch}",
            since=iso_time(time.time()),
            by_user=True,
        )


def write_offline(paths: Paths, watch: str, marker: OfflineMarker) -> None:
    write_json_atomic(paths.offline_file(watch), marker.to_dict())


def clear_offline(paths: Paths, watch: str) -> bool:
    try:
        paths.offline_file(watch).unlink()
    except FileNotFoundError:
        return False
    return True
