import threading

import pytest

from keepwatch.locks import LockBusy, hold_lock


def test_second_nonblocking_lock_is_busy(tmp_path):
    lock = tmp_path / "locks" / "w.lock"
    with hold_lock(lock):
        with pytest.raises(LockBusy):
            with hold_lock(lock, blocking=False):
                pass
    with hold_lock(lock, blocking=False):
        pass


def test_on_wait_is_called_when_blocked(tmp_path):
    lock = tmp_path / "w.lock"
    ready = threading.Event()
    release = threading.Event()

    def holder():
        with hold_lock(lock):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    ready.wait(5)
    threading.Timer(0.2, release.set).start()
    waited = []
    with hold_lock(lock, on_wait=lambda: waited.append(True)):
        pass
    thread.join()
    assert waited == [True]
