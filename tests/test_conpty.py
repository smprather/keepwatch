"""`password_mode = "conpty"`: typing a password at a Windows pseudo-console (issue 6).

pywinpty is a Windows-only optional extra, so every test here works with a fake `winpty` module in
`sys.modules`: the pty's behaviour (a prompt, an echo, an exit, a hang) is what matters, not its implementation.
"""

import sys
import types

import pytest

from keepwatch import conpty

PROMPT = "u@host's password: "


class FakePty:
    """A pty that hands out scripted output, remembers what was typed, and dies when the script runs out."""

    def __init__(self, *script, exitstatus=0):
        self.script = list(script)
        self.typed: list[str] = []
        self.exitstatus = exitstatus
        self.alive = True
        self.terminated = False
        self.closed = False

    def read(self, size):
        if self.script:
            return self.script.pop(0)
        if self.terminated:  # killed: nothing more will come
            raise EOFError
        self.alive = False  # a real pty ends when its command finishes
        raise EOFError

    def write(self, data):
        self.typed.append(data)

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.terminated = True
        self.alive = False

    def cancel(self):
        self.closed = True


def fake_winpty(monkeypatch, process):
    module = types.ModuleType("winpty")
    setattr(module, "PtyProcess", types.SimpleNamespace(spawn=lambda argv, env=None, dimensions=None: process))
    monkeypatch.setitem(sys.modules, "winpty", module)
    return module


def test_the_password_is_typed_at_the_prompt(monkeypatch):
    process = FakePty(PROMPT)
    fake_winpty(monkeypatch, process)
    exit_code, output, stderr, timed_out = conpty.run(["scp", "a", "b"], "s3cret", limit=5, env={"PATH": "/bin"})
    assert process.typed == ["s3cret\r"]  # a carriage return, which is what Enter is on a console
    assert (exit_code, stderr, timed_out) == (0, "", False)
    assert "password" in output


def test_an_echoed_password_never_comes_back(monkeypatch):
    """A pty echoes what is written unless the program turns echo off; whatever happens, the value is scrubbed."""
    process = FakePty(PROMPT, "s3cret\r\nPermission denied\n", exitstatus=1)
    fake_winpty(monkeypatch, process)
    exit_code, output, _, _ = conpty.run(["scp"], "s3cret", limit=5)
    assert "s3cret" not in output
    assert "***" in output and "Permission denied" in output
    assert exit_code == 1


def test_a_pty_that_exits_without_a_prompt_is_reported(monkeypatch):
    """Nothing is typed at a server that never asks, and its output (the real diagnosis) comes back."""
    process = FakePty("Permission denied (publickey,password).\n", exitstatus=255)
    fake_winpty(monkeypatch, process)
    exit_code, output, _, timed_out = conpty.run(["scp"], "s3cret", limit=5)
    assert process.typed == []
    assert exit_code == 255 and not timed_out
    assert "Permission denied" in output


def test_a_hanging_pty_is_killed_at_the_limit(monkeypatch):
    process = FakePty("connecting...\n")
    process.alive = True
    process.read = lambda size: (_ for _ in ()).throw(EOFError)  # never a prompt, never an exit
    fake_winpty(monkeypatch, process)
    exit_code, _, _, timed_out = conpty.run(["scp"], "s3cret", limit=0.05)
    assert timed_out is True and exit_code is None
    assert process.terminated is True


def test_the_missing_extra_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "winpty", None)  # as if pywinpty were not installed
    with pytest.raises(ImportError, match=r"keepwatch\[conpty\]"):
        conpty.run(["scp"], "s3cret", limit=5)


def test_scrub_removes_the_password():
    assert conpty.scrub("pw: s3cret\n", "s3cret") == "pw: ***\n"
    assert conpty.scrub("nothing here", "") == "nothing here"
