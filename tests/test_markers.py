import os

import pytest


@pytest.mark.posix_only
def test_posix_marker_runs_only_on_posix():
    assert os.name != "nt"


@pytest.mark.windows_only
def test_windows_marker_runs_only_on_windows():
    assert os.name == "nt"
