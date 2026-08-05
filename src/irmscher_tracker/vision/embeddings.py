from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image

FloatVector = NDArray[np.float32]


class EmbeddingIntegrityError(ValueError):
    pass


class ImageEmbedder(Protocol):
    model_id: str
    resolved_revision: str
    model_fingerprint: str
    preprocessing_version: str
    embedding_dimension: int
    load_time_seconds: float
    last_inference_seconds: float

    async def warmup(self) -> FloatVector: ...

    async def embed(self, images: Sequence[Image.Image]) -> FloatVector: ...

    def release(self) -> None: ...


def normalize_rows(values: ArrayLike) -> FloatVector:
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    if vectors.ndim != 2 or not vectors.shape[1] or not np.isfinite(vectors).all():
        raise EmbeddingIntegrityError("Embedding tensor is invalid")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise EmbeddingIntegrityError("Embedding tensor contains a zero vector")
    return np.asarray(vectors / norms, dtype=np.float32)


def serialize_vector(vector: ArrayLike) -> tuple[bytes, int]:
    value = np.asarray(vector, dtype=np.float32)
    if value.ndim != 1:
        raise EmbeddingIntegrityError("Only one embedding vector may be serialized")
    normalized = normalize_rows(value)[0]
    return normalized.tobytes(order="C"), int(normalized.shape[0])


def deserialize_vector(vector_blob: bytes, embedding_dim: int, dtype: str) -> FloatVector:
    if dtype != "float32" or embedding_dim <= 0 or len(vector_blob) != embedding_dim * 4:
        raise EmbeddingIntegrityError("Stored embedding has an invalid byte length")
    vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
    if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-4):
        raise EmbeddingIntegrityError("Stored embedding is not a normalized finite vector")
    return vector
