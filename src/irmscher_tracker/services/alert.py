from __future__ import annotations

import logging

from irmscher_tracker.domain import AlertPayload
from irmscher_tracker.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, notifier: TelegramNotifier | None = None):
        self._notifier = notifier

    async def send(self, payload: AlertPayload) -> bool:
        """Send an alert notification. Returns True if successful."""
        if self._notifier is None:
            logger.warning("No notifier configured, skipping alert for %s", payload.listing_title)
            return False

        try:
            await self._notifier.send_alert(payload)
            logger.info("Alert sent for: %s", payload.listing_title)
            return True
        except Exception:
            logger.exception("Failed to send alert for: %s", payload.listing_title)
            return False
