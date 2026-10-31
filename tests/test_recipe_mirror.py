"""The mirror recipe's push direction: batching, promotion, deletion safety, and one real scp pass.

The batching and promotion cases use a fake ctx that captures the ssh command lines and the pushes; the last
test drives a real poll through the engine against the test scp server, so a file really lands with its mtime.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from portable import toml_path
from scp_server import ScpServer
from watchglass.config import load_global_config, load_watch_config
from watchglass.ctx import Checkpoint, CommandFailed, Ctx, Unknown, checkpoint_file
from watchglass.pollengine import PollEngine
from watchglass.recipes import mirror
from watchglass.remote import ssh_run_argv
from watchglass.runner import Runner
from watchglass.state import WatchState
from watchglass.transfer import Transfer, TransferFailed

pytestmark = pytest.mark.skipif(shutil.which("scp") is None, reason="needs OpenSSH scp")
SSH_OPTIONS = ("-F", "/dev/null", "-o", "IdentityAgent=none")


class FakeTransfer(Transfer):
    """A real Transfer with its three calls replaced: it records them, and writes the bytes a pull returns."""

    def __init__(self, *, reachable=True, fail=()):
        super().__init__(base=Path("."))
        self.reachable = reachable
        self.fail = {str(name) for name in fail}
        self.pushes = []
        self.pulls = []
        self.uploads = []
        self.probes = []  # (host, port, timeout) of every probe the recipe made

    def tcp_open(self, host, port=22, timeout=5.0):
        self.probes.append((host, port, timeout))
        return self.reachable

    def push(self, path, remote_dir, *, marker="sha256", resume=False, **options):
        if Path(path).name in self.fail:
            raise TransferFailed(f"cannot push {Path(path).name}")
        self.pushes.append((str(path), remote_dir, {**options, "marker": marker, "resume": resume}))
        return f"{remote_dir}/{Path(path).name}"

    def upload(self, path, remote, *, resume=False, **options):
        if Path(path).name in self.fail:
            raise TransferFailed(f"cannot upload {Path(path).name}")
        self.uploads.append((str(path), remote, resume, options))

    def pull(self, remote, local_dir, *, size=None, sha256=None, on_conflict="skip-identical", resume=False, **options):
        name = str(remote).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if name in self.fail:
            raise TransferFailed(f"cannot pull {name}")
        recorded = {**options, "size": size, "sha256": sha256, "on_conflict": on_conflict, "resume": resume}
        self.pulls.append((str(remote), str(local_dir), recorded))
        target = Path(local_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"payload")  # a fresh mtime: the recipe must repair it from the event
        return target


class CountingCheckpoint:
    """A real Checkpoint that counts whole-document saves, for the promotion batching test."""

    def __init__(self, inner):
        self.inner = inner
        self.saves = 0

    @property
    def value(self):
        return self.inner.value

    def save(self, document):
        self.saves += 1
        self.inner.save(document)

    def clear(self):
        self.inner.clear()


class FakeCtx(Ctx):
    """A real Ctx, with the members the mirror uses replaced by recorders.

    Built without Ctx.__init__ (there is no watch or deadline to speak of here). The three properties are
    overridden rather than assigned: they are read-only on Ctx, and a subclass may shadow them where an instance
    could not set them.
    """

    def __init__(self, tmp_path, settings, *, reachable=True, fail=(), fail_commands=(), events=()):
        self.settings = settings
        self._transfer = FakeTransfer(reachable=reachable, fail=fail)
        self.events = list(events)
        self.payload = None
        self._left = 3600.0
        self.log = logging.getLogger("watchglass.test.mirror")
        self.watch_dir = tmp_path / "watch"
        self.watch_dir.mkdir(exist_ok=True)
        self._data = tmp_path / "data"
        self.commands = []  # (argv, input) pairs of every ctx.run
        self.fail_commands = tuple(fail_commands)  # command prefixes whose ssh run fails, as ctx.run(check=True) raises
        self.checkpoints = {}

    @property
    def transfer(self):
        return self._transfer

    @property
    def remaining(self):
        return self._left

    @property
    def data_dir(self):
        return self._data

    def checkpoint(self, name):
        if name not in self.checkpoints:
            self.checkpoints[name] = CountingCheckpoint(Checkpoint(checkpoint_file(self._data, name)))
        return self.checkpoints[name]

    def run(self, argv, *, timeout=None, check=True, input=None, env=None, cwd=None):
        del timeout, check, env, cwd  # the mirror passes only argv; the recorded command lines are the point
        self.commands.append(([str(item) for item in argv], input))
        if any(str(argv[-1]).startswith(prefix) for prefix in self.fail_commands):
            raise CommandFailed(argv, 1, "ssh: connect to host: Connection timed out\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def lines(self, prefix):
        """The remote command strings of every ssh run whose command starts with `prefix`."""
        return [argv[-1] for argv, _ in self.commands if argv[-1].startswith(prefix)]

    def pushes_of(self):
        return [Path(path).name for path, _, _ in self._transfer.pushes]


def settings(tmp_path, **overrides):
    local = tmp_path / "tree"
    local.mkdir(exist_ok=True)
    base = {
        "direction": "push",
        "remote": "me@host",
        "remote_dir": "/tree",
        "local_dir": str(local),
        "delete": False,
        "ignore": [],
        "mtime_tolerance": 2.0,
        "settle": 0.0,
        "file_timeout": 0.0,
        "protocol": "scp",
        "resume": False,
        "password_env": None,
        "password_mode": "askpass",
        "part_max_age": 7 * 86400.0,
        "remote_python": "auto",
        "ssh_command": None,
        "port": None,
        "identity": None,
        "known_hosts": None,
        "ssh_options": SSH_OPTIONS,
        "reachable_host": None,
        "reachable_port": None,
        "reachable_timeout": 5.0,
    }
    base.update(overrides)
    return base


def tree(tmp_path, *names, data=b"payload"):
    local = tmp_path / "tree"
    for name in names:
        path = local / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return local


def saved(entries):
    """The checkpoint document holding these relative paths."""
    return mirror.as_document({name: [7, 1700000000.0] for name in entries}, set())


def test_unreachable_host_is_unknown_and_nothing_is_deleted(tmp_path):
    tree(tmp_path, "a.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=True), reachable=False)
    ctx.checkpoint("mirror").save(saved(["a.txt", "gone.txt"]))
    with pytest.raises(Unknown):
        mirror.check(ctx)
    assert ctx.transfer.pushes == []
    assert "gone.txt" in mirror.files_of(ctx.checkpoint("mirror").value)


def test_an_unreadable_subtree_is_unknown_not_a_deletion(tmp_path):
    """A walk that could not read everything must never look like the source deleting it (spec 5.4)."""
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs a POSIX user that is not root")
    local = tree(tmp_path, "ok.txt", "locked/inside.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=True), reachable=True)
    ctx.checkpoint("mirror").save(saved(["locked/inside.txt"]))
    os.chmod(local / "locked", 0)
    try:
        with pytest.raises(Unknown, match="locked"):
            mirror.check(ctx)
    finally:
        os.chmod(local / "locked", 0o700)
    assert "locked/inside.txt" in mirror.files_of(ctx.checkpoint("mirror").value)


def test_a_tree_that_matches_the_checkpoint_is_false(tmp_path):
    tree(tmp_path, "a.txt")
    info = (tmp_path / "tree" / "a.txt").stat()
    ctx = FakeCtx(tmp_path, settings(tmp_path))
    ctx.checkpoint("mirror").save(mirror.as_document({"a.txt": [info.st_size, info.st_mtime]}, set()))
    condition, payload = mirror.check(ctx)
    assert condition is False
    assert payload == {"mkdir": [], "copy": [], "deleted": []}
    assert ctx.transfer.pushes == []


def test_reachable_strips_the_user_from_the_host():
    """A probe to user@host would never connect: tcp_open wants the host alone (the e2e test caught this)."""
    assert mirror.reachable({"remote": "me@host", "reachable_host": None, "reachable_port": None, "port": None}) == (
        "host",
        22,
    )
    assert mirror.reachable(
        {"remote": "me@host", "reachable_host": "probe", "reachable_port": 2222, "port": 2200}
    ) == ("probe", 2222)


def test_mkdir_batches_are_bounded(tmp_path):
    """Every remote command line stays under the Windows CreateProcess limit: <= 100 paths per call."""
    for index in range(250):
        tree(tmp_path, "d%03d/a.txt" % index)
    ctx = FakeCtx(tmp_path, settings(tmp_path))
    condition, payload = mirror.check(ctx)
    assert condition is True and len(payload["mkdir"]) == 250
    ctx.payload = payload
    mirror.on_true(ctx)
    mkdirs = ctx.lines("mkdir -p")
    assert len(mkdirs) == 3
    assert [len(line.split()) for line in mkdirs] == [102, 102, 52]  # "mkdir -p" plus the paths


def test_a_path_with_a_space_is_quoted_in_a_batch(tmp_path):
    """The remote login shell parses the command string, so a path with a space must survive it."""
    tree(tmp_path, "sp ace/a.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path))
    _, payload = mirror.check(ctx)
    ctx.payload = payload
    mirror.on_true(ctx)
    assert ctx.lines("mkdir -p") == ["mkdir -p '/tree/sp ace'"]


def test_rm_batches_and_the_empty_directory_sweep(tmp_path):
    (tmp_path / "tree").mkdir()
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=True))
    ctx.checkpoint("mirror").save(saved(["f%03d.txt" % index for index in range(250)]))
    condition, payload = mirror.check(ctx)
    assert condition is True and len(payload["deleted"]) == 250
    ctx.payload = payload
    mirror.on_true(ctx)
    rms = ctx.lines("rm -f")
    assert len(rms) == 3 and [len(line.split()) for line in rms] == [102, 102, 52]  # "rm -f" plus the paths
    sweep = ctx.lines("find ")
    assert len(sweep) == 1 and sweep[0].endswith("-type d -empty -delete")
    assert mirror.files_of(ctx.checkpoint("mirror").value) == {}


def test_delete_off_forgets_the_paths_but_removes_nothing(tmp_path):
    (tmp_path / "tree").mkdir()
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=False))
    ctx.checkpoint("mirror").save(saved(["gone.txt"]))
    condition, payload = mirror.check(ctx)
    assert condition is True and payload["deleted"] == ["gone.txt"]
    ctx.payload = payload
    mirror.on_true(ctx)
    assert ctx.lines("rm -f") == []
    assert mirror.files_of(ctx.checkpoint("mirror").value) == {}


def test_a_deletion_the_remote_refused_stays_in_the_checkpoint(tmp_path):
    """delete=true and the `rm` fails: the paths must be planned again, not forgotten (the trees would diverge)."""
    (tmp_path / "tree").mkdir()
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=True), fail_commands=["rm -f"])
    ctx.checkpoint("mirror").save(saved(["gone.txt"]))
    condition, payload = mirror.check(ctx)
    assert payload["deleted"] == ["gone.txt"]
    ctx.payload = payload
    with pytest.raises(CommandFailed):
        mirror.on_true(ctx)
    assert "gone.txt" in mirror.files_of(ctx.checkpoint("mirror").value)
    condition, payload = mirror.check(ctx)  # the next poll still has work to do
    assert condition is True and payload["deleted"] == ["gone.txt"]


def test_a_failed_push_is_not_promoted(tmp_path):
    """The failed file's entry stays out of the checkpoint, so the next poll copies it again."""
    tree(tmp_path, "a.txt", "b.txt", "c.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path), fail=["b.txt"])
    condition, payload = mirror.check(ctx)
    assert condition is True
    ctx.payload = payload
    with pytest.raises(TransferFailed, match="b.txt"):
        mirror.on_true(ctx)
    assert sorted(ctx.pushes_of()) == ["a.txt", "c.txt"]
    assert sorted(mirror.files_of(ctx.checkpoint("mirror").value)) == ["a.txt", "c.txt"]


def test_promotion_happens_in_bounded_batches(tmp_path):
    """One save per file is quadratic; the checkpoint is saved as batches land, never per file."""
    names = ["f%04d.txt" % index for index in range(1200)]
    for name in names:
        tree(tmp_path, name)
    ctx = FakeCtx(tmp_path, settings(tmp_path))
    _, payload = mirror.check(ctx)
    ctx.payload = payload
    mirror.on_true(ctx)
    assert len(ctx.transfer.pushes) == 1200
    saves = ctx.checkpoint("mirror").saves
    assert 1 < saves <= 5, saves
    assert sorted(mirror.files_of(ctx.checkpoint("mirror").value)) == sorted(names)


def test_ssh_run_argv_ends_with_the_remote_command():
    argv = ssh_run_argv("me@host", "mkdir -p '/a b'", port=2222, ssh_command=("ssh",))
    assert argv[-3:] == ["--", "me@host", "mkdir -p '/a b'"]
    assert argv[:1] == ["ssh"] and "-T" in argv and "BatchMode=yes" in argv
    assert argv[argv.index("-p") + 1] == "2222"


def pull_settings(tmp_path, **overrides):
    return settings(tmp_path, direction="pull", **overrides)


def file_event(*paths):
    """A watcher `files` event: (path, size, mtime) triples under the watch's remote_dir."""
    return {"event": "files", "entries": [[path, 7, 1700000000.0] for path in paths]}


def test_a_pull_whose_times_cannot_be_set_keeps_the_file_and_says_so(tmp_path, monkeypatch):
    """The repair exists for filesystems that refuse utime (FAT, read-only-ish dirs): the file still landed."""
    import os as os_module

    ctx = FakeCtx(tmp_path, pull_settings(tmp_path), events=[file_event("/tree/a.txt")])
    condition, payload = mirror.check(ctx)
    assert condition is True
    ctx.payload = payload

    def refuse(path, times):
        raise OSError("utime is not supported here")

    monkeypatch.setattr(os_module, "utime", refuse)
    messages = []
    monkeypatch.setattr(ctx.log, "warning", lambda message, *args: messages.append(message % args))
    mirror.on_true(ctx)
    assert (Path(pull_settings(tmp_path)["local_dir"]) / "a.txt").exists()
    assert any("cannot set the times" in message for message in messages), messages


def test_a_quiet_stream_never_deletes(tmp_path):
    """No events means "nothing to do", never "everything was deleted" (spec 5.6)."""
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path, delete=True))
    ctx.checkpoint("mirror").save(saved(["b.txt"]))
    condition, payload = mirror.check(ctx)
    assert condition is False
    assert payload["deleted"] == []
    ctx.payload = payload
    mirror.on_true(ctx)
    assert "b.txt" in mirror.files_of(ctx.checkpoint("mirror").value)


