import pytest

from keepwatch.state import (
    Edge,
    Outcome,
    WatchState,
    finish_poll,
    initial_state,
    next_delay,
    plan_poll,
    should_go_offline,
)

ALL = {"on_rise", "on_fall", "on_true", "on_false"}
T, F = Outcome.TRUE, Outcome.FALSE


@pytest.mark.parametrize(
    "before, outcome, condition, pending, actions",
    [
        (WatchState(False), T, True, Edge.RISE, ("on_rise", "on_true")),
        (WatchState(True), T, True, None, ("on_true",)),
        (WatchState(False), F, False, None, ("on_false",)),
        (WatchState(True), F, False, Edge.FALL, ("on_fall", "on_false")),
        (WatchState(True, Edge.RISE), T, True, Edge.RISE, ("on_rise", "on_true")),
        (WatchState(True, Edge.RISE), F, False, Edge.FALL, ("on_fall", "on_false")),
    ],
)
def test_answers(before, outcome, condition, pending, actions):
    plan = plan_poll(before, outcome, ALL)
    assert plan.state.condition is condition
    assert plan.state.pending_edge == pending
    assert plan.actions == actions
    assert plan.check_failed is False


@pytest.mark.parametrize("outcome, failed", [(Outcome.UNKNOWN, False), (Outcome.TIMEOUT, True), (Outcome.ERROR, True)])
def test_non_answers_hold_the_condition(outcome, failed):
    before = WatchState(True, Edge.RISE, 2)
    plan = plan_poll(before, outcome, ALL)
    assert plan.state == before
    assert plan.actions == ()
    assert plan.check_failed is failed


def test_undefined_hooks_are_skipped_and_edge_cleared():
    plan = plan_poll(WatchState(False), T, {"on_true"})
    assert plan.actions == ("on_true",)
    assert plan.state.pending_edge is None


def test_successful_edge_clears_pending_and_resets_failures():
    plan = plan_poll(WatchState(False, None, 3), T, ALL)
    state, failed = finish_poll(plan, [("on_rise", True), ("on_true", True)])
    assert state == WatchState(True, None, 0)
    assert failed is False


def test_failed_edge_stays_pending_and_counts():
    plan = plan_poll(WatchState(False), T, ALL)
    state, failed = finish_poll(plan, [("on_rise", False)])
    assert state == WatchState(True, Edge.RISE, 1)
    assert failed is True


def test_check_failure_counts():
    plan = plan_poll(WatchState(False, None, 1), Outcome.ERROR, ALL)
    state, failed = finish_poll(plan, [])
    assert state.failures == 2 and failed is True


def test_unknown_leaves_failures_unchanged():
    plan = plan_poll(WatchState(False, None, 2), Outcome.UNKNOWN, ALL)
    state, failed = finish_poll(plan, [])
    assert state.failures == 2 and failed is False


def test_initial_state():
    assert initial_state(True) == WatchState(True, None, 0)


@pytest.mark.parametrize(
    "interval, failures, delay",
    [
        (30, 0, 30),
        (30, 1, 60),
        (30, 2, 120),
        (30, 3, 240),
        (30, 4, 480),
        (600, 1, 600),
        (600, 2, 1200),
        (30, 10, 3600),
        (7200, 3, 7200),
    ],
)
def test_next_delay(interval, failures, delay):
    assert next_delay(interval, failures) == delay


def test_should_go_offline():
    assert should_go_offline(5, 5) is True
    assert should_go_offline(4, 5) is False
    assert should_go_offline(100, 0) is False
