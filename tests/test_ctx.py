import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from keepwatch import CommandFailed, Ctx, Ledger, LedgerCorrupt, LedgerReadOnly


def make_ctx(tmp_path, hook="on_true", emitted=None, deadline_in=30.0):
    watch_dir = tmp_path / "w"
    watch_dir.mkdir(exist_ok=True)
    return Ctx(
        watch="w",
        hook=hook,
        poll_id="p1",
        condition=True,
        payload=None,
        settings={"dest": "x"},
        watch_dir=watch_dir,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        deadline=time.time() + deadline_in,
        capture_bytes=1000,
        emit=(emitted.append if emitted is not None else None),
    )


def test_ledger_persists_and_writes_atomically(tmp_path):
    path = tmp_path / "ledgers" / "sent.json"
    ledger = Ledger(path)
    ledger.add("a")
    ledger.add("b")
    ledger.discard("a")
    reopened = Ledger(path)
    assert "b" in reopened and "a" not in reopened
    assert list(reopened) == ["b"] and len(reopened) == 1
    assert reopened.added_at("b") is not None
    assert [p.name for p in path.parent.iterdir()] == ["sent.json"]


def test_ledger_expiry(tmp_path, monkeypatch):
    path = tmp_path / "sent.json"
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ledger = Ledger(path, expire=60)
    ledger.add("old")
    now[0] += 61
    assert "old" not in ledger
    ledger.add("new")
    assert '"old"' not in path.read_text()


def test_ledger_read_only_and_key_type(tmp_path):
    ledger = Ledger(tmp_path / "x.json", writable=False)
    with pytest.raises(LedgerReadOnly):
        ledger.add("k")
    with pytest.raises(TypeError):
        Ledger(tmp_path / "y.json").add(Path("/k"))


def test_ctx_ledger_is_read_only_in_check(tmp_path):
    ctx = make_ctx(tmp_path, hook="check")
    with pytest.raises(LedgerReadOnly):
        ctx.ledger("sent").add("k")
    make_ctx(tmp_path, hook="on_true").ledger("sent", expire="90d").add("k")
    assert "k" in make_ctx(tmp_path, hook="check").ledger("sent")
    with pytest.raises(ValueError):
        make_ctx(tmp_path).ledger("../escape")


def test_file_key_changes_with_mtime(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "f.tar.gz"
    target.write_text("data")
    first = ctx.file_key(target)
    os.utime(target, (1, 1))
    assert ctx.file_key(target) != first
    assert first.startswith(f"{target}|4|")


def test_glob_expands_home_and_sorts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("b.tar.gz", "a.tar.gz", "c.txt"):
        (tmp_path / name).write_text("")
    assert make_ctx(tmp_path).glob("~/*.tar.gz") == [tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"]


def test_unchanged_for(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "f"
    target.write_text("x")
    assert ctx.unchanged_for(target, "30s") is False
    old = time.time() - 60
    os.utime(target, (old, old))
    assert ctx.unchanged_for(target, "30s") is True
    assert ctx.unchanged_for(tmp_path / "missing", "30s") is False


def test_run_captures_and_reports(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted)
    completed = ctx.run(["sh", "-c", "echo out; echo err >&2"])
    assert completed.stdout == "out\n"
    (record,) = emitted
    assert record["type"] == "command"
    assert record["exit_code"] == 0 and record["stdout"] == "out\n" and record["stderr"] == "err\n"
    assert record["timed_out"] is False and record["shell"] is False


def test_run_raises_on_failure_with_stderr_tail(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(CommandFailed, match="exited 3.*nope"):
        ctx.run("echo nope >&2; exit 3")
    assert ctx.run("exit 3", check=False).returncode == 3


def test_run_stdin_is_empty_and_input_works(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.run(["cat"]).stdout == ""
    assert ctx.run(["cat"], input="hi").stdout == "hi"


def test_run_times_out_at_the_hook_deadline(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted, deadline_in=0.5)
    with pytest.raises(subprocess.TimeoutExpired):
        ctx.run(["sleep", "10"])
    assert emitted[0]["timed_out"] is True


def test_dirs_created_on_access(tmp_path):
    ctx = make_ctx(tmp_path)
    assert not (tmp_path / "data").exists()
    assert ctx.data_dir.is_dir() and ctx.run_dir.is_dir()


@pytest.mark.parametrize("content", ["{not json", '{"entries": {"k": "soon"}}', "[]"])
def test_corrupt_ledger_names_the_file(tmp_path, content):
    path = tmp_path / "sent.json"
    path.write_text(content)
    with pytest.raises(LedgerCorrupt, match=re.escape(str(path))):
        Ledger(path)


def test_ledger_lookup_does_not_scan_the_whole_ledger(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "x.json", expire=3600)
    ledger.add("k")

    def scanned(self):
        raise AssertionError("the whole ledger was scanned")

    monkeypatch.setattr(Ledger, "_live", scanned)
    assert "k" in ledger
    assert "other" not in ledger
    assert ledger.added_at("k") is not None
    assert ledger.added_at("other") is None


def test_run_decodes_non_utf8_output(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted)
    assert ctx.run(["printf", "\\377ok"]).stdout == "\ufffdok"
    assert emitted[0]["stdout"] == "\ufffdok"
