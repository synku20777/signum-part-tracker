from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from PIL import Image

from irmscher_tracker.settings import Settings
from irmscher_tracker.vision.embeddings import FloatVector, normalize_rows

PREPROCESSING_VERSION = "dinov2-auto-image-processor-v1"
NORMALIZATION = "l2-float32"


class Dinov2Embedder:
    _instances: ClassVar[dict[tuple[str, str, str, str], Dinov2Embedder]] = {}

    def __init__(
        self,
        model_id: str,
        requested_revision: str,
        device: str,
        cache_directory: Path,
    ) -> None:
        if device != "cpu":
            raise ValueError("The vision MVP supports CPU inference only")
        self.model_id = model_id
        self.requested_revision = requested_revision
        self.device = device
        self.cache_directory = cache_directory
        self.preprocessing_version = PREPROCESSING_VERSION
        self.resolved_revision = ""
        self.model_fingerprint = ""
        self.embedding_dimension = 0
        self.pooling_method = ""
        self.load_time_seconds = 0.0
        self.last_inference_seconds = 0.0
        self._processor: Any = None
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    @classmethod
    def for_settings(cls, settings: Settings) -> Dinov2Embedder:
        key = (
            settings.vision_model_id,
            settings.vision_model_revision,
            settings.vision_device,
            str(settings.vision_model_cache_directory.resolve()),
        )
        if key not in cls._instances:
            cls._instances[key] = cls(
                settings.vision_model_id,
                settings.vision_model_revision,
                settings.vision_device,
                settings.vision_model_cache_directory,
            )
        return cls._instances[key]

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def warmup(self) -> FloatVector:
        image = Image.new("RGB", (224, 224), color=(127, 127, 127))
        try:
            return np.asarray((await self.embed([image]))[0], dtype=np.float32)
        finally:
            image.close()

    async def embed(self, images: Sequence[Image.Image]) -> FloatVector:
        if not images:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)
        await self._ensure_loaded()
        result: FloatVector = await asyncio.to_thread(self._embed_sync, images)
        return result

    async def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is None:
                await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        from transformers import AutoImageProcessor, AutoModel

        self.cache_directory.mkdir(parents=True, exist_ok=True)
        revision = self.requested_revision or None
        started = time.perf_counter()
        self._processor = AutoImageProcessor.from_pretrained(  # type: ignore[no-untyped-call]
            self.model_id,
            revision=revision,
            cache_dir=str(self.cache_directory),
        )
        self._model = AutoModel.from_pretrained(
            self.model_id,
            revision=revision,
            cache_dir=str(self.cache_directory),
            use_safetensors=True,
        )
        self._model.to("cpu")
        self._model.eval()
        self.resolved_revision = str(
            getattr(self._model.config, "_commit_hash", None)
            or self.requested_revision
            or "unresolved"
        )
        self.load_time_seconds = time.perf_counter() - started

    def _embed_sync(self, images: Sequence[Image.Image]) -> FloatVector:
        import torch

        started = time.perf_counter()
        inputs = self._processor(images=list(images), return_tensors="pt")
        with torch.inference_mode():
            outputs = self._model(**inputs)
            pooled = getattr(outputs, "pooler_output", None)
            if pooled is not None:
                tensor = pooled
                pooling = "pooler_output"
            else:
                tensor = outputs.last_hidden_state[:, 0, :]
                pooling = "last_hidden_state_cls"
            vectors = tensor.detach().to(device="cpu", dtype=torch.float32).numpy()
        normalized = normalize_rows(vectors)
        self.last_inference_seconds = time.perf_counter() - started
        if not self.embedding_dimension:
            self.embedding_dimension = int(normalized.shape[1])
            self.pooling_method = pooling
            payload = {
                "model_id": self.model_id,
                "model_revision": self.resolved_revision,
                "preprocessing_version": self.preprocessing_version,
                "pooling_method": self.pooling_method,
                "embedding_dimension": self.embedding_dimension,
                "normalization": NORMALIZATION,
            }
            self.model_fingerprint = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return normalized

    def release(self) -> None:
        self._processor = None
        self._model = None
        gc.collect()
