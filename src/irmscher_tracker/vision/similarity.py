from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import numpy as np

from irmscher_tracker.vision.embeddings import FloatVector

EvidenceStatus = Literal["ranked", "review_candidate", "positive_only", "insufficient_references"]


@dataclass(frozen=True)
class ReferenceVector:
    reference_id: int
    listing_id: int
    part_id: str
    label: Literal["positive", "negative"]
    content_sha256: str
    vector: FloatVector


@dataclass(frozen=True)
class SimilarityEvidence:
    part_id: str
    best_positive_reference_id: int | None
    best_negative_reference_id: int | None
    positive_similarity: float | None
    negative_similarity: float | None
    similarity_margin: float | None
    positive_reference_count: int
    negative_reference_count: int
    rank: int
    status: EvidenceStatus


def rank_parts(
    query_vector: FloatVector,
    references: list[ReferenceVector],
    part_ids: list[str],
    *,
    query_listing_id: int,
    query_content_sha256: str,
    min_positive: float | None = None,
    min_margin: float | None = None,
) -> list[SimilarityEvidence]:
    query = np.asarray(query_vector, dtype=np.float32)
    evidence: list[SimilarityEvidence] = []
    for part_id in sorted(part_ids):
        eligible = [
            reference
            for reference in references
            if reference.part_id == part_id
            and reference.listing_id != query_listing_id
            and reference.content_sha256 != query_content_sha256
        ]
        positives = [reference for reference in eligible if reference.label == "positive"]
        negatives = [reference for reference in eligible if reference.label == "negative"]
        positive_id, positive = _best(query, positives)
        negative_id, negative = _best(query, negatives)
        margin = positive - negative if positive is not None and negative is not None else None
        if positive is None:
            status: EvidenceStatus = "insufficient_references"
        elif negative is None:
            status = "positive_only"
        elif (
            min_positive is not None
            and min_margin is not None
            and positive >= min_positive
            and margin is not None
            and margin >= min_margin
        ):
            status = "review_candidate"
        else:
            status = "ranked"
        evidence.append(
            SimilarityEvidence(
                part_id=part_id,
                best_positive_reference_id=positive_id,
                best_negative_reference_id=negative_id,
                positive_similarity=positive,
                negative_similarity=negative,
                similarity_margin=margin,
                positive_reference_count=len(positives),
                negative_reference_count=len(negatives),
                rank=0,
                status=status,
            )
        )

    return rank_evidence(evidence)


def rank_evidence(evidence: list[SimilarityEvidence]) -> list[SimilarityEvidence]:
    return [
        replace(value, rank=index)
        for index, value in enumerate(sorted(evidence, key=_rank_key), start=1)
    ]


def _best(
    query: FloatVector, references: list[ReferenceVector]
) -> tuple[int | None, float | None]:
    if not references:
        return None, None
    matrix = np.stack([reference.vector for reference in references]).astype(np.float32)
    scores = query @ matrix.T
    best_score = float(np.max(scores))
    best = min(
        reference.reference_id
        for reference, score in zip(references, scores, strict=True)
        if np.isclose(float(score), best_score)
    )
    return best, best_score


def _rank_key(value: SimilarityEvidence) -> tuple[float | str, ...]:
    if value.similarity_margin is not None:
        tier, primary = 2.0, value.similarity_margin
    elif value.positive_similarity is not None:
        tier, primary = 1.0, value.positive_similarity
    else:
        tier, primary = 0.0, -2.0
    positive = value.positive_similarity if value.positive_similarity is not None else -2.0
    return (-tier, -primary, -positive, value.part_id)
