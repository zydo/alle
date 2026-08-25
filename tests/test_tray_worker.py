from __future__ import annotations

import threading
import time

from alle.tray import CoalescingWorker


def test_worker_callbacks_return_immediately_and_pending_work_coalesces():
    gate = threading.Event()
    started = threading.Event()
    ran = []
    delivered = []
    worker = CoalescingWorker(lambda callback: callback())

    def slow():
        started.set()
        gate.wait(2)
        ran.append("slow")
        return "slow"

    before = time.monotonic()
    worker.submit(slow, lambda ok, value: delivered.append(value))
    assert time.monotonic() - before < 0.1
    assert started.wait(1)
    worker.submit(lambda: ran.append("old") or "old", lambda *_: None)
    worker.submit(lambda: ran.append("new") or "new", lambda ok, v: delivered.append(v))
    gate.set()

    deadline = time.monotonic() + 2
    while "new" not in delivered and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.close()
    assert ran == ["slow", "new"]
    assert delivered == ["new"]  # stale slow completion was discarded


def test_final_cleanup_wait_is_bounded():
    worker = CoalescingWorker(lambda callback: callback())
    gate = threading.Event()
    before = time.monotonic()
    assert worker.finish(lambda: gate.wait(2), timeout=0.02) is False
    assert time.monotonic() - before < 0.2
    gate.set()
    worker.close()
