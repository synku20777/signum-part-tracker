from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.db.models import (
    ListingImageRow,
    ListingRow,
    ReferenceImageRow,
    VisualMatchRow,
)
from irmscher_tracker.notifications.telegram import TelegramNotifier
from irmscher_tracker.settings import Settings
from irmscher_tracker.vision.image_loader import LoadedVisionImage, VisionImageLoader


class VisualMatchNotFoundError(ValueError):
    pass


@dataclass(frozen=True)
class VisualAlertPreview:
    match_id: int
    listing_id: int
    listing_title: str
    source: str
    listing_url: str
    part_id: str
    part_name: str
    positive_similarity: float | None
    negative_similarity: float | None
    similarity_margin: float | None
    model_fingerprint: str
    best_positive_reference_id: int | None
    best_negative_reference_id: int | None

    def text(self) -> str:
        return "\n".join(
            [
                "TEST VISUAL CANDIDATE — experimental visual evidence",
                self.listing_title,
                f"Marketplace: {self.source}",
                f"Listing: {self.listing_url}",
                f"Proposed part: {self.part_name} ({self.part_id})",
                f"Positive similarity: {_similarity(self.positive_similarity)}",
                f"Negative similarity: {_similarity(self.negative_similarity)}",
                f"Margin: {_similarity(self.similarity_margin)}",
                f"Model fingerprint: {self.model_fingerprint}",
                f"Closest positive reference: {self.best_positive_reference_id or 'none'}",
                f"Closest negative reference: {self.best_negative_reference_id or 'none'}",
            ]
        )


class VisionAlertService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        loader: VisionImageLoader,
        part_names: dict[str, str],
    ) -> None:
        self._session_factory = session_factory
        self._loader = loader
        self._part_names = part_names

    async def preview(self, match_id: int) -> VisualAlertPreview:
        async with self._session_factory() as session:
            match = await session.get(VisualMatchRow, match_id)
            if match is None:
                raise VisualMatchNotFoundError("Visual match not found")
            listing_image = await session.get(ListingImageRow, match.listing_image_id)
            listing = (
                await session.get(ListingRow, listing_image.listing_id)
                if listing_image is not None
                else None
            )
            if listing_image is None or listing is None:
                raise VisualMatchNotFoundError("Visual match listing is missing")
        return VisualAlertPreview(
            match_id=match.id,
            listing_id=listing.id,
            listing_title=listing.title,
            source=listing.source,
            listing_url=listing.url,
            part_id=match.part_id,
            part_name=self._part_names.get(match.part_id, match.part_id),
            positive_similarity=match.positive_similarity,
            negative_similarity=match.negative_similarity,
            similarity_margin=match.similarity_margin,
            model_fingerprint=match.model_fingerprint,
            best_positive_reference_id=match.best_positive_reference_id,
            best_negative_reference_id=match.best_negative_reference_id,
        )

    async def send(self, match_id: int, notifier: TelegramNotifier) -> VisualAlertPreview:
        preview = await self.preview(match_id)
        async with self._session_factory() as session:
            match = await session.get(VisualMatchRow, match_id)
            assert match is not None
            listing_image = await session.get(ListingImageRow, match.listing_image_id)
            assert listing_image is not None
            listing = await session.get(ListingRow, listing_image.listing_id)
            assert listing is not None
            positive = (
                await session.get(ReferenceImageRow, match.best_positive_reference_id)
                if match.best_positive_reference_id
                else None
            )
            negative = (
                await session.get(ReferenceImageRow, match.best_negative_reference_id)
                if match.best_negative_reference_id
                else None
            )

        listing_loaded = await self._loader.listing(listing.source, listing_image.source_url)
        positive_loaded = await self._loader.reference(positive) if positive else None
        negative_loaded = await self._loader.reference(negative) if negative else None
        try:
            contact_sheet = _contact_sheet(listing_loaded, positive_loaded, negative_loaded)
            await notifier.send_photo(contact_sheet, preview.text())
        finally:
            listing_loaded.close()
            if positive_loaded:
                positive_loaded.close()
            if negative_loaded:
                negative_loaded.close()
        return preview


def future_visual_event_key(
    listing_id: int, part_id: str, model_fingerprint: str, threshold_version: str
) -> str:
    return f"visual-candidate:{listing_id}:{part_id}:{model_fingerprint}:{threshold_version}"


def future_visual_alert_eligible(
    match: VisualMatchRow,
    settings: Settings,
    *,
    distinct_positive_reference_listings: int,
    already_alerted: bool,
) -> bool:
    """Guard a future sender; this MVP deliberately has no automatic caller."""
    return bool(
        settings.vision_alerts_enabled
        and settings.vision_alert_min_positive is not None
        and settings.vision_alert_min_margin is not None
        and match.status == "review_candidate"
        and match.best_positive_reference_id is not None
        and match.best_negative_reference_id is not None
        and match.positive_similarity is not None
        and match.similarity_margin is not None
        and match.positive_similarity >= settings.vision_alert_min_positive
        and match.similarity_margin >= settings.vision_alert_min_margin
        and distinct_positive_reference_listings >= 2
        and not already_alerted
    )


def _contact_sheet(
    listing: LoadedVisionImage,
    positive: LoadedVisionImage | None,
    negative: LoadedVisionImage | None,
) -> bytes:
    panel_width, panel_height, label_height = 420, 320, 36
    canvas = Image.new("RGB", (panel_width * 3, panel_height + label_height), "white")
    draw = ImageDraw.Draw(canvas)
    for index, (label, loaded) in enumerate(
        (("Listing", listing), ("Closest positive", positive), ("Closest negative", negative))
    ):
        x = index * panel_width
        draw.rectangle(
            (x, 0, x + panel_width - 1, panel_height + label_height - 1), outline="gray"
        )
        draw.text((x + 10, 10), label, fill="black")
        if loaded is None:
            draw.text((x + 10, label_height + 20), "No reference available", fill="gray")
            continue
        fitted = ImageOps.contain(loaded.image, (panel_width - 20, panel_height - 20))
        canvas.paste(
            fitted,
            (
                x + (panel_width - fitted.width) // 2,
                label_height + (panel_height - fitted.height) // 2,
            ),
        )
    output = io.BytesIO()
    canvas.save(output, format="WEBP", lossless=True, method=6)
    canvas.close()
    return output.getvalue()


def _similarity(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.4f}"
