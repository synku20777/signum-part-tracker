from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from irmscher_tracker import __version__

_FORMAT_VERSION = 1
_CONTENT_NAME = "tracker.sqlite"
_MANIFEST_NAME = "manifest.json"
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024


class BackupError(ValueError):
    pass


def sqlite_backup(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as source_db,
        closing(sqlite3.connect(destination)) as target_db,
    ):
        source_db.backup(target_db)


def create_backup(database: Path, data_directory: Path, destination: Path) -> None:
    if destination.suffix == ".sqlite":
        sqlite_backup(database, destination)
        return
    if not destination.name.endswith(".tar.gz"):
        raise BackupError("Backup filename must end with .tar.gz or .sqlite")
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=data_directory) as temporary_name:
        temporary = Path(temporary_name)
        database_copy = temporary / _CONTENT_NAME
        sqlite_backup(database, database_copy)
        reference_files = _reference_files(data_directory / "references")
        manifest = {
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "application_version": __version__,
            "database_sha256": _sha256_file(database_copy),
            "reference_file_count": len(reference_files),
        }
        descriptor, archive_name = tempfile.mkstemp(
            prefix=".backup-", suffix=".tmp", dir=data_directory
        )
        os.close(descriptor)
        archive = Path(archive_name)
        try:
            with tarfile.open(archive, "w:gz") as bundle:
                manifest_bytes = json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
                info = tarfile.TarInfo(_MANIFEST_NAME)
                info.size = len(manifest_bytes)
                info.mtime = int(time.time())
                bundle.addfile(info, io.BytesIO(manifest_bytes))
                bundle.add(database_copy, arcname=_CONTENT_NAME, recursive=False)
                for file in reference_files:
                    bundle.add(
                        file,
                        arcname=(
                            Path("references") / file.relative_to(data_directory / "references")
                        ).as_posix(),
                        recursive=False,
                    )
            os.replace(archive, destination)
        finally:
            archive.unlink(missing_ok=True)


def restore_backup(source: Path, database: Path, data_directory: Path) -> None:
    if source.suffix == ".sqlite":
        _validate_database(source)
        sqlite_backup(source, database)
        return
    if not source.name.endswith(".tar.gz"):
        raise BackupError("Restore file must end with .tar.gz or .sqlite")
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=data_directory) as temporary_name:
        temporary = Path(temporary_name)
        _extract_validated(source, temporary)
        restored_database = temporary / _CONTENT_NAME
        restored_references = temporary / "references"
        _validate_database(restored_database)

        current_references = data_directory / "references"
        previous_references = data_directory / f".references-pre-restore-{uuid4().hex}"
        if current_references.exists():
            os.replace(current_references, previous_references)
        try:
            if restored_references.exists():
                os.replace(restored_references, current_references)
            else:
                current_references.mkdir()
            sqlite_backup(restored_database, database)
        except Exception:
            if current_references.exists():
                shutil.rmtree(current_references)
            if previous_references.exists():
                os.replace(previous_references, current_references)
            raise
        finally:
            if previous_references.exists():
                shutil.rmtree(previous_references)


def audit_reference_storage(database: Path, data_directory: Path) -> tuple[int, int]:
    references = data_directory / "references"
    removed = 0
    if references.exists():
        cutoff = time.time() - 3600
        for temporary in references.rglob(".reference-*.tmp"):
            if temporary.is_file() and temporary.stat().st_mtime < cutoff:
                temporary.unlink()
                removed += 1
    if not database.exists():
        return 0, removed
    with closing(sqlite3.connect(database)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_images'"
        ).fetchone()
        if table is None:
            return 0, removed
        paths = [row[0] for row in connection.execute("SELECT local_path FROM reference_images")]
    root = (data_directory / "references").resolve()
    missing = 0
    for value in paths:
        path = (data_directory / value).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            missing += 1
    return missing, removed


