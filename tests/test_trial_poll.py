"""The trial-poll accordion (issue 9): fast after a blip, quiet during a long outage."""

import pytest

from keepwatch.config import ConfigError, load_watch_config
from keepwatch.scheduler import trial_delay


def test_the_accordion_doubles_up_to_retry_after():
    assert trial_delay(600.0, 60.0, 1) == 60.0
    assert trial_delay(600.0, 60.0, 2) == 120.0
    assert trial_delay(600.0, 60.0, 3) == 240.0
    assert trial_delay(600.0, 60.0, 4) == 480.0
    assert trial_delay(600.0, 60.0, 5) == 600.0  # the cap: retry_after
    assert trial_delay(600.0, 60.0, 99) == 600.0


def test_a_successful_poll_resets_the_accordion():
    assert trial_delay(600.0, 60.0, 0) == 60.0  # the failure count is 0 again: start over at the base


def test_without_a_base_the_cadence_is_what_it_was():
    assert trial_delay(600.0, None, 1) == 600.0
    assert trial_delay(600.0, None, 7) == 600.0


def test_a_base_above_the_cap_never_widens_it():
    assert trial_delay(60.0, 600.0, 1) == 60.0


def test_retry_base_is_a_watch_setting(xdg, make_watch):
    watch_dir = make_watch("paced", config='retry_after = "10m"\nretry_base = "1m"\n[hooks]\ncheck = ["true"]\n')
    watch = load_watch_config(watch_dir)
    assert watch.retry_after == 600.0 and watch.retry_base == 60.0
    plain = load_watch_config(make_watch("plain", config='[hooks]\ncheck = ["true"]\n'))
    assert plain.retry_after is None and plain.retry_base is None


def test_a_push_recipe_that_resumes_needs_sftp_and_a_marker(xdg, make_watch):
    base = 'recipe = "push"\n[settings]\nlocal_dir = "."\ndest = "u@host:/in"\n'
    with pytest.raises(ConfigError, match="protocol"):
        load_watch_config(make_watch("resume-scp", config=base + "resume = true\n"))
    with pytest.raises(ConfigError, match="marker"):
        load_watch_config(make_watch("resume-none", config=base + 'protocol = "sftp"\nresume = true\nmarker = "none"\n'))
    watch = load_watch_config(make_watch("resume-sftp", config=base + 'protocol = "sftp"\nresume = true\n'))
    assert watch.settings["resume"] is True