def test_pull_copies_from_the_events_and_repairs_the_mtime(tmp_path):
    """scp -p may not set the times (Windows); the event's mtime is the repair."""
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path), events=[file_event("/tree/sub/a.txt")])
    condition, payload = mirror.check(ctx)
    assert condition is True and payload["copy"] == ["sub/a.txt"]
    assert payload["times"] == {"sub/a.txt": 1700000000.0}
    ctx.payload = payload
    mirror.on_true(ctx)
    target = Path(ctx.settings["local_dir"]) / "sub" / "a.txt"
    assert target.read_bytes() == b"payload"
    assert target.stat().st_mtime == 1700000000.0
    assert mirror.files_of(ctx.checkpoint("mirror").value) == {"sub/a.txt": [7, 1700000000.0]}
    assert ctx.transfer.pulls[0][2]["preserve_mtime"] is True
    assert ctx.transfer.pulls[0][2]["on_conflict"] == "overwrite"


def test_pull_creates_the_directories_the_watcher_reported(tmp_path):
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path), events=[{"event": "dirs", "paths": ["/tree/new"]}])
    condition, payload = mirror.check(ctx)
    assert condition is True and payload["mkdir"] == ["new"]
    ctx.payload = payload
    mirror.on_true(ctx)
    assert (Path(ctx.settings["local_dir"]) / "new").is_dir()
    assert mirror.dirs_of(ctx.checkpoint("mirror").value) == ["new"]


