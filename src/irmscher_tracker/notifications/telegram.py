from __future__ import annotations

import logging

import httpx

from irmscher_tracker.domain import AlertPayload, AlertType

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0):
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=timeout)

    async def send_alert(self, payload: AlertPayload) -> None:
        """Send a formatted alert message to Telegram."""
        message = self._format_message(payload)
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"

        response = await self._client.post(
            url,
            json={
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        response.raise_for_status()
        logger.info("Telegram message sent successfully")

    async def send_test_message(self) -> bool:
        """Send a test message to verify configuration."""
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": "\u2705 Irmscher Tracker notification test successful!",
                    "parse_mode": "HTML",
                },
            )
            response.raise_for_status()
            return True
        except Exception:
            logger.exception("Test message failed")
            return False

    async def send_photo(self, content: bytes, caption: str) -> None:
        """Send an explicit in-memory image preview."""
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendPhoto"
        response = await self._client.post(
            url,
            data={"chat_id": self._chat_id, "caption": caption},
            files={"photo": ("visual-preview.webp", content, "image/webp")},
        )
        response.raise_for_status()
        logger.info("Telegram visual preview sent successfully")

    def _format_message(self, payload: AlertPayload) -> str:
        alert_emoji = {
            AlertType.NEW_LISTING: "\U0001f7e2",
            AlertType.PRICE_DECREASE: "\U0001f4b0",
            AlertType.SCORE_THRESHOLD_CROSSED: "\u2b50",
            AlertType.REACTIVATED: "\U0001f504",
        }
        emoji = alert_emoji.get(payload.alert_type, "\U0001f514")

        lines = [
            f"{emoji} <b>{payload.alert_type.value.replace('_', ' ').title()}</b>",
            "",
            f"\U0001f3f7 <b>{payload.listing_title}</b>",
            f"\U0001f9e9 Part: {payload.part_name}",
            f"\U0001f3af Score: {payload.score}",
            "",
        ]

        # Price info
        price_line = (
            f"\U0001f4b6 Price: {payload.price} {payload.currency}"
            if payload.price is not None
            else "\U0001f4b6 Price unavailable"
        )
        if payload.previous_price is not None:
            price_line += f" (was {payload.previous_price} {payload.currency})"
        lines.append(price_line)

        if payload.shipping_cost is not None:
            lines.append(f"\U0001f4e6 Shipping: {payload.shipping_cost} {payload.currency}")

        if payload.seller_location:
            lines.append(f"\U0001f4cd Location: {payload.seller_location}")

        lines.append(f"\U0001f310 Source: {payload.source.value}")
        lines.append("")

        # Score breakdown
        lines.append("<b>Score breakdown:</b>")
        for reason in payload.score_explanation:
            sign = "+" if reason.points > 0 else ""
            lines.append(f"  {sign}{reason.points}: {reason.detail}")

        lines.append("")
        lines.append(f'<a href="{payload.listing_url}">View Listing \u2192</a>')

        return "\n".join(lines)

    async def close(self) -> None:
        await self._client.aclose()
