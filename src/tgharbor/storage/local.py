from __future__ import annotations

import asyncio
import os
import stat
import unicodedata
from pathlib import Path

from tgharbor.core.interfaces import StorageTarget
from tgharbor.core.jobs import DownloadJob


class StorageSafetyError(RuntimeError):
    """Raised when a storage path is unsafe or has an unexpected type."""


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def prepare(self, job: DownloadJob, suggested_name: str) -> StorageTarget:
        return await asyncio.to_thread(self._prepare_sync, job, suggested_name)

    async def commit(self, target: StorageTarget) -> Path:
        return await asyncio.to_thread(self._commit_sync, target)

    async def discard(self, target: StorageTarget) -> None:
        await asyncio.to_thread(self._discard_sync, target)

    def is_complete(self, target: StorageTarget) -> bool:
        self._validate_target(target)
        return _is_regular_file(target.final_path)

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _require_directory(self.root, "download root")

    def _prepare_sync(self, job: DownloadJob, suggested_name: str) -> StorageTarget:
        self._initialize_sync()
        job_directory = self.root / str(job.id)
        _assert_beneath(job_directory, self.root)
        if job_directory.exists() or job_directory.is_symlink():
            _require_directory(job_directory, "job directory")
        else:
            job_directory.mkdir(mode=0o750)

        final_path = job_directory / sanitize_filename(suggested_name)
        temporary_path = job_directory / f".{final_path.name}.part"
        target = StorageTarget(temporary_path=temporary_path, final_path=final_path)
        self._validate_target(target)

        if final_path.exists() or final_path.is_symlink():
            _require_regular_file(final_path, "completed download")
            self._discard_sync(target)
            return target
        if temporary_path.exists() or temporary_path.is_symlink():
            _require_regular_file(temporary_path, "temporary download")
            temporary_path.unlink()
        return target

    def _commit_sync(self, target: StorageTarget) -> Path:
        self._validate_target(target)
        if target.final_path.exists() or target.final_path.is_symlink():
            _require_regular_file(target.final_path, "completed download")
            self._discard_sync(target)
            return target.final_path
        _require_regular_file(target.temporary_path, "temporary download")
        _fsync_file(target.temporary_path)
        os.replace(target.temporary_path, target.final_path)
        _fsync_directory(target.final_path.parent)
        return target.final_path

    def _discard_sync(self, target: StorageTarget) -> None:
        self._validate_target(target)
        if not target.temporary_path.exists() and not target.temporary_path.is_symlink():
            return
        _require_regular_file(target.temporary_path, "temporary download")
        target.temporary_path.unlink()

    def _validate_target(self, target: StorageTarget) -> None:
        for path in (target.temporary_path, target.final_path):
            _assert_beneath(path, self.root)
            if path.parent.parent != self.root:
                raise StorageSafetyError("storage target must be inside one job directory")
            if path.parent.exists() or path.parent.is_symlink():
                _require_directory(path.parent, "job directory")


def sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    cleaned = "".join(
        "_"
        if character in {"/", "\\"} or unicodedata.category(character).startswith("C")
        else character
        for character in normalized
    ).strip(" .")
    if cleaned in {"", ".", ".."} or not any(character.isalnum() for character in cleaned):
        cleaned = "download.bin"
    return _truncate_utf8(cleaned, 180)


def _truncate_utf8(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) <= limit:
        return value
    suffix = Path(value).suffix
    if len(suffix.encode("utf-8")) > 24:
        suffix = ""
    budget = limit - len(suffix.encode("utf-8"))
    stem = value[: -len(suffix)] if suffix else value
    while stem and len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return f"{stem}{suffix}" or "download.bin"


def _assert_beneath(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as error:
        raise StorageSafetyError(f"storage path escapes download root: {path}") from error


def _require_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise StorageSafetyError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorageSafetyError(f"{label} is not a real directory: {path}")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise StorageSafetyError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise StorageSafetyError(f"{label} is not a regular file: {path}")


def _is_regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise StorageSafetyError(f"completed download is a symbolic link: {path}")
    return stat.S_ISREG(mode)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