def test_pull_deletes_gone_paths_and_the_directories_they_emptied(tmp_path):
    local = Path(pull_settings(tmp_path)["local_dir"])
    (local / "sub").mkdir(parents=True)
    (local / "sub" / "b.txt").write_bytes(b"x")
    events = [{"event": "gone", "paths": ["/tree/sub/b.txt", "/tree/sub"]}]
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path, delete=True), events=events)
    ctx.checkpoint("mirror").save(mirror.as_document({"sub/b.txt": [1, 5.0]}, {"sub"}))
    condition, payload = mirror.check(ctx)
    assert condition is True and set(payload["deleted"]) == {"sub", "sub/b.txt"}
    ctx.payload = payload
    mirror.on_true(ctx)
    assert not (local / "sub" / "b.txt").exists()
    assert not (local / "sub").exists()  # emptied, so it goes too
    assert mirror.files_of(ctx.checkpoint("mirror").value) == {}
    assert mirror.dirs_of(ctx.checkpoint("mirror").value) == []


def test_delete_off_forgets_a_gone_path_but_keeps_the_local_file(tmp_path):
    local = Path(pull_settings(tmp_path)["local_dir"])
    local.mkdir(parents=True, exist_ok=True)
    (local / "b.txt").write_bytes(b"x")
    events = [{"event": "gone", "paths": ["/tree/b.txt"]}]
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path, delete=False), events=events)
    ctx.checkpoint("mirror").save(saved(["b.txt"]))
    condition, payload = mirror.check(ctx)
    assert condition is True and payload["deleted"] == ["b.txt"]
    ctx.payload = payload
    mirror.on_true(ctx)
    assert (local / "b.txt").exists()  # kept: delete is off
    assert "b.txt" not in mirror.files_of(ctx.checkpoint("mirror").value)  # forgotten either way


