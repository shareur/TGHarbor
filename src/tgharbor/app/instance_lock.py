from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO, Self


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another process owns the state-directory lock."""


class InstanceLock:
    def __init__(self, database_path: Path) -> None:
        self.path = Path(f"{database_path}.lock")
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("instance lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise InstanceAlreadyRunningError(
                f"another TGHarbor instance is using {self.path.parent}"
            ) from error
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write("locked\n")
        lock_file.flush()
        self._file = lock_file

    def release(self) -> None:
        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
