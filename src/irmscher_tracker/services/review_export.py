from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker import __version__
from irmscher_tracker.db.models import (
    ListingImageRow,
    ListingRow,
    ManualReviewRow,
    ReferenceImageRow,
)
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.services.review import ReviewService
from irmscher_tracker.services.review_integrity import ReviewIntegrityService

_SCHEMA_VERSION = 2


class DatasetExportError(ValueError):
    pass


class ReviewDatasetExporter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        data_directory: Path,
        parts_path: Path,
        matcher: PartMatcher,
        review_service: ReviewService,
        integrity_service: ReviewIntegrityService,
    ) -> None:
        self._session_factory = session_factory
        self._data_directory = data_directory.resolve()
        self._reference_root = (self._data_directory / "references").resolve()
        self._parts_path = parts_path
        self._matcher = matcher
        self._review_service = review_service
        self._integrity_service = integrity_service

    async def export(self, destination: Path, *, allow_integrity_errors: bool) -> Path:
        destination = destination.resolve()
        if not destination.is_relative_to(self._data_directory):
            raise DatasetExportError(
                "Export destination must be inside the tracker data directory"
            )
        if destination.exists():
            raise DatasetExportError("Export destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)

        integrity = await self._integrity_service.check()
        if integrity.status == "error" and not allow_integrity_errors:
            raise DatasetExportError("Review integrity errors block dataset export")

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ReferenceImageRow,
                        ListingImageRow.listing_id,
                        ListingRow.source,
                        ManualReviewRow.reviewed_at,
                        ManualReviewRow.decision_reason,
                    )
                    .join(
                        ListingImageRow,
                        ListingImageRow.id == ReferenceImageRow.listing_image_id,
                    )
                    .join(ListingRow, ListingRow.id == ListingImageRow.listing_id)
                    .join(
                        ManualReviewRow,
                        ManualReviewRow.id == ReferenceImageRow.manual_review_id,
                    )
                    .where(ReferenceImageRow.is_active.is_(True))
                    .order_by(
                        ReferenceImageRow.part_id,
                        ReferenceImageRow.label,
                        ReferenceImageRow.content_sha256,
                        ReferenceImageRow.id,
                    )
                )
            ).all()

        temporary = Path(tempfile.mkdtemp(prefix=".dataset-", dir=destination.parent))
        try:
            records: list[dict[str, Any]] = []
            omitted: list[dict[str, object]] = []
            for reference, listing_id, source, reviewed_at, decision_reason in rows:
                source_path = (self._data_directory / reference.local_path).resolve()
                if (
                    not source_path.is_relative_to(self._reference_root)
                    or not source_path.is_file()
                    or not ReviewIntegrityService.valid_file(source_path, reference)
                ):
                    omitted.append(
                        {"reference_id": reference.id, "reason": "reference-file-invalid"}
                    )
                    continue
                relative = (
                    Path("images")
                    / reference.part_id
                    / reference.label
                    / f"{reference.content_sha256}.webp"
                )
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                records.append(
                    {
                        "reference_id": reference.id,
                        "content_sha256": reference.content_sha256,
                        "part_id": reference.part_id,
                        "label": reference.label,
                        "source": source,
                        "listing_id": listing_id,
                        "width": reference.width,
                        "height": reference.height,
                        "reviewed_at": reviewed_at.isoformat(),
                        "decision_reason": decision_reason,
                        "view": reference.view,
                        "context": reference.context,
                        "quality": reference.quality,
                        "obstruction": reference.obstruction,
                        "image_path": relative.as_posix(),
                    }
                )

            readiness = await self._review_service.dataset_readiness(integrity.status)
            counts = Counter(
                (record["part_id"], record["label"], record["source"]) for record in records
            )
            duplicate_hashes = Counter(record["content_sha256"] for record in records)
            manifest = {
                "application_version": __version__,
                "review_schema_version": _SCHEMA_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "parts_catalogue_sha256": self._sha256(self._parts_path),
                "reference_count": len(records),
                "unique_content_count": len(duplicate_hashes),
                "duplicate_content_count": sum(
                    count - 1 for count in duplicate_hashes.values() if count > 1
                ),
                "counts": [
                    {"part_id": part, "label": label, "source": source, "count": count}
                    for (part, label, source), count in sorted(counts.items())
                ],
                "coverage_warnings": [
                    {"part_id": part.part_id, "missing": part.missing_requirements}
                    for part in readiness.parts
                    if part.missing_requirements
                ],
                "integrity_status": integrity.status,
                "omitted_references": omitted,
                "references": records,
            }
            self._write_json(temporary / "manifest.json", manifest)
            self._write_csv(temporary / "manifest.csv", records)
            self._write_json(
                temporary / "parts.json",
                [
                    part.model_dump(mode="json")
                    for part in sorted(self._matcher.parts, key=lambda part: part.id)
                ],
            )
            self._write_checksums(temporary)
            os.replace(temporary, destination)
            return destination
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
        fields = [
            "reference_id",
            "content_sha256",
            "part_id",
            "label",
            "source",
            "listing_id",
            "width",
            "height",
            "reviewed_at",
            "decision_reason",
            "view",
            "context",
            "quality",
            "obstruction",
            "image_path",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)

    @classmethod
    def _write_checksums(cls, root: Path) -> None:
        files = sorted(
            path for path in root.rglob("*") if path.is_file() and path.name != "checksums.sha256"
        )
        lines = [f"{cls._sha256(path)}  {path.relative_to(root).as_posix()}" for path in files]
        (root / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")

    @staticmethod
    def _sha256(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
