"""An scp server for tests (asyncssh, in a background thread): password and key auth, a pinned host key, one root."""

import asyncio
import getpass
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, cast

import asyncssh

USER = "u"
PASSWORD = "pa ss'\"$word"


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == USER and password == PASSWORD

    def public_key_auth_supported(self):
        return True


class _CountingSftpServer(asyncssh.SFTPServer):
    """The SFTP server the harness serves, remembering where every read started.

    A resumed transfer's first read is at the partial file's size; a restart starts at zero, so a test can
    tell them apart (see `resume` in the pull recipe).
    """

    def __init__(self, channel, *, chroot: str, reads: list[int]) -> None:
        # asyncssh annotates chroot as bytes; the harness has always passed str(self.root) and it works
        super().__init__(channel, chroot=cast(Any, chroot))
        self._reads = reads

    def read(self, file_obj, offset, size):
        self._reads.append(offset)
        return super().read(file_obj, offset, size)


class ScpServer:
    def __init__(self, root: Path, workdir: Path, chroot: bool = True) -> None:
        self.root = root
        self.chroot = chroot
        self.reads: list[int] = []  # every SFTP read offset served (see _CountingSftpServer)
        self.client_key = workdir / "client_key"
        self.known_hosts = workdir / "known_hosts"
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._server = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "ScpServer":
        self._thread.start()
        if not self._ready.wait(60):
            raise RuntimeError("the test scp server did not start")
        if self._error is not None:
            raise self._error
        return self

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._listen())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _listen(self) -> None:
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key.write_private_key(str(self.client_key))
        if os.name == "nt":
            # Windows OpenSSH ignores a private key that other accounts can read ("bad permissions").
            # pytest's temp dirs also give files an explicit OWNER RIGHTS entry, which OpenSSH rejects too.
            for change in (["/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"], ["/remove:g", "*S-1-3-4"]):
                subprocess.run(["icacls", str(self.client_key), *change], check=True, capture_output=True)
        else:
            self.client_key.chmod(0o600)
        authorized = asyncssh.import_authorized_keys(client_key.export_public_key().decode())
        self._server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            server_factory=_Server,
            authorized_client_keys=authorized,
            allow_scp=True,
            sftp_factory=(
                (lambda channel: _CountingSftpServer(channel, chroot=str(self.root), reads=self.reads))
                if self.chroot
                else asyncssh.SFTPServer
            ),
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.port} {host_key.export_public_key().decode().strip()}\n", encoding="utf-8"
        )

    def stop(self) -> None:
        def close() -> None:
            if self._server is not None:
                self._server.close()
            self._loop.stop()

        self._loop.call_soon_threadsafe(close)
        self._thread.join(10)
