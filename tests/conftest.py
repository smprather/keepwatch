import textwrap

import pytest

from keepwatch.paths import Paths, resolve_paths


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