def test_a_failed_pull_is_not_promoted(tmp_path):
    events = [file_event("/tree/a.txt", "/tree/c.txt")]
    ctx = FakeCtx(tmp_path, pull_settings(tmp_path), fail=["a.txt"], events=events)
    _, payload = mirror.check(ctx)
    ctx.payload = payload
    with pytest.raises(TransferFailed, match="a.txt"):
        mirror.on_true(ctx)
    assert sorted(mirror.files_of(ctx.checkpoint("mirror").value)) == ["c.txt"]


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    running = ScpServer(root, tmp_path).start()
    yield running
    running.stop()


def mirror_watch(make_watch, server, tmp_path, extra=""):
    config = tmp_path / "ssh_config"
    config.write_text("", encoding="utf-8")
    return load_watch_config(
        make_watch(
            "mirror-push",
            config=(
                'recipe = "mirror"\n[settings]\ndirection = "push"\n'
                f"local_dir = {toml_path(tmp_path / 'tree')}\nremote = 'u@127.0.0.1'\nremote_dir = '/'\n"
                f"port = {server.port}\nidentity = {toml_path(server.client_key)}\n"
                f"known_hosts = {toml_path(server.known_hosts)}\n"
                f"ssh_options = ['-F', {toml_path(config)}, '-o', 'IdentityAgent=none']\nsettle = '0s'\n{extra}"
            ),
        )
    )


