import json
import os
from pathlib import Path
import sqlite3
import tempfile

from alembic import command
from alembic.config import Config


def _database_file() -> Path:
    descriptor, name = tempfile.mkstemp(suffix=".sqlite")
    os.close(descriptor)
    Path(name).unlink()
    return Path(name)


def _upgrade(monkeypatch, database: Path, revision: str) -> None:
    monkeypatch.setenv(
        "TRACKER_API_TOKEN", "migration-test-token-migration-test-token"
    )
    monkeypatch.setenv(
        "TRACKER_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    command.upgrade(Config("alembic.ini"), revision)


def test_empty_database_migrates_to_head(monkeypatch):
    database = _database_file()
    try:
        _upgrade(monkeypatch, database, "head")
        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(listings)")
            }
        assert revision == ("6a26ec39bb13",)
        assert {"image_urls_json", "consecutive_misses", "reactivated_at"} <= columns
    finally:
        database.unlink(missing_ok=True)


def test_populated_revision_001_is_backfilled(monkeypatch):
    database = _database_file()
    try:
        _upgrade(monkeypatch, database, "001")
        now = "2026-08-01 12:00:00"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO listings (id, source, external_id, title, description, url, "
                "image_url, price, currency, condition, seller, seller_location, "
                "published_at, first_seen_at, last_seen_at, is_active) "
                "VALUES (1, 'ebay', 'item-1', 'Title', 'Description', 'https://item', "
                "'https://image', 100, 'eur', 'used', 'seller', 'DE', ?, ?, ?, 1)",
                (now, now, now),
            )
            connection.execute(
                "INSERT INTO listing_snapshots (id, listing_id, title, description, price, "
                "currency, condition, seller, seller_location, image_url, captured_at) "
                "VALUES (1, 1, 'Title', 'Description', 100, 'eur', 'used', 'seller', "
                "'DE', 'https://image', ?)",
                (now,),
            )
            for match_id in (1, 2):
                connection.execute(
                    "INSERT INTO part_matches (id, listing_id, part_id, part_name, "
                    "total_score, reasons_json, algorithm_version, matched_at) "
                    "VALUES (?, 1, 'front-lip', 'Front lip', 100, '[]', '1.0', ?)",
                    (match_id, now),
                )
            connection.execute(
                "INSERT INTO notifications (id, listing_id, match_id, alert_type, "
                "payload_json, sent_at, success) "
                "VALUES (1, 1, 1, 'new_listing', '{}', ?, 1)",
                (now,),
            )
            connection.commit()

        _upgrade(monkeypatch, database, "head")
        with sqlite3.connect(database) as connection:
            listing = connection.execute(
                "SELECT image_urls_json, consecutive_misses FROM listings WHERE id=1"
            ).fetchone()
            snapshot = connection.execute(
                "SELECT schema_version, payload_hash, image_urls_json "
                "FROM listing_snapshots WHERE id=1"
            ).fetchone()
            notification = connection.execute(
                "SELECT event_key, match_id FROM notifications WHERE id=1"
            ).fetchone()
            matches = connection.execute(
                "SELECT id, compatibility_status FROM part_matches WHERE listing_id=1"
            ).fetchall()

        assert json.loads(listing[0]) == ["https://image"]
        assert listing[1] == 0
        assert snapshot[0] == 1
        assert len(snapshot[1]) == 64
        assert json.loads(snapshot[2]) == ["https://image"]
        assert notification == ("legacy:1", 2)
        assert matches == [(2, "unknown")]
    finally:
        database.unlink(missing_ok=True)
