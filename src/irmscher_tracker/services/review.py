from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.api.schemas import (
    ListingImageResponse,
    ManualReviewCreatedResponse,
    ManualReviewRequest,
    ManualReviewResponse,
    ReferenceImageResponse,
    ReferenceResultResponse,
    ReferenceUpdateRequest,
    ReviewListingDetailResponse,
    ReviewMatchResponse,
    ReviewOutcomeProgress,
    ReviewPartProgress,
    ReviewProgressResponse,
    ReviewQueueItemResponse,
    ReviewQueueProgress,
    ReviewQueueResponse,
    ReviewSourceProgress,
)
from irmscher_tracker.db.models import (
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    PartMatchRow,
    ReferenceImageRow,
)
from irmscher_tracker.matcher import PartMatcher

_IMAGE_HOSTS = {"ebay": "i.ebayimg.com", "sscom": "i.ss.com"}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_BODY_BYTES = 10 * 1024 * 1024
_MAX_SIDE = 10_000
_MAX_PIXELS = 40_000_000
_CAMPAIGN_TARGET = 100
_COVERAGE_TARGETS = {
    "confirmed_listings": 3,
    "positive_references": 5,
    "negative_listings": 5,
    "negative_references": 10,
}


class ReviewNotFoundError(Exception):
    pass


class ReviewConflictError(Exception):
    pass


class ReferenceImageError(Exception):
    pass


@dataclass(frozen=True)
class SanitizedImage:
    content: bytes
    sha256: str
    width: int
    height: int


