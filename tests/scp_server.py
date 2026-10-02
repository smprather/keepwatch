"""An scp server for tests (asyncssh, in a background thread): password and key auth, a pinned host key, one root."""

import asyncio
import getpass
import os
import subprocess
import threading
from pathlib import Path

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


class ScpServer:
    def __init__(self, root: Path, workdir: Path, chroot: bool = True) -> None:
        self.root = root
        self.chroot = chroot
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
            subprocess.run(
                ["icacls", str(self.client_key), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                check=True,
                capture_output=True,
            )
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
                (lambda channel: asyncssh.SFTPServer(channel, chroot=str(self.root)))
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
            self._server.close()
            self._loop.stop()

        self._loop.call_soon_threadsafe(close)
        self._thread.join(10)
