from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.db.models import (
    ImageEmbeddingRow,
    ListingImageRow,
    ManualReviewRow,
)
from irmscher_tracker.settings import Settings
from irmscher_tracker.vision.embeddings import FloatVector, deserialize_vector
from irmscher_tracker.vision.similarity import ReferenceVector, SimilarityEvidence, rank_parts


@dataclass(frozen=True)
class EvaluationResult:
    listing_id: int
    outcome: str
    expected_part: str
    top_1_part: str | None
    top_3_parts: list[str]
    positive_similarity: float | None
    negative_similarity: float | None
    similarity_margin: float | None
    reference_coverage_sufficient: bool
    reciprocal_rank: float


class VisionEvaluator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        part_ids: list[str],
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._part_ids = sorted(part_ids)

    async def evaluate(
        self, model_fingerprint: str, references: list[ReferenceVector]
    ) -> tuple[dict[str, object], Path, Path]:
        latest_reviews = await self._latest_reviews()
        results: list[EvaluationResult] = []
        excluded = Counter[str]()
        for review in latest_reviews.values():
            if review.outcome == "uncertain":
                excluded["uncertain"] += 1
                continue
            if not review.selected_part_id:
                excluded["missing_target_part"] += 1
                continue
            embeddings = await self._listing_embeddings(review.listing_id, model_fingerprint)
            if not embeddings:
                excluded["missing_listing_embedding"] += 1
                continue
            rankings = [
                rank_parts(
                    vector,
                    references,
                    self._part_ids,
                    query_listing_id=review.listing_id,
                    query_content_sha256=content_hash,
                )
                for content_hash, vector in embeddings
            ]
            combined = _best_parts(rankings)
            expected_rank = next(
                (
                    index
                    for index, item in enumerate(combined, start=1)
                    if item.part_id == review.selected_part_id
                ),
                None,
            )
            expected = next(
                (item for item in combined if item.part_id == review.selected_part_id), None
            )
            positive_listings = {
                reference.listing_id
                for reference in references
                if reference.part_id == review.selected_part_id
                and reference.label == "positive"
                and reference.listing_id != review.listing_id
            }
            results.append(
                EvaluationResult(
                    listing_id=review.listing_id,
                    outcome=review.outcome,
                    expected_part=review.selected_part_id,
                    top_1_part=combined[0].part_id if combined else None,
                    top_3_parts=[item.part_id for item in combined[:3]],
                    positive_similarity=expected.positive_similarity if expected else None,
                    negative_similarity=expected.negative_similarity if expected else None,
                    similarity_margin=expected.similarity_margin if expected else None,
                    reference_coverage_sufficient=len(positive_listings) >= 2,
                    reciprocal_rank=1 / expected_rank if expected_rank else 0.0,
                )
            )

        positive_reference_listings = {
            part_id: len(
                {
                    reference.listing_id
                    for reference in references
                    if reference.part_id == part_id and reference.label == "positive"
                }
            )
            for part_id in self._part_ids
        }
        report = _report(
            model_fingerprint,
            results,
            excluded,
            self._part_ids,
            positive_reference_listings,
        )
        directory = self._settings.vision_directory / "evaluations"
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        json_path = directory / f"{timestamp}.json"
        csv_path = directory / f"{timestamp}.csv"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=list(asdict(results[0]).keys()) if results else ["listing_id"]
            )
            writer.writeheader()
            for result in results:
                row = asdict(result)
                row["top_3_parts"] = "|".join(result.top_3_parts)
                writer.writerow(row)
        return report, json_path, csv_path

    async def _latest_reviews(self) -> dict[int, ManualReviewRow]:
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ManualReviewRow).order_by(
                            ManualReviewRow.listing_id,
                            ManualReviewRow.reviewed_at.desc(),
                            ManualReviewRow.id.desc(),
                        )
                    )
                ).scalars()
            )
        latest: dict[int, ManualReviewRow] = {}
        for row in rows:
            latest.setdefault(row.listing_id, row)
        return latest

    async def _listing_embeddings(
        self, listing_id: int, model_fingerprint: str
    ) -> list[tuple[str, FloatVector]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ImageEmbeddingRow)
                    .join(
                        ListingImageRow,
                        ListingImageRow.id == ImageEmbeddingRow.listing_image_id,
                    )
                    .where(
                        ListingImageRow.listing_id == listing_id,
                        ListingImageRow.is_current.is_(True),
                        ImageEmbeddingRow.model_fingerprint == model_fingerprint,
                    )
                    .order_by(ListingImageRow.position, ListingImageRow.id)
                )
            ).scalars()
            return [
                (
                    row.content_sha256,
                    deserialize_vector(row.vector_blob, row.embedding_dim, row.dtype),
                )
                for row in rows
            ]


