"""One corewatch per user: a second start hands over to the running one and exits.

Without this, opening corewatch from the app menu while it already sits in the tray (say,
after starting at login) would run a second copy: two sets of tray icons, twice the
sensor polling.

A lock file decides who is first, taken before anything slow happens, so two starts a
moment apart can't both win. The first instance then listens on a local socket; later
ones connect, say whether they want the window shown, and quit. Both files live in the
user's private runtime folder (``$XDG_RUNTIME_DIR``, cleared at logout), not shared /tmp.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SHOW = b"show"
QUIET = b"quiet"  # a --minimized start while already running: being there is enough
CONNECT_MS = 500
# How long a later start keeps knocking while the first one is still starting up.
HAND_OFF_SECONDS = 10.0


def runtime_dir() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    return Path(location) if location else Path("/tmp")


def default_paths() -> tuple[str, str]:
    """(lock file, socket) for this user."""
    folder = runtime_dir()
    suffix = "" if folder != Path("/tmp") else f"-{os.getuid()}"  # /tmp is shared: keep users apart
    return str(folder / f"corewatch{suffix}.lock"), str(folder / f"corewatch{suffix}.sock")


def hand_off(socket_path: str, show: bool) -> bool:
    """True if a running corewatch took the message (and will show itself if ``show``)."""
    socket = QLocalSocket()
    socket.connectToServer(socket_path)
    if not socket.waitForConnected(CONNECT_MS):
        return False
    socket.write(SHOW if show else QUIET)
    socket.flush()
    socket.waitForBytesWritten(CONNECT_MS)
    socket.disconnectFromServer()
    return True


class InstanceLock:
    """Held for the whole life of the first instance."""

    def __init__(self, lock_path: str) -> None:
        self.lock = QLockFile(lock_path)
        self.lock.setStaleLockTime(0)  # only stale when its owner has died, never by age

    def acquire(self) -> bool:
        return self.lock.tryLock(0)

    def release(self) -> None:
        self.lock.unlock()


def claim(
    lock_path: str,
    socket_path: str,
    show: bool,
    wait_seconds: float = HAND_OFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[InstanceLock | None, bool]:
    """(lock, answered). The lock if this is the first instance. Otherwise another one is
    running: ``answered`` says whether it took the message. If it holds the lock but never
    answers, this start still gives up rather than run a second copy alongside it."""
    lock = InstanceLock(lock_path)
    if lock.acquire():
        return lock, False
    deadline = time.monotonic() + wait_seconds
    while not hand_off(socket_path, show):
        if time.monotonic() >= deadline:
            return None, False
        sleep(0.1)  # it's still starting up and not listening yet
    return None, True


class InstanceServer(QObject):
    """Listens for later starts and calls ``on_show`` when one asks for the window.

    Only created while holding the instance lock, so a socket already at this path can
    only be left over from a corewatch that died, and is safe to replace.
    """

    def __init__(self, socket_path: str, on_show: Callable[[], None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.on_show = on_show
        self.server = QLocalServer(self)
        # Only this user's own programs may talk to it; it only ever acts on "show" anyway.
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        if not self.server.listen(socket_path):
            QLocalServer.removeServer(socket_path)
            self.server.listen(socket_path)
        self._buffers: dict[QLocalSocket, bytes] = {}

    @property
    def listening(self) -> bool:
        return self.server.isListening()

    def _accept(self) -> None:
        while (connection := self.server.nextPendingConnection()) is not None:
            self._buffers[connection] = b""
            connection.readyRead.connect(lambda c=connection: self._read(c))
            connection.disconnected.connect(lambda c=connection: self._finish(c))
            self._read(connection)

    def _read(self, connection: QLocalSocket) -> None:
        # The message may arrive in pieces; act once it's complete, or when the sender hangs up.
        buffer = self._buffers.get(connection, b"") + bytes(connection.readAll().data())
        self._buffers[connection] = buffer
        if buffer in (SHOW, QUIET):
            self._finish(connection)

    def _finish(self, connection: QLocalSocket) -> None:
        if connection not in self._buffers:
            return
        buffer = self._buffers.pop(connection) + bytes(connection.readAll().data())
        if buffer.strip() == SHOW:
            self.on_show()
        connection.deleteLater()

    def close(self) -> None:
        self.server.close()
