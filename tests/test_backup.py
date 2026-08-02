from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from irmscher_tracker.services.backup import BackupError, create_backup, restore_backup


def test_full_and_legacy_backup_restore(tmp_path: Path):
    database = tmp_path / "tracker.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state (value TEXT)")
        connection.execute("INSERT INTO state VALUES ('before')")
    content = b"sanitized-webp"
    digest = hashlib.sha256(content).hexdigest()
    reference = tmp_path / "references" / "roof-spoiler" / f"{digest}.webp"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(content)

    archive = tmp_path / "full.tar.gz"
    create_backup(database, tmp_path, archive)
    with tarfile.open(archive) as bundle:
        assert {"manifest.json", "tracker.sqlite"} <= set(bundle.getnames())
        manifest = json.load(bundle.extractfile("manifest.json"))  # type: ignore[arg-type]
        assert manifest["reference_file_count"] == 1

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE state SET value='after'")
    reference.unlink()
    restore_backup(archive, database, tmp_path)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("before",)
    assert reference.read_bytes() == content

    legacy = tmp_path / "legacy.sqlite"
    create_backup(database, tmp_path, legacy)
    reference.write_bytes(b"keep-current-references")
    restore_backup(legacy, database, tmp_path)
    assert reference.read_bytes() == b"keep-current-references"


def test_restore_rejects_archive_path_traversal(tmp_path: Path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        payload = b"bad"
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    database = tmp_path / "tracker.db"
    with sqlite3.connect(database):
        pass
    with pytest.raises(BackupError):
        restore_backup(archive, database, tmp_path)
