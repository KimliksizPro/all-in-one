"""Süreçler arası keyring geçiş kilidi.

UI sunucusu, `aio watch` ve terminal komutları aynı canlı keyring'i
paylaşır. Bir süreç keyring'i geçici olarak başka profile yazarken diğer
süreçlerin ara token'ı canlı hesap sanmaması için `flock` kullanılır.

Kilit sırası her yerde aynıdır: önce süreç içi `KEYRING_LOCK`, sonra bu
dosya kilidi. Böylece thread'ler arası ve süreçler arası deadlock oluşmaz.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux hedef
    fcntl = None

from . import storage

_THREAD_GUARD = threading.RLock()
_LOCAL = threading.local()


@contextmanager
def keyring_file_lock(blocking: bool = True) -> Iterator[bool]:
    """Keyring geçiş kilidi. `blocking=False` ise (acquired, ...) döndürür."""
    depth = int(getattr(_LOCAL, "depth", 0))
    if depth > 0:
        _LOCAL.depth = depth + 1
        try:
            yield True
        finally:
            _LOCAL.depth = depth
        return

    if blocking:
        _THREAD_GUARD.acquire()
    else:
        if not _THREAD_GUARD.acquire(blocking=False):
            yield False
            return

    fd: int | None = None
    acquired_file = False
    try:
        storage.ensure_dirs()
        lock_path = storage.root_dir() / "keyring.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if fcntl is not None:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
                acquired_file = True
            except BlockingIOError:
                yield False
                return
            except OSError:
                # Dosya kilidi desteklenmiyorsa süreç içi kilit yeterli.
                acquired_file = True
        else:
            acquired_file = True
        _LOCAL.depth = 1
        try:
            yield True
        finally:
            _LOCAL.depth = 0
            if acquired_file and fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _THREAD_GUARD.release()
