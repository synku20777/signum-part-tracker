import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config


def _database_file(label: str) -> Path:
    directory = Path(__file__).resolve().parents[1] / ".pytest-tmp"
    directory.mkdir(exist_ok=True)
    return directory / f"{label}-{uuid4().hex}.sqlite"


def _upgrade(monkeypatch, database: Path, revision: str) -> None:
    monkeypatch.setenv("TRACKER_API_TOKEN", "migration-test-token-migration-test-token")
    monkeypatch.setenv("TRACKER_DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    command.upgrade(Config("alembic.ini"), revision)


def test_empty_database_migrates_to_head(monkeypatch):
    database = _database_file("empty")
    try:
        _upgrade(monkeypatch, database, "head")
        with closing(sqlite3.connect(database)) as connection:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info(listings)")}
            deletion_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='ebay_deletion_notifications'"
            ).fetchone()
        assert revision == ("f1a3c5e7b9d2",)
        assert {
            "image_urls_json",
            "consecutive_misses",
            "reactivated_at",
            "source_metadata_json",
            "rss_fingerprint_seen",
            "rss_fingerprint_enriched",
            "last_detail_success_at",
            "detail_status",
            "seller_display",
            "seller_identifier",
            "seller_anonymized_at",
        } <= columns.keys()
        assert columns["price"][3] == 0
        assert deletion_table == ("ebay_deletion_notifications",)
        with closing(sqlite3.connect(database)) as connection:
            review_tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"listing_images", "manual_reviews", "reference_images"} <= review_tables
    finally:
        database.unlink(missing_ok=True)


def test_populated_revision_001_is_backfilled(monkeypatch):
    database = _database_file("populated-001")
    try:
        _upgrade(monkeypatch, database, "001")
        now = "2026-08-01 12:00:00"
        with closing(sqlite3.connect(database)) as connection:
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
        with closing(sqlite3.connect(database)) as connection:
            listing = connection.execute(
                "SELECT image_urls_json, consecutive_misses, source_metadata_json, "
                "detail_status FROM listings WHERE id=1"
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
            seller = connection.execute(
                "SELECT seller_display, seller_identifier FROM listings WHERE id=1"
            ).fetchone()

        assert json.loads(listing[0]) == ["https://image"]
        assert listing[1] == 0
        assert json.loads(listing[2]) == {"schema_version": 1}
        assert listing[3] == "not_applicable"
        assert snapshot[0] == 1
        assert len(snapshot[1]) == 64
        assert json.loads(snapshot[2]) == ["https://image"]
        assert notification == ("legacy:1", 2)
        assert matches == [(2, "unknown")]
        assert seller == ("seller", None)
        with closing(sqlite3.connect(database)) as connection:
            images = connection.execute(
                "SELECT source_url, position, is_current FROM listing_images WHERE listing_id=1"
            ).fetchall()
        assert images == [("https://image", 0, 1)]
    finally:
        database.unlink(missing_ok=True)


def test_populated_6a_revision_upgrades_to_head(monkeypatch):
    database = _database_file("populated-6a")
    try:
        _upgrade(monkeypatch, database, "6a26ec39bb13")
        now = "2026-08-01 12:00:00"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO listings (source, external_id, title, description, url, "
                "image_urls_json, price, currency, condition, seller, seller_location, "
                "published_at, last_seen_at, consecutive_misses, is_active) VALUES "
                "('ebay', 'item-2', 'Title', '', 'https://item', '[]', 42, 'EUR', "
                "'used', '', '', ?, ?, 0, 1)",
                (now, now),
            )
            connection.commit()
        _upgrade(monkeypatch, database, "head")
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT price, source_metadata_json, detail_status FROM listings"
            ).fetchone()
        assert row == (42, '{"schema_version":1}', "not_applicable")
    finally:
        database.unlink(missing_ok=True)


def test_populated_sscom_revision_upgrades_to_deletion_revision(monkeypatch):
    database = _database_file("populated-9c")
    try:
        _upgrade(monkeypatch, database, "9c1e4a7b2f60")
        now = "2026-08-02 12:00:00"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO listings (source, external_id, title, description, url, "
                "image_urls_json, price, currency, condition, seller, seller_location, "
                "published_at, last_seen_at, source_metadata_json, detail_status, "
                "consecutive_misses, is_active) VALUES "
                "('ebay', 'item-3', 'Title', '', 'https://item', '[]', NULL, 'EUR', "
                "'used', 'legacy_seller (10, 99%)', 'DE', ?, ?, '{\"schema_version\":1}', "
                "'not_applicable', 0, 1)",
                (now, now),
            )
            connection.commit()
        _upgrade(monkeypatch, database, "head")
        with closing(sqlite3.connect(database)) as connection:
            listing = connection.execute(
                "SELECT seller_display, seller_identifier, seller_anonymized_at, price "
                "FROM listings"
            ).fetchone()
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert listing == ("legacy_seller (10, 99%)", None, None, None)
        assert revision == ("f1a3c5e7b9d2",)
    finally:
        database.unlink(missing_ok=True)


