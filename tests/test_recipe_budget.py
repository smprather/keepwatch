"""The recipes' per-file budget: one slow file must not starve the rest of the queue (issue 5)."""

from keepwatch.recipes import file_budget


def test_a_file_gets_an_equal_share_of_what_is_left():
    assert file_budget(600.0, 3, 0.0) == 200.0
    assert file_budget(600.0, 2, 0.0) == 300.0
    assert file_budget(600.0, 1, 0.0) == 600.0  # the last file gets the remainder
    assert file_budget(10.0, 0, 0.0) == 10.0  # never divides by zero


def test_file_timeout_caps_the_share():
    assert file_budget(600.0, 3, 120.0) == 120.0
    assert file_budget(600.0, 3, 900.0) == 200.0  # a cap above the share changes nothing
    assert file_budget(600.0, 3, -1.0) == 200.0  # 0 or less: no extra cap


def test_nothing_is_left_when_the_budget_is_gone():
    assert file_budget(0.0, 3, 0.0) == 0.0
