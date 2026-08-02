"""Database repositories for the Irmscher Parts Tracker.

Each repository class groups queries for a single entity and always
takes an ``AsyncSession`` as its first argument so callers control
transaction scope.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from irmscher_tracker.db.models import (
    EbayDeletionNotificationRow,
    ListingImageRow,
    ListingQueryRow,
    ListingRow,
    ListingSnapshotRow,
    ManualReviewRow,
    NotificationRow,
    PartMatchRow,
    ReferenceImageRow,
    SearchRunRow,
)
from irmscher_tracker.domain import (
    MatchResult,
    NormalizedListing,
    SearchRunResult,
    SearchRunStatus,
)


def _canonical_decimal(value: Decimal | None) -> str | None:
    return str(value.quantize(Decimal("0.01"))) if value is not None else None


def normalized_image_urls(values: list[str]) -> list[str]:
    """Return valid, trimmed HTTP(S) URLs in first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = value.strip()
        parsed = urlsplit(url)
        if not url or url in seen or parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        seen.add(url)
        result.append(url)
    return result


def snapshot_payload_hash(
    *,
    title: str,
    description: str,
    price: Decimal | None,
    currency: str,
    shipping_cost: Decimal | None,
    condition: str,
    seller_display: str,
    seller_identifier: str | None,
    seller_identifier_type: str | None,
    seller_feedback_score: int | None,
    seller_feedback_percentage: Decimal | None,
    seller_location: str,
    image_urls: list[str],
    url: str,
) -> str:
    import re

    payload = {
        "schema_version": 2,
        "title": re.sub(r"\s+", " ", title.strip()),
        "description": re.sub(r"\s+", " ", description.strip()),
        "price": _canonical_decimal(price),
        "currency": currency.upper(),
        "shipping_cost": _canonical_decimal(shipping_cost),
        "condition": condition,
        "seller_display": seller_display.strip(),
        "seller_identifier": (seller_identifier or "").strip(),
        "seller_identifier_type": (seller_identifier_type or "").strip(),
        "seller_feedback_score": seller_feedback_score,
        "seller_feedback_percentage": _canonical_decimal(seller_feedback_percentage),
        "seller_location": seller_location.strip(),
        "image_urls": sorted(image_urls),
        "url": url.strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ListingRepository:
    """CRUD operations for ``ListingRow``."""

    async def get_by_source_and_external_id(
        self, session: AsyncSession, source: str, external_id: str
    ) -> ListingRow | None:
        stmt = select(ListingRow).where(
            ListingRow.source == source,
            ListingRow.external_id == external_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(
        self, session: AsyncSession, listing: NormalizedListing
    ) -> tuple[ListingRow, bool, Decimal | None, bool]:
        """Insert or update a listing.

        Returns the row, creation flag, previous price, and previous active state.
        """
        now = datetime.now(UTC)
        source_str = listing.source.value
        condition_str = listing.condition.value
        metadata = listing.source_metadata or {"schema_version": 1}
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        if len(metadata_json.encode()) > 32 * 1024:
            raise ValueError("Source metadata exceeds 32 KiB")

        existing = await self.get_by_source_and_external_id(
            session, source_str, listing.external_id
        )

        if existing is not None:
            previous_price = existing.price
            was_active = existing.is_active
            if existing.seller_anonymized_at is not None:
                listing.seller_display = ""
                listing.seller_identifier = ""
                listing.seller_identifier_type = ""
                listing.seller_feedback_score = None
                listing.seller_feedback_percentage = None
                listing.seller_location = ""
            existing.title = listing.title
            existing.description = listing.description
            existing.url = listing.url
            listing.image_urls = normalized_image_urls(listing.image_urls)
            existing.image_urls_json = json.dumps(listing.image_urls)
            existing.price = listing.price
            existing.currency = listing.currency
            existing.shipping_cost = listing.shipping_cost
            existing.condition = condition_str
            if existing.seller_anonymized_at is None:
                existing.seller_display = listing.seller_display
                existing.seller_identifier = listing.seller_identifier or None
                existing.seller_identifier_type = listing.seller_identifier_type or None
                existing.seller_feedback_score = listing.seller_feedback_score
                existing.seller_feedback_percentage = listing.seller_feedback_percentage
                existing.seller_location = listing.seller_location
            existing.published_at = listing.published_at or existing.published_at
            existing.last_seen_at = now
            existing.consecutive_misses = 0
            existing.is_active = True
            existing.inactive_at = None
            if not was_active:
                existing.reactivated_at = now
            existing.source_metadata_json = metadata_json
            existing.rss_fingerprint_seen = listing.rss_fingerprint_seen
            existing.rss_fingerprint_enriched = listing.rss_fingerprint_enriched
            existing.last_detail_success_at = listing.last_detail_success_at
            existing.detail_status = listing.detail_status

            await session.flush()
            await self.sync_images(session, existing.id, listing.image_urls, now)
            return existing, False, previous_price, was_active

        listing.image_urls = normalized_image_urls(listing.image_urls)
        row = ListingRow(
            source=source_str,
            external_id=listing.external_id,
            title=listing.title,
            description=listing.description,
            url=listing.url,
            image_urls_json=json.dumps(listing.image_urls),
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            condition=condition_str,
            seller_display=listing.seller_display,
            seller_identifier=listing.seller_identifier or None,
            seller_identifier_type=listing.seller_identifier_type or None,
            seller_feedback_score=listing.seller_feedback_score,
            seller_feedback_percentage=listing.seller_feedback_percentage,
            seller_location=listing.seller_location,
            published_at=listing.published_at or now,
            last_seen_at=now,
            last_changed_at=now,
            is_active=True,
            consecutive_misses=0,
            source_metadata_json=metadata_json,
            rss_fingerprint_seen=listing.rss_fingerprint_seen,
            rss_fingerprint_enriched=listing.rss_fingerprint_enriched,
            last_detail_success_at=listing.last_detail_success_at,
            detail_status=listing.detail_status,
        )
        session.add(row)
        await session.flush()
        await self.sync_images(session, row.id, listing.image_urls, now)
        return row, True, None, True

    async def sync_images(
        self,
        session: AsyncSession,
        listing_id: int,
        image_urls: list[str],
        observed_at: datetime | None = None,
    ) -> None:
        now = observed_at or datetime.now(UTC)
        urls = normalized_image_urls(image_urls)
        rows = list(
            (
                await session.execute(
                    select(ListingImageRow).where(ListingImageRow.listing_id == listing_id)
                )
            ).scalars()
        )
        by_url = {row.source_url: row for row in rows}
        for row in rows:
            row.is_current = False
        for position, url in enumerate(urls):
            image_row = by_url.get(url)
            if image_row is None:
                session.add(
                    ListingImageRow(
                        listing_id=listing_id,
                        source_url=url,
                        position=position,
                        is_current=True,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                image_row.position = position
                image_row.is_current = True
                image_row.last_seen_at = now
        await session.flush()

    async def get_by_id(self, session: AsyncSession, listing_id: int) -> ListingRow | None:
        return await session.get(ListingRow, listing_id)

    async def list_by_source(self, session: AsyncSession, source: str) -> list[ListingRow]:
        result = await session.execute(select(ListingRow).where(ListingRow.source == source))
        return list(result.scalars().all())

    async def list_listings(
        self,
        session: AsyncSession,
        source: str | None = None,
        is_active: bool | None = None,
        max_price: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ListingRow]:
        stmt = select(ListingRow)
        if source is not None:
            stmt = stmt.where(ListingRow.source == source)
        if is_active is not None:
            stmt = stmt.where(ListingRow.is_active == is_active)
        if max_price is not None:
            stmt = stmt.where(ListingRow.price <= max_price)
        stmt = stmt.order_by(ListingRow.last_seen_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def record_query_observation(
        self, session: AsyncSession, listing_id: int, source: str, query: str
    ) -> None:
        stmt = select(ListingQueryRow).where(
            ListingQueryRow.listing_id == listing_id,
            ListingQueryRow.source == source,
            ListingQueryRow.query == query,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        now = datetime.now(UTC)
        if row is None:
            session.add(
                ListingQueryRow(
                    listing_id=listing_id, source=source, query=query, last_seen_at=now
                )
            )
        else:
            row.last_seen_at = now
        await session.flush()

    async def mark_inactive(self, session: AsyncSession, listing_id: int) -> None:
        row = await self.get_by_id(session, listing_id)
        if row is not None and row.is_active:
            row.is_active = False
            row.inactive_at = datetime.now(UTC)

    async def increment_misses_for_unseen(
        self,
        session: AsyncSession,
        source: str,
        seen_ids: set[int],
        threshold: int,
    ) -> None:
        missing = update(ListingRow).where(
            ListingRow.source == source,
            ListingRow.is_active.is_(True),
        )
        if seen_ids:
            missing = missing.where(ListingRow.id.not_in(seen_ids))
        await session.execute(missing.values(consecutive_misses=ListingRow.consecutive_misses + 1))
        await session.execute(
            update(ListingRow)
            .where(
                ListingRow.source == source,
                ListingRow.is_active.is_(True),
                ListingRow.consecutive_misses >= threshold,
            )
            .values(is_active=False, inactive_at=datetime.now(UTC))
        )


class SnapshotRepository:
    """CRUD operations for ``ListingSnapshotRow``."""

    async def get_latest(
        self, session: AsyncSession, listing_id: int
    ) -> ListingSnapshotRow | None:
        stmt = (
            select(ListingSnapshotRow)
            .where(ListingSnapshotRow.listing_id == listing_id)
            .order_by(ListingSnapshotRow.captured_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_if_changed(
        self,
        session: AsyncSession,
        listing_id: int,
        listing: NormalizedListing,
    ) -> ListingSnapshotRow | None:
        """Create a snapshot only when tracked fields differ from the latest."""
        latest = await self.get_latest(session, listing_id)
        condition_str = listing.condition.value

        payload_hash = snapshot_payload_hash(
            title=listing.title,
            description=listing.description,
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            condition=condition_str,
            seller_display=listing.seller_display,
            seller_identifier=listing.seller_identifier,
            seller_identifier_type=listing.seller_identifier_type,
            seller_feedback_score=listing.seller_feedback_score,
            seller_feedback_percentage=listing.seller_feedback_percentage,
            seller_location=listing.seller_location,
            image_urls=listing.image_urls,
            url=listing.url,
        )

        if latest is not None and latest.payload_hash == payload_hash:
            return None

        snapshot = ListingSnapshotRow(
            listing_id=listing_id,
            schema_version=2,
            payload_hash=payload_hash,
            title=listing.title,
            description=listing.description,
            price=listing.price,
            currency=listing.currency,
            shipping_cost=listing.shipping_cost,
            condition=condition_str,
            seller_display=listing.seller_display,
            seller_identifier=listing.seller_identifier or None,
            seller_identifier_type=listing.seller_identifier_type or None,
            seller_feedback_score=listing.seller_feedback_score,
            seller_feedback_percentage=listing.seller_feedback_percentage,
            seller_location=listing.seller_location,
            image_urls_json=json.dumps(listing.image_urls),
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def list_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> list[ListingSnapshotRow]:
        stmt = (
            select(ListingSnapshotRow)
            .where(ListingSnapshotRow.listing_id == listing_id)
            .order_by(ListingSnapshotRow.captured_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


class MatchRepository:
    """CRUD operations for ``PartMatchRow``."""

    async def upsert(
        self, session: AsyncSession, listing_id: int, match: MatchResult
    ) -> PartMatchRow:
        row = await self.get_best_for_listing(session, listing_id)
        if row is None:
            row = PartMatchRow(listing_id=listing_id)
            session.add(row)
        row.part_id = match.part_id
        row.part_name = match.part_name
        row.total_score = match.total_score
        row.compatibility_status = match.compatibility_status
        row.reasons_json = json.dumps([reason.model_dump() for reason in match.reasons])
        row.algorithm_version = match.algorithm_version
        row.matched_at = datetime.now(UTC)
        await session.flush()
        return row

    async def get_best_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> PartMatchRow | None:
        stmt = (
            select(PartMatchRow)
            .where(PartMatchRow.listing_id == listing_id)
            .order_by(PartMatchRow.total_score.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_for_listing(self, session: AsyncSession, listing_id: int) -> None:
        await session.execute(
            update(NotificationRow)
            .where(NotificationRow.listing_id == listing_id)
            .values(match_id=None)
        )
        await session.execute(delete(PartMatchRow).where(PartMatchRow.listing_id == listing_id))

    async def list_matches(
        self,
        session: AsyncSession,
        part_id: str | None = None,
        min_score: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PartMatchRow]:
        stmt = select(PartMatchRow)
        if part_id is not None:
            stmt = stmt.where(PartMatchRow.part_id == part_id)
        if min_score is not None:
            stmt = stmt.where(PartMatchRow.total_score >= min_score)
        stmt = stmt.order_by(PartMatchRow.matched_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())


class NotificationRepository:
    """CRUD operations for ``NotificationRow``."""

    async def reserve(
        self,
        session: AsyncSession,
        listing_id: int,
        match_id: int | None,
        alert_type: str,
        payload: str,
        event_key: str,
    ) -> NotificationRow | None:
        stmt = (
            insert(NotificationRow)
            .values(
                event_key=event_key,
                listing_id=listing_id,
                match_id=match_id,
                alert_type=alert_type,
                payload_json=payload,
                sent_at=datetime.now(UTC),
                success=False,
                error_message=None,
            )
            .on_conflict_do_nothing(index_elements=["event_key"])
            .returning(NotificationRow.id)
        )

        result = await session.execute(stmt)
        notification_id = result.scalar_one_or_none()
        result.close()
        await session.flush()
        if notification_id is None:
            return None
        return await session.get(NotificationRow, notification_id)

    async def finish(
        self,
        session: AsyncSession,
        notification_id: int,
        success: bool,
        error_message: str | None,
    ) -> None:
        row = await session.get(NotificationRow, notification_id)
        if row is not None:
            row.success = success
            row.error_message = error_message

    async def was_notified(self, session: AsyncSession, listing_id: int, alert_type: str) -> bool:
        stmt = (
            select(NotificationRow)
            .where(
                NotificationRow.listing_id == listing_id,
                NotificationRow.alert_type == alert_type,
                NotificationRow.success == True,  # noqa: E712
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_for_listing(
        self, session: AsyncSession, listing_id: int
    ) -> list[NotificationRow]:
        stmt = (
            select(NotificationRow)
            .where(NotificationRow.listing_id == listing_id)
            .order_by(NotificationRow.sent_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


_SELLER_KEYS = {
    "seller",
    "sellerdisplay",
    "selleridentifier",
    "sellerlocation",
    "username",
    "userid",
    "eiastoken",
}


def _scrub_json(value: object, identifiers: set[str]) -> object:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = key.replace("_", "").casefold()
            sensitive = normalized_key in _SELLER_KEYS or normalized_key.startswith("seller")
            result[key] = "" if sensitive else _scrub_json(item, identifiers)
        return result
    if isinstance(value, list):
        return [_scrub_json(item, identifiers) for item in value]
    if isinstance(value, str) and _seller_matches(value, identifiers):
        return ""
    return value


def _scrub_json_text(value: str, identifiers: set[str]) -> str:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return "{}"
    return json.dumps(_scrub_json(parsed, identifiers), ensure_ascii=False, separators=(",", ":"))


def _seller_matches(value: str | None, identifiers: set[str]) -> bool:
    candidate = (value or "").strip().casefold()
    return candidate in identifiers or any(
        candidate.startswith(f"{identifier} (") for identifier in identifiers
    )


def _stored_seller_matches(
    display: str | None,
    identifier: str | None,
    identifier_type: str | None,
    deletion: EbayDeletionNotificationRow,
) -> bool:
    stored = (identifier or "").strip().casefold()
    user_id = (deletion.user_id or "").strip().casefold()
    username = (deletion.username or "").strip().casefold()
    eias_token = (deletion.eias_token or "").strip().casefold()
    if identifier_type == "user_id" and user_id and stored == user_id:
        return True
    if stored and ((user_id and stored == user_id) or (username and stored == username)):
        return True
    if _seller_matches(display, {value for value in (user_id, username) if value}):
        return True
    return identifier_type == "eias_token" and bool(eias_token) and stored == eias_token


class EbayDeletionRepository:
    async def reserve(
        self,
        session: AsyncSession,
        *,
        notification_id: str,
        username: str | None,
        user_id: str | None,
        eias_token: str | None,
    ) -> bool:
        now = datetime.now(UTC)
        result = await session.execute(
            insert(EbayDeletionNotificationRow)
            .values(
                notification_id=notification_id,
                username=username,
                user_id=user_id,
                eias_token=eias_token,
                status="pending",
                received_at=now,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=["notification_id"])
            .returning(EbayDeletionNotificationRow.id)
        )
        inserted = result.scalar_one_or_none() is not None
        result.close()
        return inserted

    async def recover_expired(self, session: AsyncSession) -> None:
        now = datetime.now(UTC)
        await session.execute(
            update(EbayDeletionNotificationRow)
            .where(
                EbayDeletionNotificationRow.status == "processing",
                EbayDeletionNotificationRow.lease_expires_at <= now,
            )
            .values(status="pending", lease_expires_at=None, next_attempt_at=now)
        )

    async def claim_next(self, session: AsyncSession) -> EbayDeletionNotificationRow | None:
        now = datetime.now(UTC)
        candidate = (
            select(EbayDeletionNotificationRow.id)
            .where(
                EbayDeletionNotificationRow.status == "pending",
                or_(
                    EbayDeletionNotificationRow.next_attempt_at.is_(None),
                    EbayDeletionNotificationRow.next_attempt_at <= now,
                ),
            )
            .order_by(EbayDeletionNotificationRow.received_at)
            .limit(1)
            .scalar_subquery()
        )
        result = await session.execute(
            update(EbayDeletionNotificationRow)
            .where(
                EbayDeletionNotificationRow.id == candidate,
                EbayDeletionNotificationRow.status == "pending",
            )
            .values(
                status="processing",
                attempt_count=EbayDeletionNotificationRow.attempt_count + 1,
                last_attempt_at=now,
                lease_expires_at=now + timedelta(minutes=5),
                last_error_code=None,
            )
            .returning(EbayDeletionNotificationRow.id)
        )
        row_id = result.scalar_one_or_none()
        result.close()
        if row_id is None:
            return None
        return await session.get(EbayDeletionNotificationRow, row_id)

    async def anonymize(self, session: AsyncSession, row: EbayDeletionNotificationRow) -> None:
        identifiers = {
            value.strip().casefold()
            for value in (row.user_id, row.username, row.eias_token)
            if value and value.strip()
        }
        if not identifiers:
            return
        listings = list(
            (
                await session.execute(select(ListingRow).where(ListingRow.source == "ebay"))
            ).scalars()
        )
        for listing in listings:
            snapshots = list(
                (
                    await session.execute(
                        select(ListingSnapshotRow).where(
                            ListingSnapshotRow.listing_id == listing.id
                        )
                    )
                ).scalars()
            )
            matched = _stored_seller_matches(
                listing.seller_display,
                listing.seller_identifier,
                listing.seller_identifier_type,
                row,
            )
            if not matched:
                matched = any(
                    _stored_seller_matches(
                        snapshot.seller_display,
                        snapshot.seller_identifier,
                        snapshot.seller_identifier_type,
                        row,
                    )
                    for snapshot in snapshots
                )
            if not matched:
                continue

            listing.seller_display = ""
            listing.seller_identifier = None
            listing.seller_identifier_type = None
            listing.seller_feedback_score = None
            listing.seller_feedback_percentage = None
            listing.seller_location = ""
            listing.seller_anonymized_at = datetime.now(UTC)
            listing.source_metadata_json = _scrub_json_text(
                listing.source_metadata_json, identifiers
            )
            for snapshot in snapshots:
                snapshot.seller_display = ""
                snapshot.seller_identifier = None
                snapshot.seller_identifier_type = None
                snapshot.seller_feedback_score = None
                snapshot.seller_feedback_percentage = None
                snapshot.seller_location = ""
                try:
                    images = json.loads(snapshot.image_urls_json or "[]")
                except json.JSONDecodeError:
                    images = []
                snapshot.payload_hash = snapshot_payload_hash(
                    title=snapshot.title,
                    description=snapshot.description,
                    price=snapshot.price,
                    currency=snapshot.currency,
                    shipping_cost=snapshot.shipping_cost,
                    condition=snapshot.condition,
                    seller_display="",
                    seller_identifier=None,
                    seller_identifier_type=None,
                    seller_feedback_score=None,
                    seller_feedback_percentage=None,
                    seller_location="",
                    image_urls=images,
                    url=listing.url,
                )
                snapshot.schema_version = 2
            notifications = list(
                (
                    await session.execute(
                        select(NotificationRow).where(NotificationRow.listing_id == listing.id)
                    )
                ).scalars()
            )
            for notification in notifications:
                notification.payload_json = _scrub_json_text(
                    notification.payload_json, identifiers
                )
            await session.execute(
                update(ManualReviewRow)
                .where(ManualReviewRow.listing_id == listing.id)
                .values(notes=None)
            )
            affected_images = select(ListingImageRow.id).where(
                ListingImageRow.listing_id == listing.id
            )
            await session.execute(
                update(ReferenceImageRow)
                .where(ReferenceImageRow.listing_image_id.in_(affected_images))
                .values(notes=None)
            )

    async def mark_processed(
        self, session: AsyncSession, row: EbayDeletionNotificationRow
    ) -> None:
        row.username = None
        row.user_id = None
        row.eias_token = None
        row.status = "processed"
        row.processed_at = datetime.now(UTC)
        row.next_attempt_at = None
        row.lease_expires_at = None
        row.last_error_code = None

    async def retry(
        self, session: AsyncSession, row: EbayDeletionNotificationRow, error_code: str
    ) -> None:
        exponent = min(max(row.attempt_count - 1, 0), 10)
        delay = min(5 * (2**exponent), 3600)
        row.status = "pending"
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        row.lease_expires_at = None
        row.last_error_code = error_code

    async def pending_stats(self, session: AsyncSession) -> tuple[int, float | None]:
        now = datetime.now(UTC)
        result = await session.execute(
            select(
                func.count(EbayDeletionNotificationRow.id),
                func.min(EbayDeletionNotificationRow.received_at),
            ).where(EbayDeletionNotificationRow.status != "processed")
        )
        count, oldest = result.one()
        if oldest is None:
            return int(count), None
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return int(count), max((now - oldest).total_seconds(), 0.0)


class SearchRunRepository:
    """CRUD operations for ``SearchRunRow``."""

    async def create(self, session: AsyncSession, source: str) -> SearchRunRow:
        row = SearchRunRow(
            source=source,
            started_at=datetime.now(UTC),
            status="running",
            total_found=0,
            new_listings=0,
            updated_listings=0,
            matches_found=0,
            alerts_sent=0,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_by_id(self, session: AsyncSession, run_id: int) -> SearchRunRow | None:
        return await session.get(SearchRunRow, run_id)

    async def complete(
        self,
        session: AsyncSession,
        run_id: int,
        result: SearchRunResult,
        status: SearchRunStatus = SearchRunStatus.COMPLETED,
    ) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.total_found = result.total_found
            row.new_listings = result.new_listings
            row.updated_listings = result.updated_listings
            row.matches_found = result.matches_found
            row.alerts_sent = result.alerts_sent
            row.errors_json = json.dumps(result.errors) if result.errors else None
            row.status = status.value

    async def fail(self, session: AsyncSession, run_id: int, error: str) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.errors_json = json.dumps({"error": error})
            row.status = SearchRunStatus.FAILED.value

    async def finish_with_status(
        self,
        session: AsyncSession,
        run_id: int,
        status: SearchRunStatus,
        error: str | None = None,
    ) -> None:
        row = await session.get(SearchRunRow, run_id)
        if row is not None:
            row.finished_at = datetime.now(UTC)
            row.status = status.value
            row.errors_json = json.dumps({"error": error}) if error else None

    async def interrupt_stale(self, session: AsyncSession) -> None:
        await session.execute(
            update(SearchRunRow)
            .where(SearchRunRow.status == SearchRunStatus.RUNNING.value)
            .values(
                finished_at=datetime.now(UTC),
                status=SearchRunStatus.INTERRUPTED.value,
                errors_json=json.dumps({"error": "Interrupted by tracker restart"}),
            )
        )

    async def list_runs(
        self, session: AsyncSession, limit: int = 100, offset: int = 0
    ) -> list[SearchRunRow]:
        stmt = (
            select(SearchRunRow)
            .order_by(SearchRunRow.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