def poll(xdg, watch, state=None):
    global_config = load_global_config(xdg.config_file, xdg)
    engine = PollEngine(
        runner=Runner(), paths=xdg, global_config=global_config, sink=lambda _record: None, pid=os.getpid()
    )
    return engine.poll(watch, state or WatchState(False))


def test_settle_with_delete_keeps_a_synced_file_that_is_being_written(tmp_path):
    """Issue 25, end to end: the file is in the checkpoint but inside settle, so it is not a deletion."""
    tree(tmp_path, "a.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path, delete=True, settle=60.0))
    ctx.checkpoint("mirror").save(saved(["a.txt"]))
    condition, payload = mirror.check(ctx)
    assert condition is False
    assert payload["deleted"] == [] and payload["copy"] == []
    assert "a.txt" in mirror.files_of(ctx.checkpoint("mirror").value)


def test_a_file_that_changes_during_its_copy_is_not_promoted(tmp_path, monkeypatch):
    """Issue 26: recording the pre-copy stat would call a mixed/older upload synced; defer it instead."""
    tree(tmp_path, "a.txt")
    ctx = FakeCtx(tmp_path, settings(tmp_path))
    _, payload = mirror.check(ctx)
    ctx.payload = payload
    original = ctx.transfer.push

    def push_while_growing(path, remote_dir, **options):
        result = original(path, remote_dir, **options)
        Path(path).write_bytes(b"payload has grown")
        return result

    monkeypatch.setattr(ctx.transfer, "push", push_while_growing)
    mirror.on_true(ctx)
    assert "a.txt" not in mirror.files_of(ctx.checkpoint("mirror").value)
    condition, payload = mirror.check(ctx)
    assert condition is True and payload["copy"] == ["a.txt"]


def test_a_resumable_push_continues_a_verified_prefix(tmp_path, monkeypatch):
    data = b"payload"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    answers = iter([(3, hashlib.sha256(data[:3]).hexdigest()), (7, hashlib.sha256(data).hexdigest())])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    finals, removed = [], []
    monkeypatch.setattr(
        mirror, "_remote_finalize", lambda ctx, settings, temp, final: finals.append((temp, final))
    )
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: removed.append(args))
    mirror._resumable_push(ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, source.stat())
    assert ctx.transfer.uploads and ctx.transfer.uploads[0][2] is True
    assert not removed
    assert finals[0][1] == "/tree/a.txt"


