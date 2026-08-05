from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select

from irmscher_tracker.db.models import (
    ImageEmbeddingRow,
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    NotificationRow,
    PartMatchRow,
    ReferenceImageRow,
    VisualMatchRow,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.services.review import (
    ReferenceImageError,
    ReferenceImageStore,
    ReviewService,
)
from irmscher_tracker.services.review_integrity import ReviewIntegrityService
from irmscher_tracker.settings import Settings
from irmscher_tracker.vision.alerts import (
    VisionAlertService,
    VisualAlertPreview,
    _contact_sheet,
    future_visual_alert_eligible,
    future_visual_event_key,
)
from irmscher_tracker.vision.embeddings import (
    EmbeddingIntegrityError,
    deserialize_vector,
    normalize_rows,
    serialize_vector,
)
from irmscher_tracker.vision.evaluation import VisionEvaluator
from irmscher_tracker.vision.image_loader import LoadedVisionImage
from irmscher_tracker.vision.model import Dinov2Embedder
from irmscher_tracker.vision.service import VisionRunBusyError, VisionService
from irmscher_tracker.vision.similarity import ReferenceVector, rank_parts


class FakeEmbedder:
    model_id = "fake/dinov2"
    resolved_revision = "fake-commit"
    model_fingerprint = "f" * 64
    preprocessing_version = "fake-v1"
    embedding_dimension = 3
    load_time_seconds = 0.01
    last_inference_seconds = 0.001

    def __init__(self) -> None:
        self.embed_calls = 0
        self.released = False

    async def warmup(self) -> np.ndarray:
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)

    async def embed(self, images: list[Image.Image]) -> np.ndarray:
        self.embed_calls += 1
        values = [np.asarray(image.resize((1, 1))).reshape(-1)[:3] for image in images]
        return normalize_rows(values)

    def release(self) -> None:
        self.released = True


class FakeLoader:
    async def listing(self, source: str, source_url: str) -> LoadedVisionImage:
        del source
        color = (255, 0, 0) if "red" in source_url else (0, 0, 255)
        image = Image.new("RGB", (8, 8), color)
        return LoadedVisionImage(image, source_url.rsplit("/", 1)[-1].ljust(64, "0")[:64], 8, 8)

    async def reference(self, row: ReferenceImageRow) -> LoadedVisionImage:
        image = Image.new("RGB", (8, 8), (255, 0, 0))
        return LoadedVisionImage(image, row.content_sha256, 8, 8)


class FailingLoader(FakeLoader):
    async def listing(self, source: str, source_url: str) -> LoadedVisionImage:
        del source, source_url
        raise ReferenceImageError("Image download failed")


class PassingIntegrity:
    async def check(self) -> SimpleNamespace:
        return SimpleNamespace(status="ok")


class FakeNotifier:
    def __init__(self) -> None:
        self.content = b""
        self.caption = ""

    async def send_photo(self, content: bytes, caption: str) -> None:
        self.content = content
        self.caption = caption


def _settings(settings: Settings, tmp_path: Path, **changes: object) -> Settings:
    return settings.model_copy(
        update={
            "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
            "vision_enabled": True,
            "vision_batch_size": 2,
            "vision_max_listings_per_run": 20,
            "vision_max_images_per_listing": 8,
            **changes,
        }
    )


def _listing(identifier: str, *, source: str = "ebay") -> ListingRow:
    now = datetime.now(UTC)
    return ListingRow(
        source=source,
        external_id=identifier,
        title=f"Listing {identifier}",
        description="",
        url=f"https://example.test/{identifier}",
        image_urls_json="[]",
        price=Decimal("10"),
        currency="EUR",
        condition="used",
        seller_display="",
        seller_location="",
        published_at=now,
        first_seen_at=now,
        last_seen_at=now,
        is_active=True,
    )


def _embedding(
    *,
    vector: list[float],
    listing_image_id: int | None = None,
    reference_image_id: int | None = None,
    content_hash: str = "a" * 64,
    fingerprint: str = "f" * 64,
) -> ImageEmbeddingRow:
    blob, dimension = serialize_vector(vector)
    return ImageEmbeddingRow(
        listing_image_id=listing_image_id,
        reference_image_id=reference_image_id,
        owner_type="listing" if listing_image_id is not None else "reference",
        content_sha256=content_hash,
        model_id="fake/dinov2",
        model_revision="fake-commit",
        model_fingerprint=fingerprint,
        preprocessing_version="fake-v1",
        embedding_dim=dimension,
        dtype="float32",
        vector_blob=blob,
        created_at=datetime.now(UTC),
    )


