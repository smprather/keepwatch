"""Recognise the ssh/scp failures that retrying can never fix (issue 24).

A wrong password, a key the server will not accept, or a changed host key is permanent: another attempt asks
the same server the same question and gets the same no — while burning the account's lockout budget (fail2ban,
AD policies). These are the shapes OpenSSH prints; a *file-level* `Permission denied` names a path and is not
one of them (see `transfer._refused_overwrite`). Everything else — a host that is down, DNS, a timeout, a
refused connection — is transient and keeps the ordinary backoff.

Stdlib only: the worker and `ctx` import this module inside plugin environments.
"""

from __future__ import annotations

import re

AUTHENTICATION_FAILED = "authentication failed"
HOST_KEY_FAILED = "host key verification failed"

_AUTHENTICATION = re.compile(
    r"Permission denied \((?=[^)]*(?:publickey|password|keyboard-interactive|hostbased|gssapi))[^)]*\)"
    r"|Too many authentication failures"
    r"|No more authentication methods to try"
    r"|No supported authentication methods available",
    re.IGNORECASE,
)
_HOST_KEY = re.compile(
    r"Host key verification failed"
    r"|REMOTE HOST IDENTIFICATION HAS CHANGED"
    r"|Host key for .+ has changed",
    re.IGNORECASE,
)


def rejection_reason(stderr: str | None) -> str | None:
    """The short reason this failure is permanent (`authentication failed` / `host key verification failed`).

    None means the failure is not a rejection: callers keep their ordinary retry/backoff behavior.
    """
    text = stderr or ""
    if _AUTHENTICATION.search(text):
        return AUTHENTICATION_FAILED
    if _HOST_KEY.search(text):
        return HOST_KEY_FAILED
    return None