def test_review_migration_backfill_skips_malformed_images(monkeypatch):
    database = _database_file("review-backfill")
    try:
        _upgrade(monkeypatch, database, "b7d9e2f4a6c1")
        now = "2026-08-02 12:00:00"
        values = [
            (
                "valid",
                '[" https://i.ebayimg.com/one.jpg ", "https://i.ebayimg.com/one.jpg", 4, "ftp://bad"]',
            ),
            ("bad-json", "{"),
        ]
        with closing(sqlite3.connect(database)) as connection:
            for external_id, images in values:
                connection.execute(
                    "INSERT INTO listings (source, external_id, title, description, url, "
                    "image_urls_json, price, currency, condition, seller_display, "
                    "seller_location, "
                    "published_at, last_seen_at, source_metadata_json, detail_status, "
                    "consecutive_misses, is_active) VALUES "
                    "('ebay', ?, 'Title', '', 'https://item', ?, 1, 'EUR', 'used', '', '', "
                    "?, ?, '{\"schema_version\":1}', 'not_applicable', 0, 1)",
                    (external_id, images, now, now),
                )
            connection.commit()
        _upgrade(monkeypatch, database, "head")
        with closing(sqlite3.connect(database)) as connection:
            images = connection.execute(
                "SELECT source_url, position FROM listing_images ORDER BY id"
            ).fetchall()
        assert images == [("https://i.ebayimg.com/one.jpg", 0)]
    finally:
        database.unlink(missing_ok=True)


def test_populated_review_revision_backfills_provenance(monkeypatch):
    database = _database_file("review-hardening")
    try:
        _upgrade(monkeypatch, database, "d4f8a2c6e9b1")
        now = "2026-08-02 12:00:00"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "INSERT INTO listings (id, source, external_id, title, description, url, "
                "image_urls_json, price, currency, condition, seller_display, seller_location, "
                "published_at, last_seen_at, source_metadata_json, detail_status, "
                "consecutive_misses, is_active) VALUES "
                "(1, 'ebay', 'reviewed', 'Title', '', 'https://item', "
                "'[\"https://i.ebayimg.com/one.jpg\"]', 1, 'EUR', 'used', '', '', ?, ?, "
                "'{\"schema_version\":1}', 'not_applicable', 0, 1)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO listing_images VALUES "
                "(1, 1, 'https://i.ebayimg.com/one.jpg', 0, 1, ?, ?)",
                (now, now),
            )
            for review_id, outcome in ((1, "uncertain"), (2, "confirmed")):
                connection.execute(
                    "INSERT INTO manual_reviews "
                    "(id, listing_id, outcome, selected_part_id, notes, reviewed_at) "
                    "VALUES (?, 1, ?, 'roof-spoiler', NULL, ?)",
                    (review_id, outcome, now),
                )
            connection.execute(
                "INSERT INTO reference_images "
                "(id, listing_image_id, manual_review_id, part_id, label, local_path, "
                "content_sha256, mime_type, width, height, notes, is_active, created_at) "
                "VALUES (1, 1, 2, 'roof-spoiler', 'positive', ?, ?, 'image/webp', "
                "12, 8, NULL, 1, ?)",
                (f"references/roof-spoiler/{'a' * 64}.webp", "a" * 64, now),
            )
            connection.commit()

        _upgrade(monkeypatch, database, "head")
        with closing(sqlite3.connect(database)) as connection:
            reviews = connection.execute(
                "SELECT id, previous_review_id, reviewer_version, review_ui_version, "
                "decision_reason, created_from_queue_mode FROM manual_reviews ORDER BY id"
            ).fetchall()
            reference = connection.execute(
                "SELECT view, context, quality, obstruction, privacy_checked_at "
                "FROM reference_images"
            ).fetchone()
        assert reviews == [
            (1, None, "legacy", "legacy", None, "legacy"),
            (2, 1, "legacy", "legacy", None, "legacy"),
        ]
        assert reference == (None, None, None, None, None)
    finally:
        database.unlink(missing_ok=True)