def test_a_resumable_push_restarts_when_the_prefix_changed(tmp_path, monkeypatch):
    data = b"payload"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    answers = iter([(3, hashlib.sha256(b"XXX").hexdigest()), (7, hashlib.sha256(data).hexdigest())])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    removed = []
    monkeypatch.setattr(mirror, "_remote_finalize", lambda *args: None)
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: removed.append(args))
    mirror._resumable_push(ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, source.stat())
    assert removed
    assert ctx.transfer.uploads and ctx.transfer.uploads[0][2] is False


def test_a_resumable_push_finalizes_a_complete_temp_without_uploading(tmp_path, monkeypatch):
    data = b"payload"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    monkeypatch.setattr(
        mirror, "_remote_hash", lambda *args, **kwargs: (7, hashlib.sha256(data).hexdigest())
    )
    finals = []
    monkeypatch.setattr(
        mirror, "_remote_finalize", lambda ctx, settings, temp, final: finals.append((temp, final))
    )
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: None)
    mirror._resumable_push(ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, source.stat())
    assert not ctx.transfer.uploads
    assert finals


def test_a_resumable_push_keeps_an_upload_that_does_not_match_the_snapshot(tmp_path, monkeypatch):
    """A temp that is not the pre-upload snapshot may be a valid grown prefix; the next poll decides."""
    data = b"payload"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    answers = iter([None, (6, hashlib.sha256(b"wrong!").hexdigest())])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    removed, finals = [], []
    monkeypatch.setattr(mirror, "_remote_finalize", lambda *args: finals.append(args))
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: removed.append(args))
    before = source.stat()
    assert mirror._resumable_push(
        ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, before
    ) is False
    assert not removed and not finals
    assert ctx.transfer.uploads and ctx.transfer.uploads[0][2] is False


def test_a_temp_matching_the_grown_file_is_finalized_next_poll_without_reupload(tmp_path, monkeypatch):
    """The upload read past the pre-upload snapshot: the temp is complete for the grown file; finalize it."""
    data = b"payload"
    grown = data + b" more"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    answers = iter([
        None, (len(grown), hashlib.sha256(grown).hexdigest()),
        (len(grown), hashlib.sha256(grown).hexdigest()),
    ])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    finals, removed = [], []
    monkeypatch.setattr(
        mirror, "_remote_finalize", lambda ctx, settings, temp, final: finals.append((temp, final))
    )
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: removed.append(args))
    uploads = []
    original = ctx.transfer.upload

    def upload(path, remote, *, resume=False, **options):
        uploads.append(resume)
        original(path, remote, resume=resume, **options)
        Path(path).write_bytes(grown)  # the writer appends while the first upload runs

    monkeypatch.setattr(ctx.transfer, "upload", upload)
    before = source.stat()
    assert mirror._resumable_push(
        ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, before
    ) is False
    assert not finals and not removed
    before = source.stat()
    assert mirror._resumable_push(
        ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, before
    ) is True
    assert uploads == [False]
    assert finals and not removed


def test_uploading_path_is_a_hidden_temp_beside_the_target():
    settings = {"remote_dir": "/tree"}
    assert mirror.uploading_path(settings, "sub/a.bin") == (
        "/tree/sub/.a.bin.watchglass-" + hashlib.sha256(b"sub/a.bin").hexdigest()[:8] + ".part"
    )


def test_a_source_that_grows_during_a_push_keeps_its_temp_and_resumes_next_poll(tmp_path, monkeypatch):
    """Issue 26's case: the first upload is a coherent old version; the next poll continues it."""
    data = b"payload"
    grown = data + b" more"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    source = tmp_path / "tree" / "a.txt"
    answers = iter([
        None, (len(data), hashlib.sha256(data).hexdigest()),
        (len(data), hashlib.sha256(data).hexdigest()), (len(grown), hashlib.sha256(grown).hexdigest()),
    ])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    finals = []
    monkeypatch.setattr(
        mirror, "_remote_finalize", lambda ctx, settings, temp, final: finals.append((temp, final))
    )
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: None)
    uploads = []
    original = ctx.transfer.upload

    def upload(path, remote, *, resume=False, **options):
        uploads.append(resume)
        original(path, remote, resume=resume, **options)
        if len(uploads) == 1:
            Path(path).write_bytes(grown)  # the writer appends during the first upload

    monkeypatch.setattr(ctx.transfer, "upload", upload)
    before = source.stat()
    assert mirror._resumable_push(ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, before) is False
    assert not finals
    before = source.stat()
    assert mirror._resumable_push(ctx, ctx.settings, source, "a.txt", mirror.transfer_options(ctx.settings), 60.0, before) is True
    assert uploads == [False, True]
    assert finals


