import os
import textwrap

import pytest

from keepwatch.paths import Paths, resolve_paths

IS_WINDOWS = os.name == "nt"


def pytest_configure(config):
    config.addinivalue_line("markers", "posix_only: needs a POSIX system (signals, sh, Expect, systemd)")
    config.addinivalue_line("markers", "windows_only: Windows-specific behaviour")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "posix_only" in item.keywords and IS_WINDOWS:
            item.add_marker(pytest.mark.skip(reason="POSIX only"))
        if "windows_only" in item.keywords and not IS_WINDOWS:
            item.add_marker(pytest.mark.skip(reason="Windows only"))


@pytest.fixture
def xdg(tmp_path, monkeypatch) -> Paths:
    """Point every XDG variable at a private temporary tree."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("KEEPWATCH_CONFIG", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return resolve_paths()


@pytest.fixture
def make_watch(xdg):
    """Create a watch directory; files starting with '#!' become executable."""

    def make(name, config="", files=None, base=None):
        watch_dir = (base or xdg.default_watches_dir) / name
        watch_dir.mkdir(parents=True)
        (watch_dir / "config.toml").write_text(textwrap.dedent(config))
        for relative, content in (files or {}).items():
            path = watch_dir / relative
            text = textwrap.dedent(content).lstrip("\n")
            path.write_text(text)
            if text.startswith("#!"):
                path.chmod(0o755)
        return watch_dir

    return make


@pytest.fixture
def call_for(xdg):
    from keepwatch.config import load_watch_config
    from keepwatch.runner import HookCall

    def make(watch_dir, hook="check", *, timeout=30.0, condition=False, payload=None,
             capture_bytes=65536, environment=None, mode="call"):
        watch = load_watch_config(watch_dir)
        return HookCall(
            watch=watch,
            hook=hook,
            poll_id="p1",
            condition=condition,
            payload=payload,
            data_dir=xdg.watch_data_dir(watch.name),
            run_dir=xdg.run_dir(os.getpid(), watch.name),
            timeout=timeout,
            capture_bytes=capture_bytes,
            environment=environment or {},
            mode=mode,
        )

    return make
