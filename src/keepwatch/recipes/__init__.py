"""Built-in recipes: hooks keepwatch provides for `recipe = "<name>"` watches (see `keepwatch docs recipes`).

Each module defines check(ctx) and on_true(ctx) and runs in the Python worker, so it uses only the
standard library and keepwatch's stdlib-only modules. Its settings are validated by keepwatch.config.
"""
