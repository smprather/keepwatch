"""Hook names, and discovery of Python hooks in watch.py without importing it. Stdlib only."""

from __future__ import annotations

import ast
from collections.abc import Collection
from pathlib import Path

CHECK = "check"
ACTION_HOOKS = ("on_rise", "on_fall", "on_true", "on_false")
HOOK_NAMES = (CHECK, *ACTION_HOOKS)
WATCH_PY = "watch.py"
NO_CHECK = "the watch has no check: define check(ctx) in watch.py or set check in [hooks] of config.toml"


def discover_python_hooks(watch_dir: Path) -> frozenset[str]:
    """Names of hook functions defined at the top level of watch.py.

    Returns an empty set when there is no watch.py. Raises SyntaxError or
    ValueError when watch.py cannot be parsed.
    """
    source = watch_dir / WATCH_PY
    if not source.is_file():
        return frozenset()
    tree = ast.parse(source.read_bytes(), filename=str(source))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HOOK_NAMES
    )


def resolve_hooks(watch_dir: Path, command_hooks: Collection[str]) -> tuple[frozenset[str], str | None]:
    """All hooks a watch defines, plus a problem message if its definition is broken."""
    commands = frozenset(command_hooks)
    try:
        python = discover_python_hooks(watch_dir)
    except SyntaxError as exc:
        return commands, f"{WATCH_PY} line {exc.lineno}: syntax error: {exc.msg}"
    except ValueError as exc:
        return commands, f"{WATCH_PY} cannot be parsed: {exc}"
    both = python & commands
    if both:
        names = ", ".join(sorted(both))
        return python | commands, f"defined both in watch.py and in [hooks] of config.toml: {names} (keep one)"
    return python | commands, None


RECIPE_HOOKS = {"pull": frozenset({CHECK, "on_true"}), "push": frozenset({CHECK, "on_true"})}


def watch_hooks(watch_dir: Path, command_hooks: Collection[str], recipe: str | None) -> tuple[frozenset[str], str | None]:
    """The hooks a watch has: its recipe's, or those of watch.py and [hooks] (with a problem message if broken)."""
    if recipe is not None:
        return RECIPE_HOOKS[recipe], None
    return resolve_hooks(watch_dir, command_hooks)