def _best_parts(rankings: list[list[SimilarityEvidence]]) -> list[SimilarityEvidence]:
    by_part: dict[str, SimilarityEvidence] = {}
    for ranking in rankings:
        for item in ranking:
            current = by_part.get(item.part_id)
            if current is None or _score(item) > _score(current):
                by_part[item.part_id] = item
    return sorted(
        by_part.values(), key=lambda item: (-_score(item)[0], -_score(item)[1], item.part_id)
    )


def _score(item: SimilarityEvidence) -> tuple[float, float]:
    primary = item.similarity_margin if item.similarity_margin is not None else -2.0
    positive = item.positive_similarity if item.positive_similarity is not None else -2.0
    return primary, positive


def _report(
    model_fingerprint: str,
    results: list[EvaluationResult],
    excluded: Counter[str],
    part_ids: list[str],
    positive_reference_listings: dict[str, int],
) -> dict[str, object]:
    confirmed = [result for result in results if result.outcome == "confirmed"]
    sufficient = [result for result in confirmed if result.reference_coverage_sufficient]
    per_part_queries = Counter(result.expected_part for result in confirmed)
    per_part_top1 = Counter(
        result.expected_part for result in confirmed if result.top_1_part == result.expected_part
    )
    sufficient_per_part = Counter(result.expected_part for result in sufficient)
    sufficient_top1 = Counter(
        result.expected_part for result in sufficient if result.top_1_part == result.expected_part
    )
    correct = [result for result in confirmed if result.top_1_part == result.expected_part]
    incorrect = [result for result in confirmed if result.top_1_part != result.expected_part]
    return {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model_fingerprint": model_fingerprint,
        "total_evaluated_listings": len(results),
        "excluded_listings": dict(sorted(excluded.items())),
        "top_1_accuracy": _ratio(
            sum(r.top_1_part == r.expected_part for r in sufficient), len(sufficient)
        ),
        "top_3_recall": _ratio(
            sum(r.expected_part in r.top_3_parts for r in sufficient), len(sufficient)
        ),
        "mean_reciprocal_rank": _mean([r.reciprocal_rank for r in sufficient]),
        "per_part": {
            part_id: {
                "query_count": per_part_queries[part_id],
                "top_1_count": per_part_top1[part_id],
                "top_1_accuracy": (
                    _ratio(sufficient_top1[part_id], sufficient_per_part[part_id])
                    if sufficient_per_part[part_id]
                    else None
                ),
            }
            for part_id in part_ids
        },
        "score_distributions": {
            "correct": _scores(correct),
            "incorrect": _scores(incorrect),
        },
        "targeted_rejected_false_positives": [
            asdict(result)
            for result in results
            if result.outcome == "rejected" and result.top_1_part == result.expected_part
        ],
        "parts_with_insufficient_distinct_reference_listings": sorted(
            {
                result.expected_part
                for result in confirmed
                if not result.reference_coverage_sufficient
            }
            | {part_id for part_id, count in positive_reference_listings.items() if count < 2}
        ),
        "results": [asdict(result) for result in results],
    }


def _scores(results: list[EvaluationResult]) -> dict[str, list[float]]:
    return {
        "positive_similarity": [
            value.positive_similarity for value in results if value.positive_similarity is not None
        ],
        "similarity_margin": [
            value.similarity_margin for value in results if value.similarity_margin is not None
        ],
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