def test_embedding_round_trip_normalizes_and_rejects_invalid_storage() -> None:
    vectors = normalize_rows([[3, 4], [0, 2]])
    assert vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), [1, 1])
    blob, dimension = serialize_vector([3, 4])
    assert dimension == 2
    assert np.allclose(deserialize_vector(blob, dimension, "float32"), [0.6, 0.8])
    with pytest.raises(EmbeddingIntegrityError):
        deserialize_vector(blob[:-1], dimension, "float32")
    with pytest.raises(EmbeddingIntegrityError):
        deserialize_vector(np.array([2, 0], np.float32).tobytes(), 2, "float32")
    with pytest.raises(EmbeddingIntegrityError):
        normalize_rows([0, 0])


def test_similarity_is_part_specific_leak_free_and_deterministic() -> None:
    references = [
        ReferenceVector(1, 10, "a", "positive", "p" * 64, np.array([1, 0], np.float32)),
        ReferenceVector(2, 11, "a", "negative", "n" * 64, np.array([0, 1], np.float32)),
        ReferenceVector(3, 99, "a", "positive", "x" * 64, np.array([1, 0], np.float32)),
        ReferenceVector(4, 12, "b", "positive", "q" * 64, np.array([1, 0], np.float32)),
    ]
    ranked = rank_parts(
        np.array([1, 0], np.float32),
        references,
        ["b", "a", "c"],
        query_listing_id=99,
        query_content_sha256="x" * 64,
        min_positive=0.9,
        min_margin=0.5,
    )
    assert [item.part_id for item in ranked] == ["a", "b", "c"]
    a, b, c = ranked
    assert (a.best_positive_reference_id, a.best_negative_reference_id) == (1, 2)
    assert a.positive_similarity == pytest.approx(1)
    assert a.negative_similarity == pytest.approx(0)
    assert a.similarity_margin == pytest.approx(1)
    assert a.status == "review_candidate"
    assert b.status == "positive_only" and b.negative_similarity is None
    assert c.status == "insufficient_references" and c.positive_similarity is None


def test_vision_settings_require_explicit_alert_thresholds(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_token=settings.api_token,
            vision_alerts_enabled=True,
        )
    configured = Settings(
        _env_file=None,
        api_token=settings.api_token,
        vision_alerts_enabled=True,
        vision_alert_min_positive=0.8,
        vision_alert_min_margin=0.1,
    )
    assert configured.vision_alerts_enabled
    assert configured.vision_device == "cpu"


