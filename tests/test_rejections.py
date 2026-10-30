"""A rejected credential or host key is permanent: the shapes ssh/scp print, and the transient ones beside them."""

import pytest

from watchglass.ctx import CommandFailed
from watchglass.rejections import rejection_reason
from watchglass.transfer import TransferAuthFailed, TransferFailed


@pytest.mark.parametrize(
    "stderr",
    [
        "u@host: Permission denied (publickey,password).\n",
        "Permission denied (publickey,password,keyboard-interactive).\n",
        "Permission denied (publickey).\n",  # BatchMode: no password to offer
        "Received disconnect from 10.0.0.1 port 22:2: Too many authentication failures\n",
        "Too many authentication failures\n",
        "No more authentication methods to try.\n",
        "No supported authentication methods available (server sent: publickey,password)\n",
    ],
)
def test_authentication_rejections_are_permanent(stderr):
    assert rejection_reason(stderr) == "authentication failed"


@pytest.mark.parametrize(
    "stderr",
    [
        "Host key verification failed.\n",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
        "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n",
        "Host key for 10.0.0.1 has changed and you have requested strict checking.\n",
    ],
)
def test_host_key_rejections_are_permanent(stderr):
    assert rejection_reason(stderr) == "host key verification failed"


@pytest.mark.parametrize(
    "stderr",
    [
        "/in/a.bin: Permission denied\n",  # the classic protocol: a file, not the login
        'dest open "/in/a.bin": Permission denied\n',  # its sftp mode
        "scp: /remote/a.bin: Permission denied\n",
        "ssh: connect to host h port 22: Connection refused\n",
        "ssh: connect to host h port 22: Connection timed out\n",
        "Connection reset by peer\n",
        "banner exchange: Connection to 10.0.0.1 port 22: invalid format\n",
        "",
    ],
)
def test_transient_and_file_level_failures_are_retried(stderr):
    assert rejection_reason(stderr) is None


def test_a_failed_command_carries_the_classification():
    permanent = CommandFailed(["ssh"], 255, "u@host: Permission denied (publickey,password).\n")
    assert permanent.permanent_reason == "authentication failed"
    transient = CommandFailed(["ssh"], 255, "ssh: connect to host h port 22: Connection refused\n")
    assert transient.permanent_reason is None


def test_a_transfer_rejection_is_a_transfer_failure_not_a_plain_command_failure():
    exc = TransferAuthFailed(["scp"], 255, "u@host: Permission denied (publickey).\n", "authentication failed")
    assert isinstance(exc, TransferFailed)
    assert not isinstance(exc, CommandFailed)
    assert exc.permanent_reason == "authentication failed"
    assert "Permission denied" in str(exc)
    assert exc.stderr.endswith("Permission denied (publickey).\n")
