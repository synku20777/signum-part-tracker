from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.db.models import (
    ImageEmbeddingRow,
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    ReferenceImageRow,
    VisionRunRow,
    VisualMatchRow,
)
from irmscher_tracker.services.review import ReferenceImageError, ReferenceImageStore
from irmscher_tracker.services.review_integrity import ReviewIntegrityService
from irmscher_tracker.settings import Settings
from irmscher_tracker.vision.embeddings import (
    EmbeddingIntegrityError,
    FloatVector,
    ImageEmbedder,
    deserialize_vector,
    serialize_vector,
)
from irmscher_tracker.vision.evaluation import VisionEvaluator
from irmscher_tracker.vision.image_loader import LoadedVisionImage, VisionImageLoader
from irmscher_tracker.vision.similarity import (
    ReferenceVector,
    SimilarityEvidence,
    rank_evidence,
    rank_parts,
)

VisionRunType = Literal["warmup", "reference_rebuild", "listing_scan", "evaluation"]
VisionRunStatus = Literal["running", "completed", "partial", "failed", "interrupted"]
_MAX_ERRORS = 100


class VisionDisabledError(ValueError):
    pass


class VisionRunBusyError(RuntimeError):
    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__("Vision run already active")


class VisionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        part_ids: list[str],
        store: ReferenceImageStore,
        embedder: ImageEmbedder,
        integrity: ReviewIntegrityService,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings
        self.part_ids = sorted(part_ids)
        self.store = store
        self.loader = VisionImageLoader(store)
        self.embedder = embedder
        self.integrity = integrity

    async def recover_stale_runs(self) -> int:
        async with self._session_factory() as session:
            stale_ids = list(
                (
                    await session.execute(
                        select(VisionRunRow.id).where(VisionRunRow.status == "running")
                    )
                ).scalars()
            )
            await session.execute(
                update(VisionRunRow)
                .where(VisionRunRow.status == "running")
                .values(status="interrupted", finished_at=datetime.now(UTC))
            )
            await session.commit()
            return len(stale_ids)

    async def reserve(self, run_type: VisionRunType, requested_count: int = 0) -> int:
        if not self.settings.vision_enabled:
            raise VisionDisabledError("Vision is disabled")
        row = VisionRunRow(
            run_type=run_type,
            status="running",
            model_fingerprint=None,
            requested_count=requested_count,
            processed_count=0,
            skipped_count=0,
            failed_count=0,
            errors_json="[]",
            started_at=datetime.now(UTC),
            finished_at=None,
        )
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                active_id = await session.scalar(
                    select(VisionRunRow.id).where(VisionRunRow.status == "running")
                )
                raise VisionRunBusyError(active_id or 0) from exc
            await session.refresh(row)
        return row.id

    async def status(self) -> dict[str, object]:
        async with self._session_factory() as session:
            active = await session.scalar(
                select(VisionRunRow)
                .where(VisionRunRow.status == "running")
                .order_by(VisionRunRow.id.desc())
                .limit(1)
            )
            latest = await session.scalar(
                select(VisionRunRow).order_by(VisionRunRow.id.desc()).limit(1)
            )
        cache = self.settings.vision_model_cache_directory
        model_cached = cache.is_dir() and any(
            path.name in {"model.safetensors", "pytorch_model.bin"} for path in cache.rglob("*")
        )
        if not self.settings.vision_enabled:
            state = "disabled"
        elif active is not None:
            state = "run_active"
        elif latest is not None and latest.status == "failed":
            state = "failed"
        elif model_cached or self.embedder.model_fingerprint:
            state = "ready"
        else:
            state = "model_not_cached"
        return {
            "state": state,
            "enabled": self.settings.vision_enabled,
            "model_cached": model_cached,
            "model_loaded": bool(self.embedder.model_fingerprint),
            "model_id": self.settings.vision_model_id,
            "model_revision": self.embedder.resolved_revision or None,
            "model_fingerprint": self.embedder.model_fingerprint or None,
            "active_run_id": active.id if active else None,
            "automatic_analysis_enabled": self.settings.vision_auto_analyze,
            "automatic_alerts_enabled": self.settings.vision_alerts_enabled,
        }

    async def runs(self, limit: int = 50) -> list[VisionRunRow]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(VisionRunRow).order_by(VisionRunRow.id.desc()).limit(limit)
                    )
                ).scalars()
            )

    async def run(self, run_id: int) -> VisionRunRow | None:
        async with self._session_factory() as session:
            return await session.get(VisionRunRow, run_id)

    async def warmup(self, run_id: int) -> None:
        try:
            await self.embedder.warmup()
            await self._finish(run_id, "completed", processed=1)
        except asyncio.CancelledError:
            await self._finish(run_id, "interrupted", errors=["cancelled"])
            raise
        except Exception:
            await self._finish(run_id, "failed", failed=1, errors=["model_load_failed"])

    async def rebuild_references(self, run_id: int, *, force: bool = False) -> None:
        errors: list[str] = []
        processed = skipped = failed = 0
        try:
            integrity = await self.integrity.check()
            if integrity.status == "error":
                await self._finish(run_id, "failed", failed=1, errors=["review_integrity_error"])
                return
            await self.embedder.warmup()
            async with self._session_factory() as session:
                references = list(
                    (
                        await session.execute(
                            select(ReferenceImageRow)
                            .where(ReferenceImageRow.is_active.is_(True))
                            .order_by(ReferenceImageRow.id)
                        )
                    ).scalars()
                )
                await session.execute(
                    update(VisionRunRow)
                    .where(VisionRunRow.id == run_id)
                    .values(
                        requested_count=len(references),
                        model_fingerprint=self.embedder.model_fingerprint,
                    )
                )
                await session.commit()

            for start in range(0, len(references), self.settings.vision_batch_size):
                batch = references[start : start + self.settings.vision_batch_size]
                loaded: list[tuple[ReferenceImageRow, LoadedVisionImage]] = []
                for reference in batch:
                    if not force and await self._existing_reference_embedding(reference):
                        skipped += 1
                        continue
                    try:
                        image = await self.loader.reference(reference)
                    except ReferenceImageError as exc:
                        failed += 1
                        errors.append(_error_code(exc))
                        continue
                    loaded.append((reference, image))
                if not loaded:
                    continue
                try:
                    vectors = await self.embedder.embed([item.image for _, item in loaded])
                    async with self._session_factory() as session:
                        for (reference, image), vector in zip(loaded, vectors, strict=True):
                            if force:
                                await session.execute(
                                    delete(ImageEmbeddingRow).where(
                                        ImageEmbeddingRow.reference_image_id == reference.id,
                                        ImageEmbeddingRow.model_fingerprint
                                        == self.embedder.model_fingerprint,
                                    )
                                )
                            session.add(
                                self._embedding_row("reference", reference.id, image, vector)
                            )
                            processed += 1
                        await session.commit()
                except Exception:
                    failed += len(loaded)
                    errors.extend(["embedding_failed"] * len(loaded))
                finally:
                    for _, image in loaded:
                        image.close()
            status: VisionRunStatus = "partial" if failed else "completed"
            await self._finish(
                run_id, status, processed=processed, skipped=skipped, failed=failed, errors=errors
            )
        except asyncio.CancelledError:
            await self._finish(
                run_id,
                "interrupted",
                processed=processed,
                skipped=skipped,
                failed=failed,
                errors=[*errors, "cancelled"],
            )
            raise
        except Exception:
            await self._finish(
                run_id,
                "failed",
                processed=processed,
                skipped=skipped,
                failed=failed + 1,
                errors=[*errors, "reference_rebuild_failed"],
            )

    async def scan(
        self,
        run_id: int,
        *,
        limit: int | None = None,
        source: str | None = None,
        listing_id: int | None = None,
        force: bool = False,
    ) -> None:
        errors: list[str] = []
        processed = skipped = failed = 0
        try:
            await self.embedder.warmup()
            references = await self.reference_vectors()
            listings = await self._candidate_listings(
                limit=limit or self.settings.vision_max_listings_per_run,
                source=source,
                listing_id=listing_id,
            )
            async with self._session_factory() as session:
                await session.execute(
                    update(VisionRunRow)
                    .where(VisionRunRow.id == run_id)
                    .values(
                        requested_count=len(listings),
                        model_fingerprint=self.embedder.model_fingerprint,
                    )
                )
                await session.commit()
            for listing in listings:
                try:
                    vectors, reused = await self._listing_vectors(listing, force=force)
                    skipped += reused
                    if not vectors:
                        failed += 1
                        errors.append("listing_images_failed")
                        continue
                    await self._persist_listing_matches(listing.id, vectors, references)
                    processed += 1
                except (EmbeddingIntegrityError, ReferenceImageError):
                    failed += 1
                    errors.append("listing_processing_failed")
                except Exception:
                    failed += 1
                    errors.append("listing_processing_failed")
            if not listings:
                skipped = 0
            status: VisionRunStatus = "partial" if failed else "completed"
            await self._finish(
                run_id, status, processed=processed, skipped=skipped, failed=failed, errors=errors
            )
        except asyncio.CancelledError:
            await self._finish(
                run_id,
                "interrupted",
                processed=processed,
                skipped=skipped,
                failed=failed,
                errors=[*errors, "cancelled"],
            )
            raise
        except Exception:
            await self._finish(
                run_id,
                "failed",
                processed=processed,
                skipped=skipped,
                failed=failed + 1,
                errors=[*errors, "listing_scan_failed"],
            )

    async def evaluate(self, run_id: int) -> tuple[dict[str, object], Path, Path] | None:
        try:
            await self.embedder.warmup()
            references = await self.reference_vectors()
            report, json_path, csv_path = await VisionEvaluator(
                self._session_factory, self.settings, self.part_ids
            ).evaluate(self.embedder.model_fingerprint, references)
            evaluated = report.get("total_evaluated_listings")
            processed = evaluated if isinstance(evaluated, int) else 0
            await self._finish(run_id, "completed", processed=processed)
            return report, json_path, csv_path
        except asyncio.CancelledError:
            await self._finish(run_id, "interrupted", errors=["cancelled"])
            raise
        except Exception:
            await self._finish(run_id, "failed", failed=1, errors=["evaluation_failed"])
            return None

    async def _candidate_listings(
        self, *, limit: int, source: str | None, listing_id: int | None
    ) -> list[ListingRow]:
        async with self._session_factory() as session:
            stmt = (
                select(ListingRow)
                .join(ListingImageRow, ListingImageRow.listing_id == ListingRow.id)
                .where(ListingRow.is_active.is_(True), ListingImageRow.is_current.is_(True))
                .distinct()
                .order_by(ListingRow.last_seen_at.desc(), ListingRow.id.desc())
            )
            if source is not None:
                stmt = stmt.where(ListingRow.source == source)
            if listing_id is not None:
                stmt = stmt.where(ListingRow.id == listing_id)
            rows = list((await session.execute(stmt)).scalars())
            selected: list[ListingRow] = []
            for row in rows:
                latest = await session.scalar(
                    select(ManualReviewRow)
                    .where(ManualReviewRow.listing_id == row.id)
                    .order_by(ManualReviewRow.reviewed_at.desc(), ManualReviewRow.id.desc())
                    .limit(1)
                )
                if listing_id is not None or latest is None or latest.outcome == "uncertain":
                    selected.append(row)
                if len(selected) >= limit:
                    break
            return selected

    async def _listing_vectors(
        self, listing: ListingRow, *, force: bool
    ) -> tuple[list[tuple[ListingImageRow, LoadedVisionImage, FloatVector]], int]:
        async with self._session_factory() as session:
            image_rows = list(
                (
                    await session.execute(
                        select(ListingImageRow)
                        .where(
                            ListingImageRow.listing_id == listing.id,
                            ListingImageRow.is_current.is_(True),
                        )
                        .order_by(ListingImageRow.position, ListingImageRow.id)
                        .limit(self.settings.vision_max_images_per_listing)
                    )
                ).scalars()
            )
        loaded: list[tuple[ListingImageRow, LoadedVisionImage]] = []
        for row in image_rows:
            try:
                loaded.append((row, await self.loader.listing(listing.source, row.source_url)))
            except ReferenceImageError:
                continue
        if not loaded:
            return [], 0
        results: list[tuple[ListingImageRow, LoadedVisionImage, FloatVector]] = []
        pending: list[tuple[ListingImageRow, LoadedVisionImage]] = []
        reused = 0
        try:
            async with self._session_factory() as session:
                for row, image in loaded:
                    existing = (
                        None
                        if force
                        else await session.scalar(
                            select(ImageEmbeddingRow).where(
                                ImageEmbeddingRow.listing_image_id == row.id,
                                ImageEmbeddingRow.content_sha256 == image.content_sha256,
                                ImageEmbeddingRow.model_fingerprint
                                == self.embedder.model_fingerprint,
                            )
                        )
                    )
                    if existing is None:
                        pending.append((row, image))
                    else:
                        reused += 1
                        results.append(
                            (
                                row,
                                image,
                                deserialize_vector(
                                    existing.vector_blob, existing.embedding_dim, existing.dtype
                                ),
                            )
                        )
            for start in range(0, len(pending), self.settings.vision_batch_size):
                batch = pending[start : start + self.settings.vision_batch_size]
                vectors = await self.embedder.embed([image.image for _, image in batch])
                async with self._session_factory() as session:
                    for (row, image), vector in zip(batch, vectors, strict=True):
                        if force:
                            await session.execute(
                                delete(ImageEmbeddingRow).where(
                                    ImageEmbeddingRow.listing_image_id == row.id,
                                    ImageEmbeddingRow.model_fingerprint
                                    == self.embedder.model_fingerprint,
                                )
                            )
                        session.add(self._embedding_row("listing", row.id, image, vector))
                        results.append((row, image, vector))
                    await session.commit()
            return results, reused
        except Exception:
            for _, image in loaded:
                image.close()
            raise

    async def _persist_listing_matches(
        self,
        listing_id: int,
        vectors: list[tuple[ListingImageRow, LoadedVisionImage, FloatVector]],
        references: list[ReferenceVector],
    ) -> None:
        strongest: dict[str, tuple[ListingImageRow, SimilarityEvidence]] = {}
        try:
            for image_row, image, vector in vectors:
                evidence = rank_parts(
                    vector,
                    references,
                    self.part_ids,
                    query_listing_id=listing_id,
                    query_content_sha256=image.content_sha256,
                    min_positive=self.settings.vision_review_min_positive,
                    min_margin=self.settings.vision_review_min_margin,
                )
                for value in evidence:
                    current = strongest.get(value.part_id)
                    if current is None or _evidence_key(value, image_row.id) < _evidence_key(
                        current[1], current[0].id
                    ):
                        strongest[value.part_id] = (image_row, value)
            ranked = rank_evidence([value for _, value in strongest.values()])
            rank_by_part = {value.part_id: value.rank for value in ranked}
            async with self._session_factory() as session:
                listing_image_ids = select(ListingImageRow.id).where(
                    ListingImageRow.listing_id == listing_id
                )
                await session.execute(
                    delete(VisualMatchRow).where(
                        VisualMatchRow.listing_image_id.in_(listing_image_ids),
                        VisualMatchRow.model_fingerprint == self.embedder.model_fingerprint,
                    )
                )
                now = datetime.now(UTC)
                for part_id, (image_row, value) in strongest.items():
                    session.add(
                        VisualMatchRow(
                            listing_image_id=image_row.id,
                            part_id=part_id,
                            model_fingerprint=self.embedder.model_fingerprint,
                            best_positive_reference_id=value.best_positive_reference_id,
                            best_negative_reference_id=value.best_negative_reference_id,
                            positive_similarity=value.positive_similarity,
                            negative_similarity=value.negative_similarity,
                            similarity_margin=value.similarity_margin,
                            positive_reference_count=value.positive_reference_count,
                            negative_reference_count=value.negative_reference_count,
                            rank_for_listing=rank_by_part[part_id],
                            status=value.status,
                            computed_at=now,
                        )
                    )
                await session.commit()
        finally:
            for _, image, _ in vectors:
                image.close()

    async def _existing_reference_embedding(self, reference: ReferenceImageRow) -> bool:
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(ImageEmbeddingRow.id).where(
                        ImageEmbeddingRow.reference_image_id == reference.id,
                        ImageEmbeddingRow.content_sha256 == reference.content_sha256,
                        ImageEmbeddingRow.model_fingerprint == self.embedder.model_fingerprint,
                    )
                )
                is not None
            )

    async def reference_vectors(self) -> list[ReferenceVector]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ReferenceImageRow, ListingImageRow.listing_id, ImageEmbeddingRow)
                    .join(
                        ListingImageRow,
                        ListingImageRow.id == ReferenceImageRow.listing_image_id,
                    )
                    .join(
                        ImageEmbeddingRow,
                        ImageEmbeddingRow.reference_image_id == ReferenceImageRow.id,
                    )
                    .where(
                        ReferenceImageRow.is_active.is_(True),
                        ImageEmbeddingRow.model_fingerprint == self.embedder.model_fingerprint,
                    )
                )
            ).all()
        return [
            ReferenceVector(
                reference_id=reference.id,
                listing_id=listing_id,
                part_id=reference.part_id,
                label=reference.label,
                content_sha256=reference.content_sha256,
                vector=deserialize_vector(
                    embedding.vector_blob, embedding.embedding_dim, embedding.dtype
                ),
            )
            for reference, listing_id, embedding in rows
        ]

    def _embedding_row(
        self,
        owner_type: Literal["listing", "reference"],
        owner_id: int,
        image: LoadedVisionImage,
        vector: FloatVector,
    ) -> ImageEmbeddingRow:
        vector_blob, dimension = serialize_vector(vector)
        return ImageEmbeddingRow(
            listing_image_id=owner_id if owner_type == "listing" else None,
            reference_image_id=owner_id if owner_type == "reference" else None,
            owner_type=owner_type,
            content_sha256=image.content_sha256,
            model_id=self.embedder.model_id,
            model_revision=self.embedder.resolved_revision,
            model_fingerprint=self.embedder.model_fingerprint,
            preprocessing_version=self.embedder.preprocessing_version,
            embedding_dim=dimension,
            dtype="float32",
            vector_blob=vector_blob,
            created_at=datetime.now(UTC),
        )

    async def _finish(
        self,
        run_id: int,
        status: VisionRunStatus,
        *,
        processed: int = 0,
        skipped: int = 0,
        failed: int = 0,
        errors: list[str] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(VisionRunRow)
                .where(VisionRunRow.id == run_id)
                .values(
                    status=status,
                    model_fingerprint=self.embedder.model_fingerprint or None,
                    processed_count=processed,
                    skipped_count=skipped,
                    failed_count=failed,
                    errors_json=json.dumps((errors or [])[:_MAX_ERRORS]),
                    finished_at=datetime.now(UTC),
                )
            )
            await session.commit()


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "missing" in message:
        return "reference_file_missing"
    if "hash" in message:
        return "reference_hash_mismatch"
    return "reference_decode_failed"


def _evidence_key(value: SimilarityEvidence, image_id: int) -> tuple[float, float, float, int]:
    tier = (
        2.0 if value.similarity_margin is not None else 1.0 if value.positive_similarity else 0.0
    )
    return (
        -tier,
        -(value.similarity_margin if value.similarity_margin is not None else -2.0),
        -(value.positive_similarity if value.positive_similarity is not None else -2.0),
        image_id,
    )
