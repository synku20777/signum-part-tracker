from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx


class EbayEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


@dataclass(frozen=True)
class EbayEndpoints:
    oauth_url: str
    browse_search_url: str
    notification_public_key_base_url: str


EBAY_ENDPOINTS = {
    EbayEnvironment.PRODUCTION: EbayEndpoints(
        oauth_url="https://api.ebay.com/identity/v1/oauth2/token",
        browse_search_url="https://api.ebay.com/buy/browse/v1/item_summary/search",
        notification_public_key_base_url=(
            "https://api.ebay.com/commerce/notification/v1/public_key/"
        ),
    ),
    EbayEnvironment.SANDBOX: EbayEndpoints(
        oauth_url="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
        browse_search_url=("https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"),
        notification_public_key_base_url=(
            "https://api.sandbox.ebay.com/commerce/notification/v1/public_key/"
        ),
    ),
}
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayAuthError(Exception):
    """Raised when eBay application authentication fails."""


class EbayApplicationTokenProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: EbayEnvironment,
        timeout: float,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._endpoints = EBAY_ENDPOINTS[environment]
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token
        async with self._lock:
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            credentials = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode()
            ).decode()
            try:
                response = await self._client.post(
                    self._endpoints.oauth_url,
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "client_credentials", "scope": EBAY_SCOPE},
                )
                response.raise_for_status()
                payload = response.json()
                token = payload["access_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError
                expires_in = max(int(payload.get("expires_in", 7200)), 0)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise EbayAuthError("eBay application authentication failed") from exc
            self._token = token
            self._expires_at = time.monotonic() + max(expires_in - 60, 0)
            return token

    async def invalidate(self) -> None:
        async with self._lock:
            self._token = None
            self._expires_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()