@pytest.mark.asyncio
async def test_vision_run_lock_and_stale_recovery(
    settings: Settings, session_factory, tmp_path: Path
) -> None:
    store = ReferenceImageStore(tmp_path)
    service = VisionService(
        session_factory,
        _settings(settings, tmp_path),
        ["a"],
        store,
        FakeEmbedder(),
        PassingIntegrity(),  # type: ignore[arg-type]
    )
    try:
        first = await service.reserve("listing_scan")
        with pytest.raises(VisionRunBusyError) as error:
            await service.reserve("evaluation")
        assert error.value.run_id == first
        assert await service.recover_stale_runs() == 1
        assert (await service.run(first)).status == "interrupted"  # type: ignore[union-attr]
        second = await service.reserve("evaluation")
        assert second > first
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_listing_scan_reuses_embeddings_and_force_replaces_current_model(
    settings: Settings, session_factory, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        positive_listing = _listing("positive")
        negative_listing = _listing("negative")
        query_listing = _listing("query")
        session.add_all([positive_listing, negative_listing, query_listing])
        await session.flush()
        images = [
            ListingImageRow(
                listing_id=positive_listing.id,
                source_url="https://i.ebayimg.com/positive.webp",
                position=0,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
            ListingImageRow(
                listing_id=negative_listing.id,
                source_url="https://i.ebayimg.com/negative.webp",
                position=0,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
            ListingImageRow(
                listing_id=query_listing.id,
                source_url="https://i.ebayimg.com/red.webp",
                position=0,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
            ListingImageRow(
                listing_id=query_listing.id,
                source_url="https://i.ebayimg.com/blue.webp",
                position=1,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
        ]
        session.add_all(images)
        await session.flush()
        reviews = [
            ManualReviewRow(
                listing_id=positive_listing.id,
                outcome="confirmed",
                selected_part_id="a",
                notes=None,
                reviewed_at=now,
            ),
            ManualReviewRow(
                listing_id=negative_listing.id,
                outcome="rejected",
                selected_part_id="a",
                notes=None,
                reviewed_at=now,
            ),
        ]
        session.add_all(reviews)
        await session.flush()
        references = [
            ReferenceImageRow(
                listing_image_id=images[0].id,
                manual_review_id=reviews[0].id,
                part_id="a",
                label="positive",
                local_path="references/a/pos.webp",
                content_sha256="p" * 64,
                mime_type="image/webp",
                width=8,
                height=8,
                notes=None,
                is_active=True,
                created_at=now,
            ),
            ReferenceImageRow(
                listing_image_id=images[1].id,
                manual_review_id=reviews[1].id,
                part_id="a",
                label="negative",
                local_path="references/a/neg.webp",
                content_sha256="n" * 64,
                mime_type="image/webp",
                width=8,
                height=8,
                notes=None,
                is_active=True,
                created_at=now,
            ),
        ]
        session.add_all(references)
        await session.flush()
        session.add_all(
            [
                _embedding(
                    vector=[1, 0, 0],
                    reference_image_id=references[0].id,
                    content_hash="p" * 64,
                ),
                _embedding(
                    vector=[0, 0, 1],
                    reference_image_id=references[1].id,
                    content_hash="n" * 64,
                ),
            ]
        )
        await session.commit()
        query_id = query_listing.id

    store = ReferenceImageStore(tmp_path)
    embedder = FakeEmbedder()
    service = VisionService(
        session_factory,
        _settings(
            settings,
            tmp_path,
            vision_review_min_positive=0.9,
            vision_review_min_margin=0.5,
        ),
        ["a"],
        store,
        embedder,
        PassingIntegrity(),  # type: ignore[arg-type]
    )
    service.loader = FakeLoader()  # type: ignore[assignment]
    try:
        first = await service.reserve("listing_scan")
        await service.scan(first, listing_id=query_id)
        assert embedder.embed_calls == 1
        async with session_factory() as session:
            match = await session.scalar(select(VisualMatchRow))
            assert match is not None
            assert match.status == "review_candidate"
            assert match.positive_similarity == pytest.approx(1)
            strongest_image = await session.get(ListingImageRow, match.listing_image_id)
            assert strongest_image is not None
            assert strongest_image.source_url.endswith("/red.webp")

        second = await service.reserve("listing_scan")
        await service.scan(second, listing_id=query_id)
        assert embedder.embed_calls == 1
        assert (await service.run(second)).skipped_count == 2  # type: ignore[union-attr]

        third = await service.reserve("listing_scan")
        await service.scan(third, listing_id=query_id, force=True)
        assert embedder.embed_calls == 2
        async with session_factory() as session:
            assert await session.scalar(select(func.count(VisualMatchRow.id))) == 1
            assert await session.scalar(select(func.count(ImageEmbeddingRow.id))) == 4

        async with session_factory() as session:
            positive = await session.scalar(
                select(ReferenceImageRow).where(ReferenceImageRow.label == "positive")
            )
            assert positive is not None
            positive.is_active = False
            await session.commit()
        fourth = await service.reserve("listing_scan")
        await service.scan(fourth, listing_id=query_id, force=True)
        async with session_factory() as session:
            match = await session.scalar(select(VisualMatchRow))
            assert match is not None and match.status == "insufficient_references"

        service.loader = FailingLoader()  # type: ignore[assignment]
        partial = await service.reserve("listing_scan")
        await service.scan(partial, listing_id=query_id)
        partial_row = await service.run(partial)
        assert partial_row is not None
        assert (partial_row.status, partial_row.failed_count) == ("partial", 1)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reference_rebuild_reuses_current_cache_and_preserves_other_models(
    settings: Settings, session_factory, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        listing = _listing("reference-owner")
        session.add(listing)
        await session.flush()
        images = []
        reviews = []
        for index in range(3):
            image = ListingImageRow(
                listing_id=listing.id,
                source_url=f"https://i.ebayimg.com/reference-{index}.webp",
                position=index,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            )
            review = ManualReviewRow(
                listing_id=listing.id,
                outcome="confirmed",
                selected_part_id="a",
                notes=None,
                reviewed_at=now,
            )
            session.add_all([image, review])
            images.append(image)
            reviews.append(review)
        await session.flush()
        references = []
        for index, (image, review) in enumerate(zip(images, reviews, strict=True)):
            reference = ReferenceImageRow(
                listing_image_id=image.id,
                manual_review_id=review.id,
                part_id="a",
                label="positive" if index < 2 else "negative",
                local_path=f"references/a/{index}.webp",
                content_sha256=str(index) * 64,
                mime_type="image/webp",
                width=8,
                height=8,
                notes=None,
                is_active=index < 2,
                created_at=now,
            )
            session.add(reference)
            references.append(reference)
        await session.flush()
        session.add_all(
            [
                _embedding(
                    vector=[1, 0, 0],
                    reference_image_id=references[0].id,
                    content_hash=references[0].content_sha256,
                ),
                _embedding(
                    vector=[1, 0, 0],
                    reference_image_id=references[1].id,
                    content_hash=references[1].content_sha256,
                    fingerprint="e" * 64,
                ),
            ]
        )
        await session.commit()

    store = ReferenceImageStore(tmp_path)
    embedder = FakeEmbedder()
    service = VisionService(
        session_factory,
        _settings(settings, tmp_path),
        ["a"],
        store,
        embedder,
        PassingIntegrity(),  # type: ignore[arg-type]
    )
    service.loader = FakeLoader()  # type: ignore[assignment]
    try:
        run_id = await service.reserve("reference_rebuild")
        await service.rebuild_references(run_id)
        run = await service.run(run_id)
        assert run is not None
        assert (run.processed_count, run.skipped_count, run.failed_count) == (1, 1, 0)
        async with session_factory() as session:
            assert await session.scalar(select(func.count(ImageEmbeddingRow.id))) == 3
            assert (
                await session.scalar(
                    select(func.count(ImageEmbeddingRow.id)).where(
                        ImageEmbeddingRow.model_fingerprint == "e" * 64
                    )
                )
                == 1
            )

        forced = await service.reserve("reference_rebuild")
        await service.rebuild_references(forced, force=True)
        async with session_factory() as session:
            assert await session.scalar(select(func.count(ImageEmbeddingRow.id))) == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_integrity_reports_invalid_vector(
    settings: Settings, session_factory, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        listing = _listing("bad-vector")
        listing.image_urls_json = '["https://i.ebayimg.com/bad.webp"]'
        session.add(listing)
        await session.flush()
        image = ListingImageRow(
            listing_id=listing.id,
            source_url="https://i.ebayimg.com/bad.webp",
            position=0,
            is_current=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(image)
        await session.flush()
        invalid = _embedding(vector=[1, 0], listing_image_id=image.id)
        invalid.vector_blob = b"short"
        session.add(invalid)
        await session.commit()
    result = await ReviewIntegrityService(session_factory, tmp_path, {"a"}).check()
    check = next(item for item in result.checks if item.name == "vision_embedding_vectors")
    assert check.status == "error"
    assert check.affected_count == 1


@pytest.mark.asyncio
async def test_evaluation_is_leave_one_listing_out_and_writes_reports(
    settings: Settings, session_factory, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        confirmed = _listing("confirmed")
        rejected = _listing("rejected")
        session.add_all([confirmed, rejected])
        await session.flush()
        images = []
        for position, listing in enumerate((confirmed, rejected), start=1):
            image = ListingImageRow(
                listing_id=listing.id,
                source_url=f"https://i.ebayimg.com/{position}.webp",
                position=0,
                is_current=True,
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(image)
            images.append(image)
        await session.flush()
        session.add_all(
            [
                ManualReviewRow(
                    listing_id=confirmed.id,
                    outcome="confirmed",
                    selected_part_id="a",
                    notes=None,
                    reviewed_at=now,
                ),
                ManualReviewRow(
                    listing_id=rejected.id,
                    outcome="rejected",
                    selected_part_id="a",
                    notes=None,
                    reviewed_at=now,
                ),
                _embedding(vector=[1, 0], listing_image_id=images[0].id, content_hash="x" * 64),
                _embedding(vector=[1, 0], listing_image_id=images[1].id, content_hash="y" * 64),
            ]
        )
        await session.commit()
        confirmed_id = confirmed.id

    references = [
        ReferenceVector(1, confirmed_id, "a", "positive", "x" * 64, np.array([1, 0], np.float32)),
        ReferenceVector(2, 10, "a", "positive", "p" * 64, np.array([0.8, 0.2], np.float32)),
        ReferenceVector(3, 11, "a", "positive", "q" * 64, np.array([0.9, 0.1], np.float32)),
        ReferenceVector(4, 12, "a", "negative", "n" * 64, np.array([0, 1], np.float32)),
    ]
    report, json_path, csv_path = await VisionEvaluator(
        session_factory, _settings(settings, tmp_path), ["a", "b"]
    ).evaluate("f" * 64, references)
    assert report["total_evaluated_listings"] == 2
    assert report["top_1_accuracy"] == 1
    assert report["top_3_recall"] == 1
    assert report["mean_reciprocal_rank"] == 1
    assert len(report["targeted_rejected_false_positives"]) == 1  # type: ignore[arg-type]
    assert json_path.is_file() and csv_path.is_file()
    result = next(row for row in report["results"] if row["listing_id"] == confirmed_id)  # type: ignore[union-attr]
    assert result["positive_similarity"] < 1
    assert report["per_part"]["b"]["top_1_accuracy"] is None  # type: ignore[index]
    assert "b" in report["parts_with_insufficient_distinct_reference_listings"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_review_keeps_deterministic_human_and_visual_evidence_separate(
    settings: Settings, session_factory, tmp_path: Path, parts_config_path: Path
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        listing = _listing("three-evidence-types")
        listing.image_urls_json = '["https://i.ebayimg.com/red.webp"]'
        session.add(listing)
        await session.flush()
        image = ListingImageRow(
            listing_id=listing.id,
            source_url="https://i.ebayimg.com/red.webp",
            position=0,
            is_current=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(image)
        await session.flush()
        session.add_all(
            [
                PartMatchRow(
                    listing_id=listing.id,
                    part_id="front-lip",
                    part_name="Front lip",
                    total_score=80,
                    compatibility_status="compatible",
                    reasons_json="[]",
                    algorithm_version="test",
                    matched_at=now,
                ),
                ManualReviewRow(
                    listing_id=listing.id,
                    outcome="confirmed",
                    selected_part_id="grille",
                    notes=None,
                    reviewed_at=now,
                ),
                _embedding(vector=[1, 0], listing_image_id=image.id, content_hash="z" * 64),
                VisualMatchRow(
                    listing_image_id=image.id,
                    part_id="roof-spoiler",
                    model_fingerprint="f" * 64,
                    best_positive_reference_id=None,
                    best_negative_reference_id=None,
                    positive_similarity=0.8,
                    negative_similarity=0.2,
                    similarity_margin=0.6,
                    positive_reference_count=2,
                    negative_reference_count=1,
                    rank_for_listing=1,
                    status="ranked",
                    computed_at=now,
                ),
            ]
        )
        await session.commit()
        listing_id = listing.id

    store = ReferenceImageStore(tmp_path)
    service = ReviewService(
        session_factory,
        PartMatcher(parts_config_path),
        store,
        _settings(settings, tmp_path),
    )
    try:
        queue = await service.queue(mode="visual-candidates", status="all")
        assert queue.total == 1
        item = queue.items[0]
        assert item.deterministic_match.part_id == "front-lip"  # type: ignore[union-attr]
        assert item.latest_review.selected_part_id == "grille"  # type: ignore[union-attr]
        assert item.visual_evidence[0].part_id == "roof-spoiler"
        async with session_factory() as session:
            assert await session.scalar(select(func.count(VisualMatchRow.id))) == 1
            assert await session.scalar(select(func.count(ManualReviewRow.id))) == 1
            assert await session.get(ListingRow, listing_id) is not None
    finally:
        await store.close()


def test_visual_alert_preview_and_contact_sheet_are_explicitly_test_only() -> None:
    preview = VisualAlertPreview(
        match_id=1,
        listing_id=2,
        listing_title="Spoiler",
        source="ebay",
        listing_url="https://example.test/item",
        part_id="a",
        part_name="Part A",
        positive_similarity=0.9,
        negative_similarity=None,
        similarity_margin=None,
        model_fingerprint="f" * 64,
        best_positive_reference_id=3,
        best_negative_reference_id=None,
    )
    assert preview.text().startswith("TEST VISUAL CANDIDATE")
    assert "probability" not in preview.text().lower()
    listing = LoadedVisionImage(Image.new("RGB", (8, 8), "red"), "a" * 64, 8, 8)
    try:
        content = _contact_sheet(listing, None, None)
        assert content.startswith(b"RIFF") and b"WEBP" in content[:16]
    finally:
        listing.close()
    assert future_visual_event_key(2, "a", "fingerprint", "v1") == (
        "visual-candidate:2:a:fingerprint:v1"
    )


@pytest.mark.asyncio
async def test_explicit_alert_send_does_not_reserve_production_notification(
    session_factory,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        listing = _listing("alert-preview")
        reference_listing = _listing("alert-reference")
        session.add_all([listing, reference_listing])
        await session.flush()
        listing_image = ListingImageRow(
            listing_id=listing.id,
            source_url="https://i.ebayimg.com/red.webp",
            position=0,
            is_current=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        reference_image = ListingImageRow(
            listing_id=reference_listing.id,
            source_url="https://i.ebayimg.com/reference.webp",
            position=0,
            is_current=True,
            first_seen_at=now,
            last_seen_at=now,
        )
        review = ManualReviewRow(
            listing_id=reference_listing.id,
            outcome="confirmed",
            selected_part_id="a",
            notes=None,
            reviewed_at=now,
        )
        session.add_all([listing_image, reference_image, review])
        await session.flush()
        reference = ReferenceImageRow(
            listing_image_id=reference_image.id,
            manual_review_id=review.id,
            part_id="a",
            label="positive",
            local_path="references/a/preview.webp",
            content_sha256="p" * 64,
            mime_type="image/webp",
            width=8,
            height=8,
            notes=None,
            is_active=True,
            created_at=now,
        )
        session.add(reference)
        await session.flush()
        match = VisualMatchRow(
            listing_image_id=listing_image.id,
            part_id="a",
            model_fingerprint="f" * 64,
            best_positive_reference_id=reference.id,
            best_negative_reference_id=None,
            positive_similarity=0.9,
            negative_similarity=None,
            similarity_margin=None,
            positive_reference_count=1,
            negative_reference_count=0,
            rank_for_listing=1,
            status="positive_only",
            computed_at=now,
        )
        session.add(match)
        await session.commit()
        match_id = match.id

    notifier = FakeNotifier()
    service = VisionAlertService(session_factory, FakeLoader(), {"a": "Part A"})  # type: ignore[arg-type]
    await service.send(match_id, notifier)  # type: ignore[arg-type]
    assert notifier.content.startswith(b"RIFF")
    assert notifier.caption.startswith("TEST VISUAL CANDIDATE")
    async with session_factory() as session:
        assert await session.scalar(select(func.count(NotificationRow.id))) == 0


def test_future_visual_alert_guard_requires_all_evidence(
    settings: Settings,
) -> None:
    match = VisualMatchRow(
        listing_image_id=1,
        part_id="a",
        model_fingerprint="f" * 64,
        best_positive_reference_id=2,
        best_negative_reference_id=3,
        positive_similarity=0.9,
        negative_similarity=0.2,
        similarity_margin=0.7,
        positive_reference_count=2,
        negative_reference_count=1,
        rank_for_listing=1,
        status="review_candidate",
        computed_at=datetime.now(UTC),
    )
    assert not future_visual_alert_eligible(
        match, settings, distinct_positive_reference_listings=2, already_alerted=False
    )
    configured = settings.model_copy(
        update={
            "vision_alerts_enabled": True,
            "vision_alert_min_positive": 0.8,
            "vision_alert_min_margin": 0.5,
        }
    )
    assert future_visual_alert_eligible(
        match, configured, distinct_positive_reference_listings=2, already_alerted=False
    )
    assert not future_visual_alert_eligible(
        match, configured, distinct_positive_reference_listings=1, already_alerted=False
    )
    assert not future_visual_alert_eligible(
        match, configured, distinct_positive_reference_listings=2, already_alerted=True
    )


@pytest.mark.real_vision_model
@pytest.mark.skipif(os.getenv("RUN_REAL_VISION_MODEL") != "1", reason="explicit opt-in required")
@pytest.mark.asyncio
async def test_real_dinov2_small_warmup(settings: Settings, tmp_path: Path) -> None:
    configured = _settings(settings, tmp_path)
    embedder = Dinov2Embedder.for_settings(configured)
    try:
        vector = await embedder.warmup()
        assert vector.shape == (embedder.embedding_dimension,)
        assert np.linalg.norm(vector) == pytest.approx(1, abs=1e-5)
        assert embedder.resolved_revision not in {"", "unresolved"}
    finally:
        embedder.release()
