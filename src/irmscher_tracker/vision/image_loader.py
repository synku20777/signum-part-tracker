from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image

from irmscher_tracker.db.models import ReferenceImageRow
from irmscher_tracker.services.review import ReferenceImageError, ReferenceImageStore


@dataclass
class LoadedVisionImage:
    image: Image.Image
    content_sha256: str
    width: int
    height: int

    def close(self) -> None:
        self.image.close()


class VisionImageLoader:
    def __init__(self, store: ReferenceImageStore) -> None:
        self._store = store

    async def listing(self, source: str, source_url: str) -> LoadedVisionImage:
        sanitized = await self._store.download(source, source_url)
        return self._loaded(sanitized.content, sanitized.sha256)

    async def reference(self, row: ReferenceImageRow) -> LoadedVisionImage:
        path = self._store.resolve(row.local_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ReferenceImageError("Reference image file is missing") from exc
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != row.content_sha256:
            raise ReferenceImageError("Reference image hash does not match")
        return self._loaded(content, actual_hash)

    @staticmethod
    def _loaded(content: bytes, content_sha256: str) -> LoadedVisionImage:
        try:
            with Image.open(io.BytesIO(content)) as source:
                source.load()
                image = source.convert("RGB")
        except (OSError, ValueError) as exc:
            raise ReferenceImageError("Image cannot be decoded") from exc
        return LoadedVisionImage(image, content_sha256, image.width, image.height)
