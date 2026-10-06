"""Built-in recipes: hooks keepwatch provides for `recipe = "<name>"` watches (see `keepwatch docs recipes`).

Each module defines check(ctx) and on_true(ctx) and runs in the Python worker, so it uses only the
standard library and keepwatch's stdlib-only modules. Its settings are validated by keepwatch.config.
"""

from __future__ import annotations


def file_budget(remaining: float, files_left: int, limit: float) -> float:
    """Seconds one file may take: an equal share of what is left of the action, capped by `limit` when set.

    A share, not the whole budget, so one slow file cannot starve the rest of the queue; the last file gets
    whatever is left. `limit` is the recipe's `file_timeout` (0 or less: no extra cap).
    """
    share = remaining / max(files_left, 1)
    return share if limit <= 0 else min(share, limit)
