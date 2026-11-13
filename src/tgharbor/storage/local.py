from __future__ import annotations

import asyncio
import json
import os
import stat
import unicodedata
from pathlib import Path
from uuid import UUID

from tgharbor.core.interfaces import StorageTarget
from tgharbor.core.jobs import DownloadJob
from tgharbor.core.library_api import PendingOrganization
from tgharbor.core.reconciliation import LegacyMove


class StorageSafetyError(RuntimeError):
    """Raised when a storage path is unsafe or has an unexpected type."""


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)
        self.inbox = self.root / "inbox"
        self.archive = self.root / "archive"
        self.partial = self.root / ".partial"

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def normalize_name(self, value: str) -> str:
        return sanitize_filename(value)

    async def prepare(self, job: DownloadJob, suggested_name: str) -> StorageTarget:
        return await asyncio.to_thread(self._prepare_sync, job, suggested_name)

    async def commit(self, target: StorageTarget) -> Path:
        return await asyncio.to_thread(self._commit_sync, target)

    async def discard(self, target: StorageTarget) -> None:
        await asyncio.to_thread(self._discard_sync, target)

    def is_complete(self, target: StorageTarget) -> bool:
        self._validate_target(target)
        return _read_state(_state_path(target)) == "committed" and _is_regular_file(
            target.final_path
        )

    def relative_path(self, path: Path) -> Path:
        _assert_beneath(path, self.root)
        return path.resolve(strict=False).relative_to(self.root)

    async def organize(
        self,
        source: Path,
        job_id: str,
        *,
        new_name: str | None = None,
        archive_directory: str | None = None,
        operator_id: int | None = None,
        action: str | None = None,
    ) -> Path:
        return await asyncio.to_thread(
            self._organize_sync,
            source,
            job_id,
            new_name,
            archive_directory,
            operator_id,
            action,
        )

    async def create_archive_directory(self, value: str) -> Path:
        return await asyncio.to_thread(self._create_archive_directory_sync, value)

    async def list_archive_directories(self) -> list[Path]:
        return await asyncio.to_thread(self._list_archive_directories_sync)

    async def move_legacy_output(self, job: DownloadJob) -> LegacyMove:
        return await asyncio.to_thread(self._move_legacy_output_sync, job)

    async def finish_legacy_move(self, move: LegacyMove) -> None:
        await asyncio.to_thread(self._finish_legacy_move_sync, move)

    async def available_legacy_name(self, desired_name: str, job_id: str) -> str:
        return await asyncio.to_thread(self._available_legacy_name_sync, desired_name, job_id)

    async def pending_organizations(self) -> list[PendingOrganization]:
        return await asyncio.to_thread(self._pending_organizations_sync)

    async def finish_organization(self, job_id: UUID) -> None:
        await asyncio.to_thread(self._finish_organization_sync, job_id)

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _require_directory(self.root, "download root")
        for path in (self.inbox, self.archive, self.partial):
            if path.exists() or path.is_symlink():
                _require_directory(path, path.name)
            else:
                path.mkdir(mode=0o750)

    def _prepare_sync(self, job: DownloadJob, suggested_name: str) -> StorageTarget:
        self._initialize_sync()
        job_directory = self.partial / str(job.id)
        _assert_beneath(job_directory, self.root)
        if job_directory.exists() or job_directory.is_symlink():
            _require_directory(job_directory, "job directory")
        else:
            job_directory.mkdir(mode=0o750)

        file_name = sanitize_filename(job.reserved_name or suggested_name)
        final_path = self.inbox / file_name
        temporary_path = job_directory / f".{file_name}.part"
        target = StorageTarget(temporary_path=temporary_path, final_path=final_path)
        self._validate_target(target)
        state_path = _state_path(target)
        state = _read_state(state_path)
        if state is None:
            if final_path.exists() or final_path.is_symlink():
                raise StorageSafetyError(f"unreserved destination already exists: {final_path}")
            _write_state(state_path, "reserved")
            state = "reserved"
        if state == "reserved":
            if final_path.exists() or final_path.is_symlink():
                raise StorageSafetyError(f"reserved destination unexpectedly exists: {final_path}")
            self._discard_sync(target)
        elif state == "committing":
            self._recover_committing(target, state_path)
        elif state == "committed":
            _require_regular_file(final_path, "completed download")
            self._discard_sync(target)
        else:
            raise StorageSafetyError(f"invalid storage state for job {job.id}")
        return target

    def _commit_sync(self, target: StorageTarget) -> Path:
        self._validate_target(target)
        state_path = _state_path(target)
        if _read_state(state_path) == "committed":
            _require_regular_file(target.final_path, "completed download")
            self._discard_sync(target)
            return target.final_path
        _require_regular_file(target.temporary_path, "temporary download")
        _fsync_file(target.temporary_path)
        if target.final_path.exists() or target.final_path.is_symlink():
            raise StorageSafetyError(f"destination already exists: {target.final_path}")
        _write_state(state_path, "committing")
        os.link(target.temporary_path, target.final_path, follow_symlinks=False)
        _fsync_file(target.final_path)
        target.temporary_path.unlink()
        _fsync_directory(target.final_path.parent)
        _fsync_directory(target.temporary_path.parent)
        _write_state(state_path, "committed")
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
            if path.parent.exists() or path.parent.is_symlink():
                _require_directory(path.parent, "job directory")
        if target.final_path.parent != self.inbox:
            raise StorageSafetyError("completed target must be inside inbox")
        if target.temporary_path.parent.parent != self.partial:
            raise StorageSafetyError("temporary target must be inside one partial job directory")

    def _recover_committing(self, target: StorageTarget, state_path: Path) -> None:
        final_exists = target.final_path.exists() or target.final_path.is_symlink()
        temporary_exists = target.temporary_path.exists() or target.temporary_path.is_symlink()
        if final_exists:
            _require_regular_file(target.final_path, "completed download")
            if temporary_exists:
                _require_regular_file(target.temporary_path, "temporary download")
                if not os.path.samestat(
                    target.final_path.stat(follow_symlinks=False),
                    target.temporary_path.stat(follow_symlinks=False),
                ):
                    raise StorageSafetyError("committing files do not reference the same data")
                target.temporary_path.unlink()
            _write_state(state_path, "committed")
            return
        if temporary_exists:
            _require_regular_file(target.temporary_path, "temporary download")
            target.temporary_path.unlink()
        _write_state(state_path, "reserved")

    def _organize_sync(
        self,
        source: Path,
        job_id: str,
        new_name: str | None,
        archive_directory: str | None,
        operator_id: int | None,
        action: str | None,
    ) -> Path:
        self._initialize_sync()
        state_path = self.partial / f"organize-{job_id}" / ".organize.json"
        if state_path.exists() or state_path.is_symlink():
            return self._recover_organization_state(state_path).relative_path
        source_path = self.root / source
        _assert_beneath(source_path, self.root)
        _require_regular_file(source_path, "stored file")
        destination_parent = self.inbox
        if archive_directory is not None:
            relative_directory = validate_relative_directory(archive_directory)
            destination_parent = self.archive.joinpath(*relative_directory.parts)
            _assert_beneath(destination_parent, self.archive)
            _require_directory(destination_parent, "archive directory")
        desired_name = sanitize_filename(new_name or source_path.name)
        destination = destination_parent / desired_name
        _assert_beneath(destination, self.root)
        if destination != source_path and (destination.exists() or destination.is_symlink()):
            name = Path(desired_name)
            destination = destination_parent / f"{name.stem}--{job_id[:8]}{name.suffix}"
        if destination != source_path and (destination.exists() or destination.is_symlink()):
            raise StorageSafetyError(f"destination already exists: {destination}")
        if destination == source_path:
            return self.relative_path(source_path)
        if (operator_id is None) != (action is None):
            raise ValueError("operator_id and action must be provided together")
        if operator_id is not None and action is not None:
            if operator_id <= 0:
                raise ValueError("operator_id must be positive")
            if action not in {"rename", "archive", "rename-and-archive"}:
                raise ValueError("unsupported organization action")
            state_path.parent.mkdir(mode=0o750)
            details = source_path.stat(follow_symlinks=False)
            _write_json_state(
                state_path,
                {
                    "job_id": str(UUID(job_id)),
                    "operator_id": operator_id,
                    "action": action,
                    "source": source_path.relative_to(self.root).as_posix(),
                    "destination": destination.relative_to(self.root).as_posix(),
                    "device": details.st_dev,
                    "inode": details.st_ino,
                    "size": details.st_size,
                },
            )
        os.replace(source_path, destination)
        _fsync_directory(source_path.parent)
        if destination.parent != source_path.parent:
            _fsync_directory(destination.parent)
        return self.relative_path(destination)

    def _pending_organizations_sync(self) -> list[PendingOrganization]:
        self._initialize_sync()
        pending: list[PendingOrganization] = []
        for state_path in sorted(self.partial.glob("organize-*/.organize.json")):
            pending.append(self._recover_organization_state(state_path))
        return pending

    def _recover_organization_state(self, state_path: Path) -> PendingOrganization:
        _assert_beneath(state_path, self.partial)
        payload = _read_json_state(state_path)
        try:
            job_id = UUID(str(payload["job_id"]))
            operator_id = int(payload["operator_id"])
            action = str(payload["action"])
            source_relative = Path(str(payload["source"]))
            destination_relative = Path(str(payload["destination"]))
        except (KeyError, TypeError, ValueError) as error:
            raise StorageSafetyError("organization state is invalid") from error
        if operator_id <= 0 or action not in {"rename", "archive", "rename-and-archive"}:
            raise StorageSafetyError("organization state contains an invalid operation")
        for path in (source_relative, destination_relative):
            if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                raise StorageSafetyError("organization state contains an unsafe path")
        source = self.root / source_relative
        destination = self.root / destination_relative
        _assert_beneath(source, self.root)
        _assert_beneath(destination, self.root)
        if destination_relative.parts[0] not in {"inbox", "archive"}:
            raise StorageSafetyError("organization destination is outside the library")
        _require_directory(destination.parent, "organization destination directory")
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists and destination_exists:
            raise StorageSafetyError("organization source and destination both exist")
        if not source_exists and not destination_exists:
            raise StorageSafetyError("organization source and destination are both missing")
        current = source if source_exists else destination
        _require_regular_file(current, "organization file")
        _require_fingerprint(current, payload)
        if source_exists:
            os.replace(source, destination)
            _fsync_directory(source.parent)
            if destination.parent != source.parent:
                _fsync_directory(destination.parent)
        return PendingOrganization(
            job_id=job_id,
            operator_id=operator_id,
            action=action,
            relative_path=destination_relative,
            output_path=destination,
        )

    def _finish_organization_sync(self, job_id: UUID) -> None:
        state_path = self.partial / f"organize-{job_id.hex}" / ".organize.json"
        if not state_path.exists() and not state_path.is_symlink():
            state_path = self.partial / f"organize-{job_id}" / ".organize.json"
        if state_path.exists() or state_path.is_symlink():
            _require_regular_file(state_path, "organization state")
            state_path.unlink()
            _fsync_directory(state_path.parent)
        try:
            state_path.parent.rmdir()
        except OSError:
            pass

    def _create_archive_directory_sync(self, value: str) -> Path:
        self._initialize_sync()
        relative = validate_relative_directory(value)
        current = self.archive
        for part in relative.parts:
            current = current / part
            _assert_beneath(current, self.archive)
            if current.exists() or current.is_symlink():
                _require_directory(current, "archive directory")
            else:
                current.mkdir(mode=0o750)
        return self.relative_path(current)

    def _list_archive_directories_sync(self) -> list[Path]:
        self._initialize_sync()
        directories: list[Path] = []
        for path in self.archive.rglob("*"):
            if path.is_symlink():
                raise StorageSafetyError(f"archive contains a symbolic link: {path}")
            if path.is_dir():
                directories.append(path.relative_to(self.archive))
        return sorted(directories, key=lambda item: item.as_posix().casefold())

    def _move_legacy_output_sync(self, job: DownloadJob) -> LegacyMove:
        self._initialize_sync()
        if job.output_path is None or job.reserved_name is None:
            raise StorageSafetyError("legacy job lacks an output path or reserved name")
        source = job.output_path
        if not source.is_absolute():
            source = self.root / source
        expected_directory = self.root / str(job.id)
        _assert_beneath(source, self.root)
        if source.parent != expected_directory:
            raise StorageSafetyError("legacy output is not inside its UUID directory")

        state_directory = self.partial / str(job.id)
        state_path = state_directory / ".legacy.json"
        if state_path.exists() or state_path.is_symlink():
            return self._recover_legacy_move(job, source, state_path)

        _require_directory(expected_directory, "legacy job directory")
        entries = list(expected_directory.iterdir())
        if entries != [source]:
            raise StorageSafetyError("legacy job directory is ambiguous")
        _require_regular_file(source, "legacy output")
        source_stat = source.stat(follow_symlinks=False)
        destination = self.inbox / sanitize_filename(job.reserved_name)
        _assert_beneath(destination, self.inbox)
        if destination.exists() or destination.is_symlink():
            raise StorageSafetyError(f"reserved legacy destination already exists: {destination}")

        if state_directory.exists() or state_directory.is_symlink():
            _require_directory(state_directory, "legacy state directory")
        else:
            state_directory.mkdir(mode=0o750)
        payload = {
            "job_id": str(job.id),
            "source": source.relative_to(self.root).as_posix(),
            "destination": destination.relative_to(self.root).as_posix(),
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
            "size": source_stat.st_size,
        }
        _write_json_state(state_path, payload)
        return self._apply_legacy_move(source, destination, state_path, payload)

    def _available_legacy_name_sync(self, desired_name: str, job_id: str) -> str:
        self._initialize_sync()
        desired = sanitize_filename(desired_name)
        destination = self.inbox / desired
        if not destination.exists() and not destination.is_symlink():
            return desired
        path = Path(desired)
        suffixed = f"{path.stem}--{job_id[:8]}{path.suffix}"
        destination = self.inbox / suffixed
        if destination.exists() or destination.is_symlink():
            raise StorageSafetyError("no collision-safe legacy destination is available")
        return suffixed

    def _recover_legacy_move(self, job: DownloadJob, source: Path, state_path: Path) -> LegacyMove:
        payload = _read_json_state(state_path)
        if payload.get("job_id") != str(job.id):
            raise StorageSafetyError("legacy move state belongs to another job")
        try:
            stored_source = Path(str(payload["source"]))
            stored_destination = Path(str(payload["destination"]))
            device = int(payload["device"])
            inode = int(payload["inode"])
            size = int(payload["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise StorageSafetyError("legacy move state is invalid") from error
        if stored_source.is_absolute() or stored_destination.is_absolute():
            raise StorageSafetyError("legacy move state contains an absolute path")
        if self.root / stored_source != source:
            raise StorageSafetyError("legacy move source changed after intent was recorded")
        destination = self.root / stored_destination
        if destination.parent != self.inbox or destination.name != job.reserved_name:
            raise StorageSafetyError("legacy move destination changed after intent was recorded")
        payload = {
            "job_id": str(job.id),
            "source": stored_source.as_posix(),
            "destination": stored_destination.as_posix(),
            "device": device,
            "inode": inode,
            "size": size,
        }
        return self._apply_legacy_move(source, destination, state_path, payload)

    def _apply_legacy_move(
        self, source: Path, destination: Path, state_path: Path, payload: dict[str, object]
    ) -> LegacyMove:
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists:
            _require_regular_file(source, "legacy output")
            _require_fingerprint(source, payload)
        if destination_exists:
            _require_regular_file(destination, "legacy destination")
            _require_fingerprint(destination, payload)
        if not source_exists and not destination_exists:
            raise StorageSafetyError("legacy move lost both source and destination")
        if not destination_exists:
            os.link(source, destination, follow_symlinks=False)
            _fsync_file(destination)
            _fsync_directory(destination.parent)
        if source_exists:
            if not os.path.samestat(
                source.stat(follow_symlinks=False), destination.stat(follow_symlinks=False)
            ):
                raise StorageSafetyError("legacy source and destination differ")
            source.unlink()
            _fsync_directory(source.parent)
        try:
            source.parent.rmdir()
        except OSError:
            pass
        return LegacyMove(
            source_path=source,
            relative_path=destination.relative_to(self.root),
            output_path=destination,
            size_bytes=int(payload["size"]),
            state_path=state_path,
        )

    def _finish_legacy_move_sync(self, move: LegacyMove) -> None:
        _assert_beneath(move.state_path, self.partial)
        if move.state_path.exists() or move.state_path.is_symlink():
            _require_regular_file(move.state_path, "legacy move state")
            move.state_path.unlink()
            _fsync_directory(move.state_path.parent)
        try:
            move.state_path.parent.rmdir()
        except OSError:
            pass


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


def validate_relative_directory(value: str) -> Path:
    normalized = unicodedata.normalize("NFC", value).strip()
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise StorageSafetyError("archive directory must be a safe relative path")
    path = Path(*parts)
    sanitized = Path(*(sanitize_filename(part) for part in parts))
    if sanitized.as_posix() != path.as_posix():
        raise StorageSafetyError("archive directory contains unsupported characters")
    return sanitized


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


def _state_path(target: StorageTarget) -> Path:
    return target.temporary_path.parent / ".state"


def _read_state(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    _require_regular_file(path, "storage state")
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise StorageSafetyError(f"cannot read storage state: {path}") from error


def _write_state(path: Path, state: str) -> None:
    temporary = path.with_name(".state.tmp")
    if temporary.exists() or temporary.is_symlink():
        _require_regular_file(temporary, "temporary storage state")
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, f"{state}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _read_json_state(path: Path) -> dict[str, object]:
    _require_regular_file(path, "legacy move state")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StorageSafetyError(f"cannot read legacy move state: {path}") from error
    if not isinstance(payload, dict):
        raise StorageSafetyError("legacy move state must be a JSON object")
    return payload


def _write_json_state(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(".legacy.tmp")
    if temporary.exists() or temporary.is_symlink():
        _require_regular_file(temporary, "temporary legacy move state")
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        content = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("ascii")
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _require_fingerprint(path: Path, payload: dict[str, object]) -> None:
    details = path.stat(follow_symlinks=False)
    expected = (int(payload["device"]), int(payload["inode"]), int(payload["size"]))
    actual = (details.st_dev, details.st_ino, details.st_size)
    if actual != expected:
        raise StorageSafetyError(f"legacy file changed after intent was recorded: {path}")


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
