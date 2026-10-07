import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import uuid

from PySide6.QtWidgets import QApplication

from corewatch.gui import single


def paths(tmp_path) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    return str(tmp_path / "corewatch.lock"), str(tmp_path / f"cw-{uuid.uuid4().hex[:8]}.sock")


def app() -> None:
    QApplication.instance() or QApplication([])


def test_a_second_start_hands_over_to_the_running_one(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    app()
    lock_path, socket_path = paths(tmp_path)
    lock, answered = single.claim(lock_path, socket_path, show=True)
    assert lock is not None and not answered  # first one in
    shown = []
    server = single.InstanceServer(socket_path, lambda: shown.append(True))
    assert oct(os.stat(socket_path).st_mode & 0o077) == "0o0"  # no access for other users
    second, answered = single.claim(lock_path, socket_path, show=True)
    assert second is None and answered
    qtbot.waitUntil(lambda: shown == [True], timeout=2000)
    assert single.claim(lock_path, socket_path, show=False) == (None, True)  # a quiet start: running is enough
    qtbot.wait(100)
    assert shown == [True]
    server.close()
    lock.release()


def test_a_start_during_the_first_ones_startup_waits_for_it(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    app()
    lock_path, socket_path = paths(tmp_path)
    lock, _ = single.claim(lock_path, socket_path, show=True)  # first holds the lock, not listening yet
    shown: list[bool] = []
    servers = []

    def sleep(seconds: float) -> None:  # the first instance finishes starting while we wait
        if not servers:
            servers.append(single.InstanceServer(socket_path, lambda: shown.append(True)))

    second, answered = single.claim(lock_path, socket_path, show=True, wait_seconds=5, sleep=sleep)
    assert second is None and answered  # no second copy, even though nobody was listening at first
    qtbot.waitUntil(lambda: shown == [True], timeout=2000)
    servers[0].close()
    lock.release()  # type: ignore[union-attr]


def test_a_holder_that_never_answers_still_means_no_second_copy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app()
    lock_path, socket_path = paths(tmp_path)
    lock, _ = single.claim(lock_path, socket_path, show=True)
    assert single.claim(lock_path, socket_path, show=True, wait_seconds=0.2, sleep=lambda s: None) == (None, False)
    lock.release()  # type: ignore[union-attr]


def test_a_lock_left_by_a_crashed_corewatch_is_taken_over(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import subprocess
    import sys

    app()
    lock_path, socket_path = paths(tmp_path)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from PySide6.QtCore import QLockFile; import os, sys\n"
            "held = QLockFile(sys.argv[1]); held.tryLock(0)\n"
            "os._exit(0)  # die holding it, like a crash",
            lock_path,
        ],
        check=True,
    )
    assert os.path.exists(lock_path)
    lock, _ = single.claim(lock_path, socket_path, show=True)
    assert lock is not None  # its owner is gone, so the lock is stale and taken over
    lock.release()


def test_a_message_that_arrives_in_pieces_still_counts(qtbot, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from PySide6.QtNetwork import QLocalSocket

    app()
    _, socket_path = paths(tmp_path)
    shown = []
    server = single.InstanceServer(socket_path, lambda: shown.append(True))
    client = QLocalSocket()
    client.connectToServer(socket_path)
    assert client.waitForConnected(1000)
    client.write(b"sh")
    client.flush()
    qtbot.wait(50)
    client.write(b"ow")
    client.flush()
    qtbot.waitUntil(lambda: shown == [True], timeout=2000)
    client.disconnectFromServer()
    server.close()


def test_files_live_in_a_private_folder_of_this_user() -> None:
    from pathlib import Path

    lock_path, socket_path = single.default_paths()
    folder = Path(socket_path).parent
    assert Path(lock_path).parent == folder
    # $XDG_RUNTIME_DIR normally; Qt falls back to its own private folder if that's unusable.
    if folder == Path("/tmp"):  # no private folder at all: names carry the user id
        assert socket_path.endswith(f"corewatch-{os.getuid()}.sock")
    else:  # $XDG_RUNTIME_DIR, or Qt's own /tmp/runtime-<user> fallback
        info = folder.stat()
        assert info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
