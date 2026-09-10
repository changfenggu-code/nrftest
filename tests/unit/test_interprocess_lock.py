import subprocess
import sys
from pathlib import Path

import pytest

from host.nrftest.interprocess_lock import InterprocessFileLock, InterprocessLockError


def test_interprocess_lock_rejects_conflict_and_can_be_reacquired(tmp_path: Path) -> None:
    path = tmp_path / "autopts.lock"
    first = InterprocessFileLock(path)
    second = InterprocessFileLock(path)

    first.acquire()
    try:
        with pytest.raises(InterprocessLockError, match="another nrftest AutoPTS Host session"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
    assert path.is_file()


def test_interprocess_lock_rejects_another_python_process(tmp_path: Path) -> None:
    path = tmp_path / "autopts.lock"
    first = InterprocessFileLock(path)
    script = (
        "from pathlib import Path; "
        "from host.nrftest.interprocess_lock import InterprocessFileLock; "
        f"InterprocessFileLock(Path({str(path)!r})).acquire()"
    )

    first.acquire()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    finally:
        first.release()

    assert result.returncode != 0
    assert "another nrftest AutoPTS Host session" in result.stderr
