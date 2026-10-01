from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.config import LogSettings, load_global_config


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_init_creates_config_and_watches_dir(xdg):
    result = run("init")
    assert result.exit_code == 0, result.output
    assert f"created {xdg.config_file}" in result.output
    assert xdg.default_watches_dir.is_dir()
    config = load_global_config(xdg.config_file, xdg)
    assert config.watch_dirs == (xdg.default_watches_dir,)
    assert config.reload_interval == 5.0 and config.log == LogSettings()
    assert dict(config.defaults) == {}


def test_init_never_overwrites(xdg):
    xdg.config_home.mkdir(parents=True)
    xdg.config_file.write_text('reload_interval = "9s"\n')
    result = run("init")
    assert result.exit_code == 0
    assert f"exists  {xdg.config_file}" in result.output
    assert xdg.config_file.read_text() == 'reload_interval = "9s"\n'


def test_init_writes_agent_files_once(xdg):
    assert run("init").exit_code == 0
    agents = xdg.default_watches_dir / "AGENTS.md"
    claude = xdg.default_watches_dir / "CLAUDE.md"
    assert "keepwatch docs agent" in agents.read_text()
    assert claude.read_text() == "@AGENTS.md\n"
    agents.write_text("# mine\n")
    result = run("init")
    assert f"exists  {agents}" in result.output
    assert agents.read_text() == "# mine\n"
