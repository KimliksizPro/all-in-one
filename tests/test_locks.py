from __future__ import annotations

import os
import subprocess
import sys
import threading

from aio.locks import keyring_file_lock


def test_file_lock_blocks_other_thread_nonblocking(aio_home):
    results: list[bool] = []

    def contender() -> None:
        with keyring_file_lock(blocking=False) as acquired:
            results.append(acquired)

    with keyring_file_lock(blocking=True):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(2)
    assert results == [False]

    with keyring_file_lock(blocking=False) as acquired:
        assert acquired is True


def test_file_lock_blocks_other_process(aio_home):
    code = (
        "import os, sys, time\n"
        "from aio.locks import keyring_file_lock\n"
        "with keyring_file_lock(blocking=True):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(1.2)\n"
    )
    env = os.environ.copy()
    env["AIO_HOME"] = str(aio_home)
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(aio_home),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        assert line == "locked"
        with keyring_file_lock(blocking=False) as acquired:
            assert acquired is False
    finally:
        child.wait(timeout=5)
    with keyring_file_lock(blocking=False) as acquired:
        assert acquired is True
