"""Starting points for `keepwatch new`. "{name}" is replaced with the watch name."""

from __future__ import annotations

PYTHON_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"
# check_timeout = "60s"
# action_timeout = "60s"
# max_failures = 5
# retry_after = "1h"
# python_dependencies = ["requests>=2.32"]

[settings]
# Free-form parameters for watch.py: ctx.settings["example"]
example = "value"
'''

PYTHON_WATCH = '''"""Watch "{name}". Contract: keepwatch docs python. Helpers: keepwatch docs ctx."""

from keepwatch import Ctx


def check(ctx: Ctx):
    """Look at the world; never change it.

    Return True or False (optionally as (answer, payload) with a JSON payload for the actions),
    None when you cannot tell right now, or raise an exception if the check itself broke.
    """
    return False


def on_true(ctx: Ctx) -> None:
    """Runs on every poll while the condition is TRUE. Raise to report a failure.

    Other hooks: on_rise (FALSE -> TRUE), on_fall (TRUE -> FALSE), on_false (every poll while FALSE).
    """
    ctx.log.info("condition is TRUE; payload=%s", ctx.payload)
'''

SHELL_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"

[hooks]
# A list runs directly; a string runs through /bin/sh -c. See: keepwatch docs executables
check = ["./check.sh"]
on_true = ["./on_true.sh"]

[check_exit_codes]
# Defaults: true = [0], false = [1]; other codes are errors unless listed here.
unknown = [3]

[settings]
# Reaches the scripts as $KEEPWATCH_SETTING_EXAMPLE
example = "value"
'''

SHELL_CHECK = '''#!/bin/sh
# Check for watch "{name}": exit 0 = TRUE, 1 = FALSE, 3 = unknown (see config.toml).
# To hand data to the actions, write JSON to "$KEEPWATCH_PAYLOAD_OUT".
# Never change anything here: keepwatch poll --dry-run runs this for real.
exit 1
'''

SHELL_ACTION = '''#!/bin/sh
# Runs on every poll while the condition of "{name}" is TRUE. Exit nonzero to report a failure.
# The check's payload (if any) is in the JSON file "$KEEPWATCH_PAYLOAD_FILE".
set -eu
echo "condition is TRUE; example setting: $KEEPWATCH_SETTING_EXAMPLE"
'''

EXPECT_CONFIG = '''# keepwatch watch "{name}". Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"
action_timeout = "2m"

[hooks]
check = ["./check.sh"]
on_true = ["expect", "./on_true.exp"]

[settings]
# Reaches the Expect script as $env(KEEPWATCH_SETTING_HOST)
host = "example.com"
'''

EXPECT_ACTION = '''#!/usr/bin/env expect
# Runs on every poll while the condition of "{name}" is TRUE. exit 1 reports a failure.
# stdin is /dev/null, so drive interactive programs through spawn/expect only.
set timeout 30
set host $env(KEEPWATCH_SETTING_HOST)
spawn echo "connecting to $host"
expect {
    "connecting" { }
    timeout { exit 1 }
}
expect eof
'''

POWERSHELL_CONFIG = '''# keepwatch watch "{name}" (PowerShell). Every key: keepwatch docs config
description = "Describe what {name} watches in one line"
interval = "60s"

[hooks]
# .ps1 files run with Windows PowerShell; see: keepwatch docs executables
check = ["./check.ps1"]
on_true = ["./on_true.ps1"]

[check_exit_codes]
# Defaults: true = [0], false = [1]; other codes are errors unless listed here.
unknown = [3]

[settings]
# Reaches the scripts as $env:KEEPWATCH_SETTING_EXAMPLE
example = "value"
'''

POWERSHELL_CHECK = '''# Check for watch "{name}": exit 0 = TRUE, 1 = FALSE, 3 = unknown (see config.toml).
# To hand data to the actions, write JSON to the file named by $env:KEEPWATCH_PAYLOAD_OUT.
# Never change anything here: keepwatch poll --dry-run runs this for real.
try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
exit 1
'''

POWERSHELL_ACTION = '''# Runs on every poll while the condition of "{name}" is TRUE. Exit nonzero (or throw) to report a failure.
# The check's payload (if any) is in the JSON file named by $env:KEEPWATCH_PAYLOAD_FILE.
try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8 } catch { }
$ErrorActionPreference = 'Stop'
Write-Output "condition is TRUE; example setting: $env:KEEPWATCH_SETTING_EXAMPLE"
'''

TEMPLATES: dict[str, dict[str, str]] = {
    "python": {"config.toml": PYTHON_CONFIG, "watch.py": PYTHON_WATCH},
    "shell": {"config.toml": SHELL_CONFIG, "check.sh": SHELL_CHECK, "on_true.sh": SHELL_ACTION},
    "expect": {"config.toml": EXPECT_CONFIG, "check.sh": SHELL_CHECK, "on_true.exp": EXPECT_ACTION},
    "powershell": {"config.toml": POWERSHELL_CONFIG, "check.ps1": POWERSHELL_CHECK, "on_true.ps1": POWERSHELL_ACTION},
}


def render(template: str, name: str) -> dict[str, str]:
    return {path: content.replace("{name}", name) for path, content in TEMPLATES[template].items()}


GLOBAL_CONFIG = '''# keepwatch global config. Every key: keepwatch docs config
# Every key is optional; the commented values are the defaults. Changes apply live.

# Directories whose subdirectories are watches (earlier entries win name clashes).
# watch_dirs = ["~/.config/keepwatch/watches"]

# How often the service picks up config changes.
# reload_interval = "5s"

# Run when a watch goes offline or comes back online. Receives KEEPWATCH_ALERT_EVENT
# (offline/online), KEEPWATCH_WATCH and KEEPWATCH_ALERT_REASON.
# alert_command = 'notify-send "keepwatch: $KEEPWATCH_WATCH $KEEPWATCH_ALERT_EVENT" "$KEEPWATCH_ALERT_REASON"'

[defaults]
# Defaults for every watch's config.toml.
# interval = "60s"
# check_timeout = "60s"
# action_timeout = "60s"
# max_failures = 5
# retry_after = "1h"
# initial_condition = false

[environment]
# Extra environment variables for every hook, e.g. a stable ssh-agent socket:
# SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"

[log]
# max_bytes = 10000000
# backups = 10
# capture_bytes = 65536
'''


AGENTS_MD = """# keepwatch watches

Every subdirectory here is a keepwatch watch: a `config.toml` plus the code for its check and actions.

Before you create or change a watch, read the contract:

    keepwatch docs agent

`keepwatch docs --all` prints the complete reference.

Development loop: `keepwatch new <name>` → edit → `keepwatch validate <name>` →
`keepwatch poll <name> --fake true,false --dry-run` → `keepwatch poll <name> --dry-run` →
`keepwatch poll <name>` → `keepwatch logs --poll <id> -v`.
"""

CLAUDE_MD = "@AGENTS.md\n"
