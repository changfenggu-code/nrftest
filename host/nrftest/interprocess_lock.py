from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO, final

DEFAULT_AUTOPTS_LOCK_PATH = Path(tempfile.gettempdir()) / "nrftest" / "autopts-session.lock"


class InterprocessLockError(RuntimeError):
    """Raised when another process owns an nrftest host resource."""


@final
class InterprocessFileLock:
    """Non-blocking, crash-released file lock shared by all Host processes."""

    def __init__(self, path: Path = DEFAULT_AUTOPTS_LOCK_PATH) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self._stream is not None:
            raise InterprocessLockError(f"interprocess lock is already held: {self._path}")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            stream = self._path.open("a+b", buffering=0)
            if stream.seek(0, os.SEEK_END) == 0:
                _ = stream.write(b"\0")
            stream.seek(0)
            self._lock(stream)
        except OSError as error:
            if "stream" in locals():
                stream.close()
            raise InterprocessLockError(
                f"another nrftest AutoPTS Host session may own {self._path}: {error}"
            ) from error
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.seek(0)
            self._unlock(stream)
        finally:
            stream.close()

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
