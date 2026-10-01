"""Test commands that behave the same on every OS: they run the current Python interpreter."""

import sys
import textwrap
from pathlib import Path

PY = Path(sys.executable).as_posix()


def literal(text: str) -> str:
    """A TOML literal string (no escape processing). `text` must not contain a single quote."""
    assert "'" not in text, text
    return f"'{text}'"


def toml_path(path) -> str:
    """A filesystem path as a TOML literal string; forward slashes work on Windows too."""
    return literal(Path(path).as_posix())


def py(code: str) -> str:
    """A TOML list hook running `code` with the current Python. Use double quotes inside `code`."""
    return f"[{literal(PY)}, '-c', {literal(code)}]"


def exits(code: int) -> str:
    return py(f"import sys; sys.exit({code})")


TRUE = exits(0)


def script(body: str) -> str:
    """The text of a .py hook file: runs directly on POSIX (shebang) and through Python on Windows."""
    return f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip("\n")
