"""The mirror recipe's pure parts: the walk, the diff against the checkpoint, and the plan it produces."""

import os
import time

import pytest

from keepwatch.recipes import mirror


def test_plan_push_copies_new_and_changed_and_creates_directories():
    plan = mirror.plan_push({"a/b.txt": [2, 10.0]}, {"a"}, {}, set())
    assert plan.copy == ("a/b.txt",)
    assert plan.mkdir == ("a",)
    assert plan.deleted == ()


def test_a_difference_within_tolerance_is_not_a_change():
    assert mirror.plan_push({"a.txt": [2, 10.0]}, set(), {"a.txt": [2, 11.0]}, set()).copy == ()


def test_a_change_beyond_tolerance_is_a_copy():
    assert mirror.plan_push({"a.txt": [2, 20.0]}, set(), {"a.txt": [2, 10.0]}, set()).copy == ("a.txt",)


def test_a_size_change_is_a_copy():
    assert mirror.plan_push({"a.txt": [3, 10.0]}, set(), {"a.txt": [2, 10.0]}, set()).copy == ("a.txt",)


def test_a_path_the_walk_did_not_see_is_deleted():
    assert mirror.plan_push({}, set(), {"a.txt": [2, 10.0]}, set()).deleted == ("a.txt",)


def test_deleted_directories_are_reported_deepest_first():
    assert mirror.plan_push({}, set(), {}, {"a", "a/b"}).deleted == ("a/b", "a")


def test_mkdir_comes_in_depth_order():
    assert mirror.plan_push({"a/b/c.txt": [1, 1.0]}, {"a", "a/b"}, {}, set()).mkdir == ("a", "a/b")


def test_a_plan_survives_the_payload_round_trip():
    plan = mirror.Plan(mkdir=("a",), copy=("a/b",), deleted=("c",))
    assert mirror.Plan.from_payload(plan.as_dict()) == plan


def test_the_checkpoint_document_round_trips():
    document = mirror.as_document({"a": [1, 2.0]}, {"b"})
    assert document == {"version": 1, "files": {"a": [1, 2.0]}, "dirs": ["b"]}
    assert mirror.files_of(document) == {"a": [1, 2.0]}
    assert mirror.dirs_of(document) == ["b"]


def test_plan_pull_reads_the_watchers_events():
    events = [
        {"event": "files", "entries": [["/r/a.txt", 2, 10.0]]},
        {"event": "dirs", "paths": ["/r/sub"]},
    ]
    plan = mirror.plan_pull(events, {}, set(), remote_dir="/r")
    assert plan.copy == ("a.txt",)
    assert plan.mkdir == ("sub",)


def test_plan_pull_skips_what_the_checkpoint_already_has():
    events = [{"event": "files", "entries": [["/r/a.txt", 2, 10.0]]}]
    assert mirror.plan_pull(events, {"a.txt": [2, 10.0]}, set(), remote_dir="/r").copy == ()


def test_plan_pull_reads_a_gone_path():
    events = [{"event": "gone", "paths": ["/r/b.txt"]}]
    plan = mirror.plan_pull(events, {"b.txt": [2, 10.0]}, set(), remote_dir="/r")
    assert plan.deleted == ("b.txt",)


def test_no_events_never_deletes():
    """A quiet stream means \"nothing to do\", never \"everything was deleted\" (spec 5.6)."""
    assert mirror.plan_pull([], {"b.txt": [2, 10.0]}, {"sub"}, remote_dir="/r").deleted == ()


def test_plan_pull_ignores_events_it_does_not_understand():
    events = [{"event": "heartbeat"}, {"event": "files", "entries": [["/r/a", 1, 1.0]]}]
    assert mirror.plan_pull(events, {}, set(), remote_dir="/r").copy == ("a",)


def test_walk_local_is_recursive_and_reports_relative_posix_paths(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"x")
    files, dirs, problems = mirror.walk_local(tmp_path, (), 0.0)
    assert sorted(files) == ["b.txt", "sub/a.txt"]
    assert files["sub/a.txt"][0] == 5
    assert dirs == {"sub"} and problems == []


def test_walk_local_prunes_ignored_names(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"x")
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "a.txt").write_bytes(b"x")
    (tmp_path / "skip.tmp").write_bytes(b"x")
    files, dirs, _ = mirror.walk_local(tmp_path, (".git", "*.tmp"), 0.0)
    assert sorted(files) == ["keep/a.txt"]
    assert dirs == {"keep"}


def test_walk_local_skips_a_file_still_being_written(tmp_path):
    (tmp_path / "fresh.txt").write_bytes(b"x")
    files, _, _ = mirror.walk_local(tmp_path, (), 60.0, now=time.time())
    assert files == {}


def test_walk_local_reports_an_unreadable_subtree(tmp_path):
    """A subtree that cannot be read is not an absence (spec 5.4): it must be a problem, not a deletion."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "inside.txt").write_bytes(b"x")
    (tmp_path / "ok.txt").write_bytes(b"x")
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs a POSIX user that is not root")
    os.chmod(locked, 0)
    try:
        files, _, problems = mirror.walk_local(tmp_path, (), 0.0)
    finally:
        os.chmod(locked, 0o700)
    assert "ok.txt" in files
    assert problems and "locked" in problems[0]