def test_a_rejected_resume_helper_login_keeps_its_permanent_classification(tmp_path, monkeypatch):
    """Issue 24 through the resume helper: a rejected login must park, not be retried as a missing Python."""
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))

    def run(*args, **kwargs):
        raise CommandFailed(["ssh"], 255, "u@host: Permission denied (publickey,password).\n")

    monkeypatch.setattr(ctx, "run", run)
    with pytest.raises(CommandFailed) as caught:
        mirror._remote_hash(ctx, ctx.settings, "/tree/x.part")
    assert caught.value.permanent_reason == "authentication failed"


def test_the_remote_helper_modes_round_trip(tmp_path, monkeypatch):
    """The hash/finalize/remove options and JSON answers, without an ssh server (the helper runs locally)."""
    data = b"payload"
    temp = tmp_path / "temp.bin"
    temp.write_bytes(data)
    final = tmp_path / "final.bin"
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))

    def run(argv, *, timeout=None, check=True, input=None, env=None, cwd=None):
        completed = subprocess.run(
            [sys.executable, "-u", "-"], input=(input or "").encode(), capture_output=True, timeout=timeout
        )
        if check and completed.returncode != 0:
            raise CommandFailed(argv, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        return subprocess.CompletedProcess(argv, completed.returncode, completed.stdout.decode(), completed.stderr.decode())

    monkeypatch.setattr(ctx, "run", run)
    assert mirror._remote_hash(ctx, ctx.settings, str(temp)) == (7, hashlib.sha256(data).hexdigest())
    mirror._remote_finalize(ctx, ctx.settings, str(temp), str(final))
    assert final.read_bytes() == data and not temp.exists()
    mirror._remote_remove(ctx, ctx.settings, str(final))
    assert not final.exists()


def test_on_true_push_with_resume_finalizes_and_promotes(tmp_path, monkeypatch):
    data = b"payload"
    tree(tmp_path, "a.txt", data=data)
    ctx = FakeCtx(tmp_path, settings(tmp_path, resume=True, protocol="sftp"))
    answers = iter([None, (7, hashlib.sha256(data).hexdigest())])
    monkeypatch.setattr(mirror, "_remote_hash", lambda *args, **kwargs: next(answers))
    finals = []
    monkeypatch.setattr(
        mirror, "_remote_finalize", lambda ctx, settings, temp, final: finals.append((temp, final))
    )
    monkeypatch.setattr(mirror, "_remote_remove", lambda *args: None)
    _, payload = mirror.check(ctx)
    ctx.payload = payload
    mirror.on_true(ctx)
    assert ctx.transfer.uploads and ctx.transfer.uploads[0][2] is False
    assert finals and finals[0][1] == "/tree/a.txt"
    assert mirror.files_of(ctx.checkpoint("mirror").value)["a.txt"][0] == 7


def test_a_real_push_copies_the_file_with_its_mtime_and_promotes(make_watch, server, tmp_path, xdg):
    """The end-to-end proof: a real poll, a real scp, and a checkpoint that then calls the tree level."""
    old = time.time() - 600
    path = tree(tmp_path, "a.txt")
    os.utime(path / "a.txt", (old, old))
    watch = mirror_watch(make_watch, server, tmp_path)
    report = poll(xdg, watch)
    assert report.failed is False, report.results
    assert (server.root / "a.txt").read_bytes() == b"payload"
    assert (server.root / "a.txt").stat().st_mtime == pytest.approx(old, abs=2)
    assert mirror.files_of(Checkpoint(checkpoint_file(xdg.watch_data_dir("mirror-push"), "mirror")).value) == {
        "a.txt": [7, pytest.approx(old, abs=2)]
    }
    again = poll(xdg, watch, WatchState(True))
    assert again.after.condition is False  # nothing changed: no second copy
