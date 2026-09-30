"""Parse and format durations such as "30s", "15m", "1h30m" or a number of seconds."""

from __future__ import annotations

import re

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_WHOLE = re.compile(r"(?:\d+[dhms])+")
_PART = re.compile(r"(\d+)([dhms])")


class DurationError(ValueError):
    """Raised when a value is not a valid duration."""


def parse_duration(value: object) -> float:
    """Return a duration in seconds.

    Accepts a non-negative int or float (seconds), or a string of one or more
    integer+unit pairs: "30s", "15m", "1h30m", "90d". Units: s, m, h, d.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DurationError(f'expected a duration like "30s", got {value!r}')
    if isinstance(value, (int, float)):
        if value < 0:
            raise DurationError(f"duration must not be negative, got {value!r}")
        return float(value)
    text = value.strip().lower()
    if text.isdigit():
        raise DurationError(
            f'duration {value!r} needs a unit: "{text}s" for seconds (units: s, m, h, d)'
        )
    if not _WHOLE.fullmatch(text):
        raise DurationError(
            f'expected a duration like "30s", "15m" or "1h30m" (units: s, m, h, d), got {value!r}'
        )
    return float(sum(int(count) * _UNIT_SECONDS[unit] for count, unit in _PART.findall(text)))


def format_duration(seconds: float) -> str:
    """Format seconds compactly: 90 -> "1m30s", 3600 -> "1h", 0.5 -> "0.5s"."""
    if seconds != int(seconds):
        return f"{seconds:g}s"
    remaining = int(seconds)
    if remaining == 0:
        return "0s"
    parts = []
    for unit in ("d", "h", "m", "s"):
        count, remaining = divmod(remaining, _UNIT_SECONDS[unit])
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)