def _reference_files(reference_directory: Path) -> list[Path]:
    if not reference_directory.exists():
        return []
    files: list[Path] = []
    for path in sorted(reference_directory.rglob("*.webp")):
        relative = path.relative_to(reference_directory)
        if len(relative.parts) != 2 or path.stem != _sha256_file(path):
            raise BackupError("Reference storage contains an invalid content-addressed file")
        files.append(path)
    return files


def _extract_validated(source: Path, destination: Path) -> None:
    with tarfile.open(source, "r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) > _MAX_ARCHIVE_MEMBERS or sum(member.size for member in members) > (
            _MAX_ARCHIVE_BYTES
        ):
            raise BackupError("Backup archive exceeds safety limits")
        names: set[str] = set()
        reference_members: list[tarfile.TarInfo] = []
        for member in members:
            path = PurePosixPath(member.name)
            if (
                member.name in names
                or "\\" in member.name
                or path.is_absolute()
                or ".." in path.parts
                or member.issym()
                or member.islnk()
            ):
                raise BackupError("Backup archive contains an unsafe path")
            names.add(member.name)
            if member.isdir():
                if not path.parts or path.parts[0] != "references" or len(path.parts) > 2:
                    raise BackupError("Backup archive contains an unexpected directory")
                continue
            if not member.isfile():
                raise BackupError("Backup archive contains an unsupported entry")
            if member.name in {_MANIFEST_NAME, _CONTENT_NAME}:
                continue
            if (
                len(path.parts) != 3
                or path.parts[0] != "references"
                or path.suffix != ".webp"
                or len(path.stem) != 64
                or any(character not in "0123456789abcdef" for character in path.stem)
            ):
                raise BackupError("Backup archive contains an unexpected file")
            reference_members.append(member)
        if {_MANIFEST_NAME, _CONTENT_NAME} - names:
            raise BackupError("Backup archive is incomplete")

        manifest = _read_json_member(bundle, _MANIFEST_NAME)
        _validate_manifest(manifest, len(reference_members))
        for member in members:
            if not member.isfile():
                continue
            target = destination / PurePosixPath(member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = bundle.extractfile(member)
            if source_file is None:
                raise BackupError("Backup archive entry cannot be read")
            with target.open("wb") as output:
                shutil.copyfileobj(source_file, output)

    if _sha256_file(destination / _CONTENT_NAME) != manifest["database_sha256"]:
        raise BackupError("Backup database hash does not match its manifest")
    for member in reference_members:
        extracted_path = destination / PurePosixPath(member.name)
        if _sha256_file(extracted_path) != extracted_path.stem:
            raise BackupError("Reference image hash does not match its filename")


def _read_json_member(bundle: tarfile.TarFile, name: str) -> dict[str, Any]:
    member = bundle.getmember(name)
    if member.size > 64 * 1024:
        raise BackupError("Backup manifest is too large")
    handle = bundle.extractfile(member)
    if handle is None:
        raise BackupError("Backup manifest cannot be read")
    try:
        value = json.loads(handle.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupError("Backup manifest is invalid") from exc
    if not isinstance(value, dict):
        raise BackupError("Backup manifest is invalid")
    return value


def _validate_manifest(manifest: dict[str, Any], reference_count: int) -> None:
    created_at = manifest.get("created_at")
    database_hash = manifest.get("database_sha256")
    try:
        created = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError:
        created = None
    offset = created.utcoffset() if created is not None else None
    if (
        manifest.get("format_version") != _FORMAT_VERSION
        or created is None
        or offset is None
        or offset.total_seconds() != 0
        or not isinstance(manifest.get("application_version"), str)
        or not isinstance(database_hash, str)
        or len(database_hash) != 64
        or any(character not in "0123456789abcdef" for character in database_hash)
        or manifest.get("reference_file_count") != reference_count
    ):
        raise BackupError("Backup manifest is invalid")


def _validate_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError("Backup database is invalid") from exc
    if result != ("ok",):
        raise BackupError("Backup database failed its integrity check")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
