"""One poll of one watch: check (or fake), state machine, actions, log records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from keepwatch.config import GlobalConfig, WatchConfig
from keepwatch.hooks import CHECK, NO_CHECK, resolve_hooks
from keepwatch.logstore import Sink, make_record
from keepwatch.paths import Paths
from keepwatch.runner import HookCall, HookResult, Runner
from keepwatch.state import Outcome, WatchState, finish_poll, plan_poll

_PLUGIN_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_FAILED = ("error", "failed", "timeout")
_COMMAND_FIELDS = (
    "argv",
    "shell",
    "exit_code",
    "timed_out",
    "duration",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
)


def parse_fakes(spec: str) -> list[Outcome]:
    """Parse "true,false,timeout" into outcomes."""
    valid = ", ".join(outcome.value for outcome in Outcome)
    outcomes = []
    for part in spec.split(","):
        word = part.strip()
        try:
            outcomes.append(Outcome(word.lower()))
        except ValueError:
            raise ValueError(f"'{word}' is not an outcome; use a comma-separated list of: {valid}") from None
    return outcomes


@dataclass(frozen=True)
class Fake:
    outcome: Outcome
    payload: Any = None


@dataclass
class PollReport:
    poll_id: str
    watch: str
    outcome: Outcome
    reason: str | None
    payload: Any
    before: WatchState
    after: WatchState
    planned: tuple[str, ...]
    results: list[HookResult] = field(default_factory=list)
    failed: bool = False
    faked: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "poll_id": self.poll_id,
            "watch": self.watch,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "payload": self.payload,
            "condition_before": self.before.condition,
            "condition_after": self.after.condition,
            "pending_edge": self.after.pending_edge.value if self.after.pending_edge else None,
            "failures": self.after.failures,
            "planned_actions": list(self.planned),
            "results": [{"hook": result.hook, "status": result.status, "reason": result.reason} for result in self.results],
            "failed": self.failed,
            "faked": self.faked,
            "dry_run": self.dry_run,
        }


def _edge(state: WatchState) -> str | None:
    return state.pending_edge.value if state.pending_edge else None


class PollEngine:
    def __init__(self, *, runner: Runner, paths: Paths, global_config: GlobalConfig, sink: Sink, pid: int) -> None:
        self._runner = runner
        self._paths = paths
        self._global = global_config
        self._sink = sink
        self._pid = pid

    def poll(
        self,
        watch: WatchConfig,
        state: WatchState,
        *,
        fake: Fake | None = None,
        dry_run: bool = False,
    ) -> PollReport:
        poll_id = uuid.uuid4().hex[:12]
        tag = {"watch": watch.name, "poll_id": poll_id}
        faked = fake is not None
        self._sink(make_record("poll.start", **tag, condition=state.condition, faked=faked, dry_run=dry_run))
        hooks, problem = resolve_hooks(watch.watch_dir, watch.hooks)
        if problem is None and fake is None and CHECK not in hooks:
            problem = NO_CHECK
        if problem is not None:
            outcome, reason, payload = Outcome.ERROR, problem, None
        elif fake is not None:
            outcome, reason, payload = fake.outcome, "faked with --fake", fake.payload
        else:
            result = self._run(watch, CHECK, poll_id, state.condition, None, watch.check_timeout)
            outcome, reason, payload = result.outcome(), result.reason, result.payload
        plan = plan_poll(state, outcome, hooks)
        answered = outcome in (Outcome.TRUE, Outcome.FALSE, Outcome.UNKNOWN)
        self._sink(
            make_record(
                "check.outcome",
                level="INFO" if answered else "ERROR",
                **tag,
                outcome=outcome.value,
                reason=reason,
                payload=payload,
                condition_before=state.condition,
                condition_after=plan.state.condition,
                pending_edge=_edge(plan.state),
                actions=list(plan.actions),
                faked=faked,
            )
        )
        results: list[HookResult] = []
        if dry_run:
            after, failed = finish_poll(plan, [(hook, True) for hook in plan.actions])
        else:
            for hook in plan.actions:
                result = self._run(watch, hook, poll_id, plan.state.condition, payload, watch.action_timeout)
                results.append(result)
                if not result.succeeded:
                    break
            after, failed = finish_poll(plan, [(result.hook, result.succeeded) for result in results])
        self._sink(
            make_record(
                "poll.end",
                level="WARNING" if failed else "INFO",
                **tag,
                failed=failed,
                failures=after.failures,
                condition=after.condition,
                pending_edge=_edge(after),
                dry_run=dry_run,
            )
        )
        return PollReport(
            poll_id=poll_id,
            watch=watch.name,
            outcome=outcome,
            reason=reason,
            payload=payload,
            before=state,
            after=after,
            planned=plan.actions,
            results=results,
            failed=failed,
            faked=faked,
            dry_run=dry_run,
        )

    def _run(
        self,
        watch: WatchConfig,
        hook: str,
        poll_id: str,
        condition: bool,
        payload: Any,
        timeout: float,
    ) -> HookResult:
        call = HookCall(
            watch=watch,
            hook=hook,
            poll_id=poll_id,
            condition=condition,
            payload=payload,
            data_dir=self._paths.watch_data_dir(watch.name),
            run_dir=self._paths.run_dir(self._pid, watch.name),
            timeout=timeout,
            capture_bytes=self._global.log.capture_bytes,
            environment=self._global.environment,
        )
        result = self._runner.run(call)
        tag = {"watch": watch.name, "poll_id": poll_id, "hook": hook}
        for message in result.messages:
            if message.get("type") == "command":
                fields = {key: message.get(key) for key in _COMMAND_FIELDS}
                level = "INFO" if message.get("exit_code") == 0 else "WARNING"
                self._sink(make_record("command", level=level, **tag, **fields))
            else:
                level = message.get("level") if message.get("level") in _PLUGIN_LEVELS else "INFO"
                self._sink(
                    make_record(
                        "plugin.log",
                        level=level,
                        **tag,
                        logger=message.get("logger"),
                        message=message.get("message"),
                        fields=message.get("fields") or {},
                        traceback=message.get("traceback"),
                    )
                )
        level = "ERROR" if result.status in _FAILED else "INFO"
        self._sink(make_record("hook.end", level=level, watch=watch.name, poll_id=poll_id, **result.to_record()))
        return result
