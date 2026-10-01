import subprocess as _subprocess
import sys

import pytest
from click.testing import CliRunner

from keepwatch import cli as cli_module
from keepwatch import systemd, winsched
from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def fake_systemctl(tmp_path, monkeypatch, exit_code=0):
    calls = tmp_path / "calls.txt"
    script = tmp_path / "systemctl"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho "fake failure" >&2\nexit {exit_code}\n'
                      if exit_code else f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("KEEPWATCH_SYSTEMCTL", str(script))
    return calls


def unit_file(xdg):
    return xdg.config_home.parent / "systemd" / "user" / "keepwatch.service"


@pytest.mark.posix_only
def test_install_writes_the_unit_and_enables_it(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    result = run("install")
    assert result.exit_code == 0, result.output
    text = unit_file(xdg).read_text()
    assert "ExecStart=" in text and " run" in text
    assert 'Environment="PATH=' in text and "WantedBy=default.target" in text
    assert calls.read_text().splitlines() == ["--user daemon-reload", "--user enable --now keepwatch.service"]
    assert "systemctl --user status keepwatch" in result.output
    assert "SSH_AUTH_SOCK" not in result.output


@pytest.mark.posix_only
def test_install_warns_about_ssh_agent(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-XXXX/agent.123")
    result = run("install")
    assert result.exit_code == 0
    assert "SSH_AUTH_SOCK" in result.output and "keepwatch docs environment" in result.output


@pytest.mark.posix_only
def test_install_dry_run_changes_nothing(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    result = run("install", "--dry-run")
    assert result.exit_code == 0
    assert "[Service]" in result.output and "systemctl --user enable --now keepwatch.service" in result.output
    assert not unit_file(xdg).exists() and not calls.exists()


@pytest.mark.posix_only
def test_install_reports_systemctl_failure(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch, exit_code=1)
    result = run("install")
    assert result.exit_code == 1
    assert "fake failure" in result.output


@pytest.mark.posix_only
def test_uninstall(xdg, tmp_path, monkeypatch):
    calls = fake_systemctl(tmp_path, monkeypatch)
    assert run("install").exit_code == 0
    result = run("uninstall")
    assert result.exit_code == 0, result.output
    assert not unit_file(xdg).exists()
    assert calls.read_text().splitlines()[-2:] == ["--user disable --now keepwatch.service", "--user daemon-reload"]


@pytest.mark.posix_only
def test_install_keeps_a_custom_config(xdg, tmp_path, monkeypatch):
    fake_systemctl(tmp_path, monkeypatch)
    custom = tmp_path / "custom.toml"
    custom.write_text("")
    assert run("--config", str(custom), "install").exit_code == 0
    assert f'--config "{custom.resolve()}" run' in unit_file(xdg).read_text()
    assert run("install").exit_code == 0
    assert "--config" not in unit_file(xdg).read_text()


@pytest.mark.posix_only
def test_find_executable_prefers_the_running_script(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "keepwatch"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(script), "install"])
    assert systemd.find_executable() == str(script)


def on_windows(monkeypatch, exit_codes=()):
    scripts = []
    codes = list(exit_codes)

    def fake_powershell(script):
        scripts.append(script)
        code = codes.pop(0) if codes else 0
        return _subprocess.CompletedProcess(["powershell.exe"], code, "", "refused by policy" if code else "")

    monkeypatch.setattr(cli_module, "_on_windows", lambda: True)
    monkeypatch.setattr(winsched, "run_powershell", fake_powershell)
    return scripts


def test_windows_install_registers_and_starts_a_logon_task(xdg, monkeypatch):
    scripts = on_windows(monkeypatch)
    result = run("install")
    assert result.exit_code == 0, result.output
    register, start = scripts
    assert "Register-ScheduledTask" in register and "New-ScheduledTaskTrigger -AtLogOn" in register
    assert "-m keepwatch run" in register and "keepwatch.lnk" in register
    assert "Start-ScheduledTask" not in register and "Start-ScheduledTask" in start
    assert "registered Task Scheduler task 'keepwatch'" in result.output and "started it" in result.output


def test_windows_install_start_failure_is_only_a_warning(xdg, monkeypatch):
    scripts = on_windows(monkeypatch, exit_codes=(0, 1))
    result = run("install")
    assert result.exit_code == 0, result.output
    assert len(scripts) == 2
    assert "will start at the next logon" in result.output


def test_windows_install_falls_back_to_a_startup_shortcut(xdg, monkeypatch):
    scripts = on_windows(monkeypatch, exit_codes=(1, 0))
    result = run("install")
    assert result.exit_code == 0, result.output
    assert "CreateShortcut" in scripts[1]
    assert "Startup-folder shortcut" in result.output


def test_windows_install_fails_when_both_methods_fail(xdg, monkeypatch):
    on_windows(monkeypatch, exit_codes=(1, 1))
    result = run("install")
    assert result.exit_code == 1
    assert "refused by policy" in result.output


def test_windows_install_dry_run_and_custom_config(xdg, tmp_path, monkeypatch):
    scripts = on_windows(monkeypatch)
    custom = tmp_path / "custom.toml"
    custom.write_text("")
    result = run("--config", str(custom), "install", "--dry-run")
    assert result.exit_code == 0
    assert scripts == []
    assert "Register-ScheduledTask" in result.output and "CreateShortcut" in result.output
    assert f'--config "{custom.resolve()}" run' in result.output


def test_windows_uninstall(xdg, monkeypatch):
    scripts = on_windows(monkeypatch)
    result = run("uninstall")
    assert result.exit_code == 0, result.output
    (script,) = scripts
    assert "Unregister-ScheduledTask" in script and "keepwatch.lnk" in script


def test_windows_uninstall_has_one_windows_branch():
    import inspect

    source = inspect.getsource(cli_module.uninstall.callback)
    assert source.count("if _on_windows():") == 1
