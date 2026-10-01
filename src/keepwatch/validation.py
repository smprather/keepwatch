"""`keepwatch validate`: find every problem in watches before they run."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keepwatch.config import (
    Command,
    ConfigError,
    Discovery,
    GlobalConfig,
    WatchConfig,
    WatchNotFound,
    find_watch,
    load_watch_config,
)
from keepwatch.hooks import CHECK, NO_CHECK, WATCH_PY, discover_python_hooks, resolve_hooks
from keepwatch.paths import Paths
from keepwatch.runner import HookCall, Runner


@dataclass
class WatchCheck:
    name: str
    path: Path | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path) if self.path else None, "ok": self.ok, "problems": self.problems}


def missing_executable(command: Command, watch: WatchConfig, environment: Mapping[str, str]) -> str | None:
    """Why a command hook's program cannot run, or None. Shell strings are checked when they run."""
    if command.argv is None:
        return None
    program = command.argv[0]
    if "/" in program:
        path = Path(program) if os.path.isabs(program) else watch.watch_dir / program
        if not path.is_file():
            return f"{program} does not exist"
        if not os.access(path, os.X_OK):
            return f"{program} is not executable (run: chmod +x {path})"
        return None
    search = {**os.environ, **environment, **watch.environment}.get("PATH", "")
    return None if shutil.which(program, path=search) else f"'{program}' is not on PATH"


def check_watch(watch_dir: Path, global_config: GlobalConfig, runner: Runner, paths: Paths, pid: int) -> WatchCheck:
    report = WatchCheck(watch_dir.name, watch_dir)
    try:
        watch = load_watch_config(watch_dir, global_config.defaults)
    except ConfigError as exc:
        report.problems.extend(str(problem) for problem in exc.problems)
        return report
    hooks, problem = resolve_hooks(watch_dir, watch.hooks)
    source = watch_dir / WATCH_PY
    if problem is not None:
        report.problems.append(f"{source}: {problem}; see: keepwatch docs python")
    elif CHECK not in hooks:
        report.problems.append(f"{watch_dir}: {NO_CHECK}; see: keepwatch docs python")
    for hook, command in watch.hooks.items():
        missing = missing_executable(command, watch, global_config.environment)
        if missing is not None:
            report.problems.append(f"{watch.config_file}: [hooks] {hook}: {missing}; see: keepwatch docs executables")
    if watch.python_dependencies:
        report.problems.append(
            f"{watch.config_file}: python_dependencies is not supported by this version of keepwatch; "
            "see: keepwatch docs dependencies"
        )
    elif source.is_file() and problem is None:
        result = runner.run(
            HookCall(
                watch=watch,
                hook=CHECK,
                poll_id="validate",
                condition=watch.initial_condition,
                payload=None,
                data_dir=paths.watch_data_dir(watch.name),
                run_dir=paths.run_dir(pid, watch.name),
                timeout=watch.check_timeout,
                capture_bytes=global_config.log.capture_bytes,
                environment=global_config.environment,
                mode="describe",
            )
        )
        if result.status != "ok":
            report.problems.append(f"{source}: {result.reason}; see: keepwatch docs python")
        else:
            hidden = sorted(set(result.hooks or []) - discover_python_hooks(watch_dir))
            if hidden:
                report.problems.append(
                    f"{source}: {', '.join(hidden)} would never run: keepwatch only runs hooks written as "
                    "top-level 'def <hook>(ctx):' functions in watch.py, not imported, assigned, async or "
                    "nested ones; see: keepwatch docs python"
                )
    return report


def validate_watches(
    global_config: GlobalConfig,
    discovery: Discovery,
    names: list[str],
    runner: Runner,
    paths: Paths,
    pid: int,
) -> tuple[list[str], list[WatchCheck]]:
    general = [
        str(problem)
        for problem in discovery.problems
        if not names or problem.path.name in names
    ]
    results = []
    for name in names or list(discovery.watches):
        try:
            watch_dir = find_watch(discovery, name)
        except WatchNotFound as exc:
            results.append(WatchCheck(name, None, [str(exc)]))
            continue
        results.append(check_watch(watch_dir, global_config, runner, paths, pid))
    return general, results
