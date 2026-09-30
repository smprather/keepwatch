"""The per-watch state machine (spec section 7) and backoff (section 8.2). Pure: no I/O."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum


class Outcome(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    ERROR = "error"


ANSWERS = frozenset({Outcome.TRUE, Outcome.FALSE})
CHECK_FAILURES = frozenset({Outcome.TIMEOUT, Outcome.ERROR})


class Edge(StrEnum):
    RISE = "rise"
    FALL = "fall"


EDGE_HOOKS = {Edge.RISE: "on_rise", Edge.FALL: "on_fall"}
BACKOFF_BASE = 60.0
BACKOFF_CAP = 3600.0


@dataclass(frozen=True)
class WatchState:
    condition: bool
    pending_edge: Edge | None = None
    failures: int = 0


def initial_state(initial_condition: bool) -> WatchState:
    return WatchState(condition=initial_condition)


@dataclass(frozen=True)
class Plan:
    """What a poll will do: the state after the outcome, and the actions to run in order."""

    state: WatchState
    outcome: Outcome
    actions: tuple[str, ...]
    check_failed: bool


def plan_poll(state: WatchState, outcome: Outcome, defined_hooks: Collection[str]) -> Plan:
    """Apply a check outcome to the state and list the actions to run (section 7.1)."""
    if outcome not in ANSWERS:
        return Plan(state, outcome, (), outcome in CHECK_FAILURES)
    condition = outcome is Outcome.TRUE
    pending = state.pending_edge
    if condition != state.condition:
        pending = Edge.RISE if condition else Edge.FALL
    actions: list[str] = []
    if pending is not None:
        edge_hook = EDGE_HOOKS[pending]
        if edge_hook in defined_hooks:
            actions.append(edge_hook)
        else:
            pending = None
    level_hook = "on_true" if condition else "on_false"
    if level_hook in defined_hooks:
        actions.append(level_hook)
    return Plan(replace(state, condition=condition, pending_edge=pending), outcome, tuple(actions), False)


def finish_poll(plan: Plan, results: Sequence[tuple[str, bool]]) -> tuple[WatchState, bool]:
    """Fold action results (hook, succeeded) into the state. Returns (state, poll_failed)."""
    failed = plan.check_failed or any(not succeeded for _, succeeded in results)
    pending = plan.state.pending_edge
    if pending is not None and (EDGE_HOOKS[pending], True) in results:
        pending = None
    if failed:
        failures = plan.state.failures + 1
    elif plan.outcome in ANSWERS:
        failures = 0
    else:
        failures = plan.state.failures
    return replace(plan.state, pending_edge=pending, failures=failures), failed


def next_delay(interval: float, failures: int) -> float:
    """Seconds to wait before the next poll (section 8.2)."""
    if failures <= 0:
        return interval
    base = max(interval, BACKOFF_BASE)
    cap = max(interval, BACKOFF_CAP)
    return min(base * 2 ** (failures - 1), cap)


def should_go_offline(failures: int, max_failures: int) -> bool:
    return max_failures > 0 and failures >= max_failures
