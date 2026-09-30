import re

import pytest

from keepwatch.durations import DurationError, format_duration, parse_duration


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("30s", 30),
        ("15m", 900),
        ("1h", 3600),
        ("90d", 90 * 86400),
        ("1h30m", 5400),
        (" 2M ", 120),
        (45, 45),
        (1.5, 1.5),
        (0, 0),
    ],
)
def test_parse_duration_accepts(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("30", 'needs a unit: "30s"'),
        ("", "expected a duration"),
        ("1x", "expected a duration"),
        ("m5", "expected a duration"),
        ("1.5h", "expected a duration"),
        (-1, "must not be negative"),
        (True, "expected a duration"),
        (None, "expected a duration"),
    ],
)
def test_parse_duration_rejects(value, fragment):
    with pytest.raises(DurationError, match=re.escape(fragment)):
        parse_duration(value)


@pytest.mark.parametrize(
    "seconds, text",
    [(90, "1m30s"), (3600, "1h"), (0, "0s"), (0.5, "0.5s"), (86401, "1d1s")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