class ReferenceImageStore:
    def __init__(self, data_directory: Path, client: httpx.AsyncClient | None = None) -> None:
        self.data_directory = data_directory.resolve()
        self.reference_directory = self.data_directory / "references"
        self._client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        self._owns_client = client is None
        # ponytail: one lock fits the required one-worker deployment; shard if throughput matters.
        self._lock = asyncio.Lock()

    async def download(self, source: str, source_url: str) -> SanitizedImage:
        expected_host = _IMAGE_HOSTS.get(source)
        if expected_host is None:
            raise ReferenceImageError("Image source is not supported")
        url = self._validated_url(source_url, expected_host)
        try:
            for redirects in range(4):
                async with self._client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirects == 3 or not (location := response.headers.get("location")):
                            raise ReferenceImageError("Image redirect was rejected")
                        url = self._validated_url(urljoin(url, location), expected_host)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type.lower() not in _IMAGE_CONTENT_TYPES:
                        raise ReferenceImageError("Image content type is not supported")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_BODY_BYTES:
                            raise ReferenceImageError("Image exceeds the 10 MiB limit")
                    return await asyncio.to_thread(self._sanitize, bytes(body))
        except ReferenceImageError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise ReferenceImageError("Image download failed") from exc
        raise ReferenceImageError("Image redirect was rejected")

    @staticmethod
    def _validated_url(value: str, expected_host: str) -> str:
        parsed = urlsplit(value)
        try:
            invalid = parsed.port not in {None, 443}
        except ValueError:
            invalid = True
        if invalid or (
            parsed.scheme != "https"
            or parsed.hostname != expected_host
            or parsed.username
            or parsed.password
        ):
            raise ReferenceImageError("Image URL was rejected")
        return value

    @staticmethod
    def _sanitize(body: bytes) -> SanitizedImage:
        try:
            with Image.open(io.BytesIO(body)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ReferenceImageError("Image format is not supported")
                original.seek(0)
                original.load()
                if (
                    original.width > _MAX_SIDE
                    or original.height > _MAX_SIDE
                    or original.width * original.height > _MAX_PIXELS
                ):
                    raise ReferenceImageError("Image dimensions exceed the limit")
                image = ImageOps.exif_transpose(original)
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                output = io.BytesIO()
                image.save(output, format="WEBP", lossless=True, method=6)
                content = output.getvalue()
                return SanitizedImage(
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    width=image.width,
                    height=image.height,
                )
        except ReferenceImageError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise ReferenceImageError("Image is corrupt") from exc

    async def store(self, part_id: str, image: SanitizedImage) -> tuple[str, bool]:
        relative = Path("references") / part_id / f"{image.sha256}.webp"
        target = (self.data_directory / relative).resolve()
        if not target.is_relative_to(self.reference_directory.resolve()):
            raise ReferenceImageError("Reference path was rejected")
        async with self._lock:
            if target.is_file():
                return relative.as_posix(), False
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".reference-", suffix=".tmp", dir=target.parent, delete=False
                ) as handle:
                    handle.write(image.content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary = Path(handle.name)
                os.replace(temporary, target)
                temporary = None
                return relative.as_posix(), True
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def resolve(self, local_path: str) -> Path:
        path = (self.data_directory / local_path).resolve()
        if not path.is_relative_to(self.reference_directory.resolve()):
            raise ReferenceImageError("Reference path was rejected")
        return path

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ReviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        matcher: PartMatcher,
        store: ReferenceImageStore,
    ) -> None:
        self._session_factory = session_factory
        self._parts = [(part.id, part.name) for part in matcher.parts]
        self._part_ids = {part_id for part_id, _ in self._parts}
        self._store = store

    async def queue(
        self,
        *,
        status: str = "unreviewed",
        source: str | None = None,
        part_id: str | None = None,
        is_active: bool | None = True,
        min_score: int | None = None,
        max_score: int | None = None,
        has_images: bool | None = True,
        match_state: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> ReviewQueueResponse:
        async with self._session_factory() as session:
            # ponytail: Python-side filtering fits a personal SQLite queue; use SQL if it grows.
            stmt = select(ListingRow)
            if source is not None:
                stmt = stmt.where(ListingRow.source == source)
            if is_active is not None:
                stmt = stmt.where(ListingRow.is_active == is_active)
            listings = list((await session.execute(stmt)).scalars())
            items = await self._queue_items(session, listings, current_images_only=True)

        filtered = []
        for item in items:
            if status == "unreviewed" and item.latest_review is not None:
                continue
            if status == "reviewed" and item.latest_review is None:
                continue
            if status in {"confirmed", "rejected", "uncertain"} and (
                item.latest_review is None or item.latest_review.outcome != status
            ):
                continue
            if part_id is not None and item.effective_part_id != part_id:
                continue
            score = item.deterministic_match.total_score if item.deterministic_match else None
            if min_score is not None and (score is None or score < min_score):
                continue
            if max_score is not None and (score is None or score > max_score):
                continue
            if has_images is not None and bool(item.images) is not has_images:
                continue
            if match_state == "matched" and item.deterministic_match is None:
                continue
            if match_state == "unmatched" and item.deterministic_match is not None:
                continue
            filtered.append(item)
        filtered.sort(
            key=lambda item: (
                item.deterministic_match is not None,
                item.deterministic_match.total_score if item.deterministic_match else 0,
                item.first_seen_at,
                item.listing_id,
            ),
            reverse=True,
        )
        return ReviewQueueResponse(
            items=filtered[offset : offset + limit],
            total=len(filtered),
            limit=limit,
            offset=offset,
        )

    async def progress(self) -> ReviewProgressResponse:
        async with self._session_factory() as session:
            listings = list((await session.execute(select(ListingRow))).scalars())
            items = await self._queue_items(session, listings, current_images_only=True)
            references = (
                await session.execute(
                    select(ReferenceImageRow, ListingImageRow.listing_id)
                    .join(
                        ListingImageRow,
                        ListingImageRow.id == ReferenceImageRow.listing_image_id,
                    )
                    .where(ReferenceImageRow.is_active.is_(True))
                )
            ).all()

        reviewed = [item for item in items if item.latest_review is not None]
        outcomes = Counter(item.latest_review.outcome for item in reviewed if item.latest_review)
        source_counts = Counter(item.source for item in reviewed)
        confirmed = Counter(
            item.latest_review.selected_part_id
            for item in reviewed
            if item.latest_review
            and item.latest_review.outcome == "confirmed"
            and item.latest_review.selected_part_id in self._part_ids
        )
        reference_counts: Counter[tuple[str, str]] = Counter()
        reference_listings: dict[tuple[str, str], set[int]] = {}
        for reference, listing_id in references:
            key = (reference.part_id, reference.label)
            reference_counts[key] += 1
            reference_listings.setdefault(key, set()).add(listing_id)

        parts: list[ReviewPartProgress] = []
        for part_id, part_name in self._parts:
            values = {
                "confirmed_listings": confirmed[part_id],
                "positive_references": reference_counts[(part_id, "positive")],
                "positive_listings": len(reference_listings.get((part_id, "positive"), set())),
                "negative_references": reference_counts[(part_id, "negative")],
                "negative_listings": len(reference_listings.get((part_id, "negative"), set())),
            }
            missing = [
                f"{target - values[name]} more {name.replace('_', ' ')}"
                for name, target in _COVERAGE_TARGETS.items()
                if values[name] < target
            ]
            parts.append(
                ReviewPartProgress(
                    part_id=part_id,
                    part_name=part_name,
                    **values,
                    missing_requirements=missing,
                    coverage_ready=not missing,
                )
            )

        unreviewed = [
            item for item in items if item.is_active and item.images and item.latest_review is None
        ]
        reviewed_count = len(reviewed)
        return ReviewProgressResponse(
            target_reviews=_CAMPAIGN_TARGET,
            reviewed_listings=reviewed_count,
            remaining_reviews=max(_CAMPAIGN_TARGET - reviewed_count, 0),
            campaign_complete=reviewed_count >= _CAMPAIGN_TARGET,
            coverage_complete=all(part.coverage_ready for part in parts),
            outcomes=ReviewOutcomeProgress(
                confirmed=outcomes["confirmed"],
                rejected=outcomes["rejected"],
                uncertain=outcomes["uncertain"],
            ),
            queue=ReviewQueueProgress(
                unreviewed_matched=sum(
                    item.deterministic_match is not None for item in unreviewed
                ),
                unreviewed_unmatched=sum(item.deterministic_match is None for item in unreviewed),
            ),
            sources=[
                ReviewSourceProgress(source=source, reviewed_listings=source_counts[source])
                for source in sorted({item.source for item in items})
            ],
            parts=parts,
        )

    async def detail(self, listing_id: int) -> ReviewListingDetailResponse:
        async with self._session_factory() as session:
            listing = await session.get(ListingRow, listing_id)
            if listing is None:
                raise ReviewNotFoundError
            item = (await self._queue_items(session, [listing], current_images_only=False))[0]
            reviews = list(
                (
                    await session.execute(
                        select(ManualReviewRow)
                        .where(ManualReviewRow.listing_id == listing_id)
                        .order_by(ManualReviewRow.reviewed_at.desc(), ManualReviewRow.id.desc())
                    )
                ).scalars()
            )
            references = await self._reference_rows(session, listing_id=listing_id)
        return ReviewListingDetailResponse(
            **item.model_dump(),
            review_history=[self._review_response(row) for row in reviews],
            references=references,
        )

    async def submit(
        self, listing_id: int, request: ManualReviewRequest
    ) -> ManualReviewCreatedResponse:
        if request.selected_part_id and request.selected_part_id not in self._part_ids:
            raise ValueError("Unknown selected part")
        async with self._session_factory() as session:
            listing = await session.get(ListingRow, listing_id)
            if listing is None:
                raise ReviewNotFoundError
            image_ids = {reference.listing_image_id for reference in request.references}
            images = list(
                (
                    await session.execute(
                        select(ListingImageRow).where(
                            ListingImageRow.listing_id == listing_id,
                            ListingImageRow.id.in_(image_ids),
                        )
                    )
                ).scalars()
            )
            if len(images) != len(image_ids):
                raise ValueError("A selected image does not belong to this listing")
            review = ManualReviewRow(
                listing_id=listing_id,
                outcome=request.outcome,
                selected_part_id=request.selected_part_id,
                notes=request.notes,
                reviewed_at=datetime.now(UTC),
            )
            session.add(review)
            await session.commit()
            await session.refresh(review)
            review_response = self._review_response(review)
            source = listing.source
            image_map = {image.id: image for image in images}

        results: list[ReferenceResultResponse] = []
        assert request.selected_part_id is not None or not request.references
        for selection in request.references:
            try:
                result = await self._save_reference(
                    source=source,
                    image=image_map[selection.listing_image_id],
                    review_id=review_response.id,
                    part_id=request.selected_part_id or "",
                    label=selection.label,
                    notes=selection.notes,
                )
            except ReferenceImageError as exc:
                result = ReferenceResultResponse(
                    listing_image_id=selection.listing_image_id,
                    status="failed",
                    detail=str(exc),
                )
            results.append(result)
        return ManualReviewCreatedResponse(review=review_response, references=results)

    async def references(
        self,
        *,
        part_id: str | None = None,
        label: str | None = None,
        is_active: bool | None = None,
    ) -> list[ReferenceImageResponse]:
        async with self._session_factory() as session:
            return await self._reference_rows(
                session, part_id=part_id, label=label, is_active=is_active
            )

    async def update_reference(
        self, reference_id: int, request: ReferenceUpdateRequest
    ) -> ReferenceImageResponse:
        async with self._session_factory() as session:
            row = await session.get(ReferenceImageRow, reference_id)
            if row is None:
                raise ReviewNotFoundError
            if request.is_active:
                try:
                    stored_file = self._store.resolve(row.local_path)
                except ReferenceImageError:
                    raise ReviewConflictError from None
                if not stored_file.is_file():
                    raise ReviewConflictError
                conflict = await session.scalar(
                    select(ReferenceImageRow.id).where(
                        ReferenceImageRow.id != row.id,
                        ReferenceImageRow.part_id == row.part_id,
                        ReferenceImageRow.content_sha256 == row.content_sha256,
                        ReferenceImageRow.is_active.is_(True),
                    )
                )
                if conflict is not None:
                    raise ReviewConflictError
            if "notes" in request.model_fields_set:
                row.notes = request.notes
            if request.is_active is not None:
                row.is_active = request.is_active
            await session.commit()
            listing_id = await session.scalar(
                select(ListingImageRow.listing_id).where(
                    ListingImageRow.id == row.listing_image_id
                )
            )
            assert listing_id is not None
            return self._reference_response(row, listing_id)

    async def content_path(self, reference_id: int) -> Path:
        async with self._session_factory() as session:
            row = await session.get(ReferenceImageRow, reference_id)
            if row is None:
                raise ReviewNotFoundError
            try:
                path = self._store.resolve(row.local_path)
            except ReferenceImageError:
                raise ReviewNotFoundError from None
        if not path.is_file():
            raise ReviewNotFoundError
        return path

    async def _save_reference(
        self,
        *,
        source: str,
        image: ListingImageRow,
        review_id: int,
        part_id: str,
        label: str,
        notes: str | None,
    ) -> ReferenceResultResponse:
        async with self._session_factory() as session:
            prior = (
                await session.execute(
                    select(ReferenceImageRow).where(
                        ReferenceImageRow.listing_image_id == image.id,
                        ReferenceImageRow.part_id == part_id,
                        ReferenceImageRow.label == label,
                    )
                )
            ).scalar_one_or_none()
            if prior is not None and self._store.resolve(prior.local_path).is_file():
                if prior.is_active:
                    return ReferenceResultResponse(
                        listing_image_id=image.id,
                        status="existing",
                        reference=self._reference_response(prior, image.listing_id),
                    )
                conflict = await self._active_conflict(session, prior)
                if conflict:
                    return ReferenceResultResponse(
                        listing_image_id=image.id,
                        status="conflict",
                        detail="The same image already has another active label for this part",
                    )
                prior.is_active = True
                prior.notes = notes
                prior.manual_review_id = review_id
                await session.commit()
                return ReferenceResultResponse(
                    listing_image_id=image.id,
                    status="reactivated",
                    reference=self._reference_response(prior, image.listing_id),
                )

        downloaded = await self._store.download(source, image.source_url)
        local_path, file_created = await self._store.store(part_id, downloaded)
        try:
            async with self._session_factory() as session:
                same = (
                    await session.execute(
                        select(ReferenceImageRow).where(
                            ReferenceImageRow.part_id == part_id,
                            ReferenceImageRow.label == label,
                            ReferenceImageRow.content_sha256 == downloaded.sha256,
                        )
                    )
                ).scalar_one_or_none()
                if same is not None:
                    same_listing_id = await session.scalar(
                        select(ListingImageRow.listing_id).where(
                            ListingImageRow.id == same.listing_image_id
                        )
                    )
                    assert same_listing_id is not None
                    if same.is_active:
                        status: Literal["existing", "reactivated"] = "existing"
                    elif await self._active_conflict(session, same):
                        return ReferenceResultResponse(
                            listing_image_id=image.id,
                            status="conflict",
                            detail="The same image already has another active label for this part",
                        )
                    else:
                        same.is_active = True
                        same.notes = notes
                        same.manual_review_id = review_id
                        status = "reactivated"
                        await session.commit()
                    return ReferenceResultResponse(
                        listing_image_id=image.id,
                        status=status,
                        reference=self._reference_response(same, same_listing_id),
                    )
                opposite = await session.scalar(
                    select(ReferenceImageRow.id).where(
                        ReferenceImageRow.part_id == part_id,
                        ReferenceImageRow.content_sha256 == downloaded.sha256,
                        ReferenceImageRow.is_active.is_(True),
                    )
                )
                if opposite is not None:
                    return ReferenceResultResponse(
                        listing_image_id=image.id,
                        status="conflict",
                        detail="The same image already has another active label for this part",
                    )
                row = ReferenceImageRow(
                    listing_image_id=image.id,
                    manual_review_id=review_id,
                    part_id=part_id,
                    label=label,
                    local_path=local_path,
                    content_sha256=downloaded.sha256,
                    mime_type="image/webp",
                    width=downloaded.width,
                    height=downloaded.height,
                    notes=notes,
                    is_active=True,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return ReferenceResultResponse(
                    listing_image_id=image.id,
                    status="created",
                    reference=self._reference_response(row, image.listing_id),
                )
        except IntegrityError as exc:
            raise ReferenceImageError("Reference conflicts with an existing approval") from exc
        finally:
            if file_created:
                async with self._session_factory() as session:
                    used = await session.scalar(
                        select(func.count(ReferenceImageRow.id)).where(
                            ReferenceImageRow.local_path == local_path
                        )
                    )
                if not used:
                    self._store.resolve(local_path).unlink(missing_ok=True)

    @staticmethod
    async def _active_conflict(session: AsyncSession, row: ReferenceImageRow) -> bool:
        return (
            await session.scalar(
                select(ReferenceImageRow.id).where(
                    ReferenceImageRow.id != row.id,
                    ReferenceImageRow.part_id == row.part_id,
                    ReferenceImageRow.content_sha256 == row.content_sha256,
                    ReferenceImageRow.is_active.is_(True),
                )
            )
        ) is not None

    async def _queue_items(
        self,
        session: AsyncSession,
        listings: list[ListingRow],
        *,
        current_images_only: bool,
    ) -> list[ReviewQueueItemResponse]:
        if not listings:
            return []
        listing_ids = [listing.id for listing in listings]
        image_stmt = select(ListingImageRow).where(ListingImageRow.listing_id.in_(listing_ids))
        if current_images_only:
            image_stmt = image_stmt.where(ListingImageRow.is_current.is_(True))
        images = list(
            (
                await session.execute(
                    image_stmt.order_by(ListingImageRow.position, ListingImageRow.id)
                )
            ).scalars()
        )
        matches = list(
            (
                await session.execute(
                    select(PartMatchRow).where(PartMatchRow.listing_id.in_(listing_ids))
                )
            ).scalars()
        )
        reviews = list(
            (
                await session.execute(
                    select(ManualReviewRow)
                    .where(ManualReviewRow.listing_id.in_(listing_ids))
                    .order_by(ManualReviewRow.reviewed_at.desc(), ManualReviewRow.id.desc())
                )
            ).scalars()
        )
        images_by_listing: dict[int, list[ListingImageRow]] = {}
        for image in images:
            images_by_listing.setdefault(image.listing_id, []).append(image)
        matches_by_listing = {match.listing_id: match for match in matches}
        reviews_by_listing: dict[int, list[ManualReviewRow]] = {}
        for review in reviews:
            reviews_by_listing.setdefault(review.listing_id, []).append(review)

        return [
            self._queue_item(
                listing,
                images_by_listing.get(listing.id, []),
                matches_by_listing.get(listing.id),
                reviews_by_listing.get(listing.id, []),
            )
            for listing in listings
        ]

    def _queue_item(
        self,
        listing: ListingRow,
        images: list[ListingImageRow],
        match: PartMatchRow | None,
        reviews: list[ManualReviewRow],
    ) -> ReviewQueueItemResponse:
        latest = reviews[0] if reviews else None
        match_response = self._match_response(match) if match else None
        return ReviewQueueItemResponse(
            listing_id=listing.id,
            source=listing.source,
            title=listing.title,
            description=listing.description or "",
            url=listing.url,
            price=listing.price,
            currency=listing.currency,
            condition=listing.condition,
            published_at=listing.published_at,
            first_seen_at=listing.first_seen_at,
            last_seen_at=listing.last_seen_at,
            is_active=listing.is_active,
            images=[self._image_response(image) for image in images],
            deterministic_match=match_response,
            effective_part_id=(latest.selected_part_id if latest else None)
            or (match.part_id if match else None),
            latest_review=self._review_response(latest) if latest else None,
            review_history_count=len(reviews),
        )

    @staticmethod
    def _match_response(row: PartMatchRow) -> ReviewMatchResponse:
        try:
            reasons = json.loads(row.reasons_json or "[]")
            if not isinstance(reasons, list) or not all(
                isinstance(reason, dict) for reason in reasons
            ):
                reasons = []
        except (json.JSONDecodeError, TypeError):
            reasons = []
        return ReviewMatchResponse(
            part_id=row.part_id,
            part_name=row.part_name,
            total_score=row.total_score,
            compatibility_status=row.compatibility_status,
            reasons=reasons,
            algorithm_version=row.algorithm_version,
        )

    @staticmethod
    def _image_response(row: ListingImageRow) -> ListingImageResponse:
        return ListingImageResponse(
            id=row.id,
            source_url=row.source_url,
            position=row.position,
            is_current=row.is_current,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
        )

    @staticmethod
    def _review_response(row: ManualReviewRow) -> ManualReviewResponse:
        return ManualReviewResponse(
            id=row.id,
            listing_id=row.listing_id,
            outcome=row.outcome,  # type: ignore[arg-type]
            selected_part_id=row.selected_part_id,
            notes=row.notes,
            reviewed_at=row.reviewed_at,
        )

    async def _reference_rows(
        self,
        session: AsyncSession,
        *,
        listing_id: int | None = None,
        part_id: str | None = None,
        label: str | None = None,
        is_active: bool | None = None,
    ) -> list[ReferenceImageResponse]:
        stmt = select(ReferenceImageRow, ListingImageRow.listing_id).join(
            ListingImageRow, ListingImageRow.id == ReferenceImageRow.listing_image_id
        )
        if listing_id is not None:
            stmt = stmt.where(ListingImageRow.listing_id == listing_id)
        if part_id is not None:
            stmt = stmt.where(ReferenceImageRow.part_id == part_id)
        if label is not None:
            stmt = stmt.where(ReferenceImageRow.label == label)
        if is_active is not None:
            stmt = stmt.where(ReferenceImageRow.is_active == is_active)
        result = await session.execute(stmt.order_by(ReferenceImageRow.created_at.desc()))
        return [self._reference_response(row, row_listing_id) for row, row_listing_id in result]

    @staticmethod
    def _reference_response(row: ReferenceImageRow, listing_id: int) -> ReferenceImageResponse:
        return ReferenceImageResponse(
            id=row.id,
            listing_id=listing_id,
            listing_image_id=row.listing_image_id,
            manual_review_id=row.manual_review_id,
            part_id=row.part_id,
            label=row.label,  # type: ignore[arg-type]
            content_sha256=row.content_sha256,
            mime_type=row.mime_type,
            width=row.width,
            height=row.height,
            notes=row.notes,
            is_active=row.is_active,
            created_at=row.created_at,
            content_url=f"/review/references/{row.id}/content",
        )
