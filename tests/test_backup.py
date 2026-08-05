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
    evaluation = tmp_path / "vision" / "evaluations" / "20260803_120000.json"
    evaluation.parent.mkdir(parents=True)
    evaluation.write_text('{"top_1_accuracy":0.5}\n', encoding="utf-8")
    model_cache = tmp_path / "models" / "huggingface" / "model.safetensors"
    model_cache.parent.mkdir(parents=True)
    model_cache.write_bytes(b"reproducible-model-cache")

    archive = tmp_path / "full.tar.gz"
    create_backup(database, tmp_path, archive)
    with tarfile.open(archive) as bundle:
        assert {
            "manifest.json",
            "tracker.sqlite",
            "vision/evaluations/20260803_120000.json",
        } <= set(bundle.getnames())
        assert not any(name.startswith("models/") for name in bundle.getnames())
        manifest = json.load(bundle.extractfile("manifest.json"))  # type: ignore[arg-type]
        assert manifest["reference_file_count"] == 1
        assert manifest["vision_evaluation_file_count"] == 1
        assert (
            manifest["vision_evaluation_sha256"][evaluation.name]
            == hashlib.sha256(evaluation.read_bytes()).hexdigest()
        )

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE state SET value='after'")
    reference.unlink()
    evaluation.unlink()
    restore_backup(archive, database, tmp_path)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("before",)
    assert reference.read_bytes() == content
    assert evaluation.read_text(encoding="utf-8") == '{"top_1_accuracy":0.5}\n'

    legacy = tmp_path / "legacy.sqlite"
    create_backup(database, tmp_path, legacy)
    reference.write_bytes(b"keep-current-references")
    evaluation.write_text("keep-current-evaluation", encoding="utf-8")
    restore_backup(legacy, database, tmp_path)
    assert reference.read_bytes() == b"keep-current-references"
    assert evaluation.read_text(encoding="utf-8") == "keep-current-evaluation"


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
