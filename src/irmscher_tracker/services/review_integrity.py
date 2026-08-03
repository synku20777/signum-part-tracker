from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.api.schemas import (
    ReviewIntegrityCheckResponse,
    ReviewIntegrityResponse,
    ReviewIntegritySummary,
)
from irmscher_tracker.db.models import (
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    ReferenceImageRow,
)
from irmscher_tracker.db.repositories import normalized_image_urls

logger = logging.getLogger(__name__)
_MAX_IDS = 100


class ReviewIntegrityService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        data_directory: Path,
        part_ids: set[str],
    ) -> None:
        self._session_factory = session_factory
        self._reference_root = (data_directory / "references").resolve()
        self._part_ids = part_ids

    async def check(self, *, repair: bool = False) -> ReviewIntegrityResponse:
        async with self._session_factory() as session:
            listings = list((await session.execute(select(ListingRow))).scalars())
            images = list((await session.execute(select(ListingImageRow))).scalars())
            reviews = list((await session.execute(select(ManualReviewRow))).scalars())
            references = list((await session.execute(select(ReferenceImageRow))).scalars())

        listing_by_id = {row.id: row for row in listings}
        image_by_id = {row.id: row for row in images}
        review_by_id = {row.id: row for row in reviews}
        reviews_by_listing: dict[int, list[ManualReviewRow]] = {}
        for review in reviews:
            reviews_by_listing.setdefault(review.listing_id, []).append(review)
        latest_by_listing = {
            listing_id: max(rows, key=lambda row: (row.reviewed_at, row.id))
            for listing_id, rows in reviews_by_listing.items()
        }

        checks: list[ReviewIntegrityCheckResponse] = []
        self._add(
            checks,
            "listing_image_parent",
            "error",
            [row.id for row in images if row.listing_id not in listing_by_id],
            "Listing images reference missing listings.",
        )

        image_set_errors: list[int] = []
        for listing in listings:
            try:
                raw = json.loads(listing.image_urls_json or "[]")
                expected_urls = normalized_image_urls(raw) if isinstance(raw, list) else []
            except (json.JSONDecodeError, TypeError, AttributeError):
                image_set_errors.append(listing.id)
                continue
            current = [
                row.source_url
                for row in sorted(images, key=lambda row: (row.position, row.id))
                if row.listing_id == listing.id and row.is_current
            ]
            if current != expected_urls:
                image_set_errors.append(listing.id)
        self._add(
            checks,
            "current_listing_images",
            "error",
            image_set_errors,
            "Current normalized image rows do not match listing image JSON.",
        )

        self._add(
            checks,
            "manual_review_parent",
            "error",
            [row.id for row in reviews if row.listing_id not in listing_by_id],
            "Manual reviews reference missing listings.",
        )
        self._add(
            checks,
            "manual_review_part",
            "error",
            [
                row.id
                for row in reviews
                if row.selected_part_id is not None and row.selected_part_id not in self._part_ids
            ],
            "Manual reviews reference unknown configured parts.",
        )
        self._add(
            checks,
            "review_history_chain",
            "error",
            [
                row.id
                for row in reviews
                if row.previous_review_id is not None
                and (
                    row.previous_review_id not in review_by_id
                    or review_by_id[row.previous_review_id].listing_id != row.listing_id
                )
            ],
            "Review history links are missing or cross listing boundaries.",
        )

        reference_parent_errors: list[int] = []
        reference_provenance_errors: list[int] = []
        positive_state_errors: list[int] = []
        privacy_warnings: list[int] = []
        path_errors: list[int] = []
        active_missing: list[int] = []
        inactive_missing: list[int] = []
        file_errors: list[int] = []
        known_files: set[Path] = set()
        for reference in references:
            image = image_by_id.get(reference.listing_image_id)
            origin_review = review_by_id.get(reference.manual_review_id)
            if image is None or origin_review is None or reference.part_id not in self._part_ids:
                reference_parent_errors.append(reference.id)
                continue
            if (
                origin_review.listing_id != image.listing_id
                or origin_review.selected_part_id != reference.part_id
            ):
                reference_provenance_errors.append(reference.id)
            latest = latest_by_listing.get(image.listing_id)
            if (
                reference.is_active
                and reference.label == "positive"
                and (
                    latest is None
                    or latest.outcome != "confirmed"
                    or latest.selected_part_id != reference.part_id
                )
            ):
                positive_state_errors.append(reference.id)
            if reference.is_active and reference.privacy_checked_at is None:
                privacy_warnings.append(reference.id)
            path = (self._reference_root.parent / reference.local_path).resolve()
            expected_path = (
                self._reference_root / reference.part_id / f"{reference.content_sha256}.webp"
            )
            if not path.is_relative_to(self._reference_root) or path != expected_path:
                path_errors.append(reference.id)
                continue
            known_files.add(path)
            if not path.is_file():
                (active_missing if reference.is_active else inactive_missing).append(reference.id)
                continue
            if not self.valid_file(path, reference):
                file_errors.append(reference.id)

        self._add(
            checks,
            "reference_parent",
            "error",
            reference_parent_errors,
            "References point to missing images, reviews, or parts.",
        )
        self._add(
            checks,
            "reference_provenance",
            "error",
            reference_provenance_errors,
            "Reference review provenance does not match its listing and part.",
        )
        self._add(
            checks,
            "active_positive_state",
            "error",
            positive_state_errors,
            "Active positive references disagree with the latest review.",
        )
        self._add(
            checks,
            "reference_privacy_confirmation",
            "warning",
            privacy_warnings,
            "Active legacy references lack a recorded pixel privacy confirmation.",
        )
        self._add(
            checks,
            "reference_paths",
            "error",
            path_errors,
            "Reference paths escape or do not match content-addressed storage.",
        )
        self._add(
            checks,
            "active_reference_files",
            "error",
            active_missing,
            "Active reference files are missing.",
        )
        self._add(
            checks,
            "inactive_reference_files",
            "warning",
            inactive_missing,
            "Inactive reference files are missing.",
        )
        self._add(
            checks,
            "reference_file_content",
            "error",
            file_errors,
            "Stored reference hashes, WebP data, MIME types, or dimensions disagree.",
        )

        conflicts = [ids[0] for ids in self._active_conflicts(references).values() if len(ids) > 1]
        self._add(
            checks,
            "active_label_conflicts",
            "error",
            conflicts,
            "A part has conflicting active labels for one content hash.",
        )

        stale = self._stale_temporary_files()
        if repair:
            for path in stale:
                path.unlink(missing_ok=True)
            if stale:
                logger.info("Removed %d stale review temporary files", len(stale))
            stale = self._stale_temporary_files()
        self._add(
            checks,
            "stale_temporary_files",
            "warning",
            list(range(1, len(stale) + 1)),
            "Reference temporary files older than one hour remain.",
            repairable=True,
        )

        orphan_count = 0
        if self._reference_root.exists():
            orphan_count = sum(
                path.resolve() not in known_files for path in self._reference_root.rglob("*.webp")
            )
        self._add(
            checks,
            "untracked_reference_files",
            "warning",
            list(range(1, orphan_count + 1)),
            "Content-addressed WebP files are not referenced by the database.",
        )

        note_errors = self._seller_note_errors(listings, reviews, references, image_by_id)
        self._add(
            checks,
            "seller_text_in_notes",
            "error",
            note_errors,
            "Review or reference notes contain current seller-derived text.",
        )

        errors = sum(check.status == "error" for check in checks)
        warnings = sum(check.status == "warning" for check in checks)
        status: Literal["ok", "warning", "error"] = (
            "error" if errors else "warning" if warnings else "ok"
        )
        return ReviewIntegrityResponse(
            status=status,
            checked_at=datetime.now(UTC),
            summary=ReviewIntegritySummary(errors=errors, warnings=warnings),
            checks=checks,
        )

    @staticmethod
    def valid_file(path: Path, reference: ReferenceImageRow) -> bool:
        try:
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            with Image.open(path) as image:
                image.load()
                return (
                    digest == reference.content_sha256
                    and image.format == "WEBP"
                    and image.width == reference.width
                    and image.height == reference.height
                    and reference.mime_type == "image/webp"
                )
        except (OSError, UnidentifiedImageError, ValueError):
            return False

    @staticmethod
    def _active_conflicts(
        references: list[ReferenceImageRow],
    ) -> dict[tuple[str, str], list[int]]:
        groups: dict[tuple[str, str], list[int]] = {}
        for reference in references:
            if reference.is_active:
                groups.setdefault((reference.part_id, reference.content_sha256), []).append(
                    reference.id
                )
        return groups

    def _stale_temporary_files(self) -> list[Path]:
        if not self._reference_root.exists():
            return []
        cutoff = time.time() - 3600
        return [
            path
            for path in self._reference_root.rglob(".reference-*.tmp")
            if path.is_file() and path.stat().st_mtime < cutoff
        ]

    @staticmethod
    def _seller_note_errors(
        listings: list[ListingRow],
        reviews: list[ManualReviewRow],
        references: list[ReferenceImageRow],
        image_by_id: dict[int, ListingImageRow],
    ) -> list[int]:
        identifiers = {
            listing.id: {
                value.strip().casefold()
                for value in (listing.seller_identifier, listing.seller_display)
                if value and len(value.strip()) >= 3
            }
            for listing in listings
        }
        affected: list[int] = []
        for review in reviews:
            note = (review.notes or "").casefold()
            if note and any(value in note for value in identifiers.get(review.listing_id, set())):
                affected.append(review.id)
        for reference in references:
            image = image_by_id.get(reference.listing_image_id)
            note = (reference.notes or "").casefold()
            if (
                image
                and note
                and any(value in note for value in identifiers.get(image.listing_id, set()))
            ):
                affected.append(reference.id)
        return affected

    @staticmethod
    def _add(
        checks: list[ReviewIntegrityCheckResponse],
        name: str,
        failure_status: Literal["warning", "error"],
        affected: list[int],
        detail: str,
        *,
        repairable: bool = False,
    ) -> None:
        checks.append(
            ReviewIntegrityCheckResponse(
                name=name,
                status=failure_status if affected else "ok",
                affected_record_ids=affected[:_MAX_IDS],
                affected_count=len(affected),
                detail=detail if affected else "Check passed.",
                repairable=repairable,
            )
        )
