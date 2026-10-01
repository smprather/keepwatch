"""Windows: start keepwatch at logon with Task Scheduler, or a Startup-folder shortcut as a fallback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from keepwatch import platform

TASK_NAME = "keepwatch"
_SHORTCUT = "(Join-Path ([Environment]::GetFolderPath('Startup')) 'keepwatch.lnk')"


def pythonw() -> str:
    """pythonw.exe next to the running interpreter (no console window), else the interpreter itself."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else Path(sys.executable))


def _quote(text: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + text.replace("'", "''") + "'"


def run_arguments(config_path: Path | None) -> str:
    config = f' --config "{config_path}"' if config_path is not None else ""
    return f"-m keepwatch{config} run"


def register_script(executable: str, arguments: str) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute {_quote(executable)} -Argument {_quote(arguments)}",
            '$user = "$env:USERDOMAIN\\$env:USERNAME"',
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew",
            "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
            f"Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Action $action -Trigger $trigger "
            "-Settings $settings -Principal $principal -Force | Out-Null",
            f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}",
        ]
    )


def shortcut_script(executable: str, arguments: str) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$path = {_SHORTCUT}",
            "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($path)",
            f"$shortcut.TargetPath = {_quote(executable)}",
            f"$shortcut.Arguments = {_quote(arguments)}",
            "$shortcut.WindowStyle = 7",
            "$shortcut.Save()",
            f"Start-Process -FilePath {_quote(executable)} -ArgumentList {_quote(arguments)} -WindowStyle Hidden",
        ]
    )


def unregister_script() -> str:
    return "\n".join(
        [
            f"Unregister-ScheduledTask -TaskName {_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue",
            f"Remove-Item -LiteralPath {_SHORTCUT} -ErrorAction SilentlyContinue",
        ]
    )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    """Run a script with Windows PowerShell ($KEEPWATCH_POWERSHELL replaces powershell.exe)."""
    program = os.environ.get("KEEPWATCH_POWERSHELL", "powershell.exe")
    return subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=platform.NO_WINDOW,
    )
