"""eBay Browse API source adapter.

Uses OAuth client-credentials authentication and the Browse API
item_summary/search endpoint. All eBay-specific data transformations
stay inside this module.
"""

from __future__ import annotations

import base64
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from irmscher_tracker.domain import (
    ListingCondition,
    NormalizedListing,
    SearchHit,
    Source,
    SourceSearchResult,
)
from irmscher_tracker.sources.base import SourceAdapter

logger = logging.getLogger(__name__)

EBAY_AUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Map eBay condition strings/IDs to our enum.
_CONDITION_MAP: dict[str, ListingCondition] = {
    "New": ListingCondition.NEW,
    "1000": ListingCondition.NEW,
    "New Other": ListingCondition.NEW,
    "1500": ListingCondition.NEW,
    "Remanufactured": ListingCondition.REFURBISHED,
    "2000": ListingCondition.REFURBISHED,
    "Certified Refurbished": ListingCondition.REFURBISHED,
    "2010": ListingCondition.REFURBISHED,
    "Seller refurbished": ListingCondition.REFURBISHED,
    "2500": ListingCondition.REFURBISHED,
    "Used": ListingCondition.USED,
    "3000": ListingCondition.USED,
    "Very Good": ListingCondition.USED,
    "4000": ListingCondition.USED,
    "Good": ListingCondition.USED,
    "5000": ListingCondition.USED,
    "Acceptable": ListingCondition.USED,
    "6000": ListingCondition.USED,
    "For parts or not working": ListingCondition.PARTS_ONLY,
    "7000": ListingCondition.PARTS_ONLY,
}


class EbayAuthError(Exception):
    """Raised when eBay authentication fails."""


class EbayApiError(Exception):
    """Raised on non-retryable eBay API errors."""


class RetryableEbayError(Exception):
    """Raised for eBay responses that are safe to retry."""


class EbayAdapter(SourceAdapter):
    """eBay Browse API adapter.

    Searches via OAuth client-credentials flow, paginates results,
    and normalises every item into a ``NormalizedListing``.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace_id: str = "EBAY_DE",
        timeout: float = 30.0,
        max_results_per_query: int = 200,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._marketplace_id = marketplace_id
        self._max_results_per_query = max_results_per_query
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_expires_at = 0.0

    # -- SourceAdapter interface ----------------------------------------

    @property
    def source_name(self) -> Source:
        return Source.EBAY

    async def search(self, queries: list[str]) -> SourceSearchResult:
        """Search eBay and preserve completeness and query provenance."""
        seen: dict[str, SearchHit] = {}
        successful_queries: list[str] = []
        query_errors: dict[str, str] = {}
        token = await self._get_token()

        for query in queries:
            try:
                items, complete = await self._search_single(query, token)
                for item in items:
                    listing = self._normalize(item)
                    hit = seen.setdefault(
                        listing.external_id,
                        SearchHit(listing=listing),
                    )
                    hit.queries.add(query)
                if complete:
                    successful_queries.append(query)
                else:
                    query_errors[query] = "Result limit reached before pagination completed"
            except EbayAuthError:
                raise
            except Exception as exc:
                logger.exception("Error searching eBay for query '%s'", query)
                query_errors[query] = type(exc).__name__

        return SourceSearchResult(
            hits=list(seen.values()),
            successful_queries=successful_queries,
            query_errors=query_errors,
            discovery_complete=not query_errors,
            enrichment_complete=True,
        )

    async def close(self) -> None:
        """Shut down the underlying HTTP client."""
        await self._client.aclose()

    # -- Authentication -------------------------------------------------

    async def _authenticate(self) -> str:
        """Obtain an OAuth token via client-credentials grant."""
        logger.info("Authenticating with eBay API")
        credentials = f"{self._client_id}:{self._client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()

        response = await self._client.post(
            EBAY_AUTH_URL,
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": EBAY_SCOPE,
            },
        )

        if response.status_code == 401:
            raise EbayAuthError("Invalid eBay client credentials")
        response.raise_for_status()

        data = response.json()
        token: str = data["access_token"]
        expires_in = max(int(data.get("expires_in", 7200)), 0)
        self._token_expires_at = time.monotonic() + max(expires_in - 60, 0)
        return token

    async def _get_token(self) -> str:
        """Return a cached token, or authenticate first."""
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._token = await self._authenticate()
        return self._token

    # -- Search ---------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, RetryableEbayError)),
    )
    async def _search_single(self, query: str, token: str) -> tuple[list[dict[str, Any]], bool]:
        """Execute a single search query with pagination."""
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id,
        }

        results: list[dict[str, Any]] = []
        offset = 0
        page_size = 50
        total: int | None = None

        while offset < self._max_results_per_query:
            params: dict[str, str | int] = {
                "q": query,
                "limit": page_size,
                "offset": offset,
            }

            response = await self._client.get(
                EBAY_SEARCH_URL,
                headers=headers,
                params=params,
            )

            # Handle token expiry transparently.
            if response.status_code == 401:
                logger.warning("eBay token expired, re-authenticating")
                self._token = await self._authenticate()
                headers["Authorization"] = f"Bearer {self._token}"
                response = await self._client.get(
                    EBAY_SEARCH_URL,
                    headers=headers,
                    params=params,
                )

            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableEbayError(f"eBay returned HTTP {response.status_code}")
            if response.is_error:
                raise EbayApiError(f"eBay returned HTTP {response.status_code}")

            data = response.json()
            response_total = data.get("total")
            total = response_total if isinstance(response_total, int) else total
            items: list[dict[str, Any]] = data.get("itemSummaries", [])
            results.extend(items)

            if len(items) < page_size:
                break

            offset += page_size

        reached_limit = len(results) >= self._max_results_per_query
        complete = not reached_limit or (isinstance(total, int) and total <= len(results))
        return results[: self._max_results_per_query], complete

    # -- Normalisation --------------------------------------------------

    @staticmethod
    def _normalize(item: dict[str, Any]) -> NormalizedListing:
        """Map an eBay ``itemSummary`` to a ``NormalizedListing``."""
        item_id: str = item.get("itemId", "")
        title: str = item.get("title", "")
        description: str = item.get("shortDescription", "")

        # Price
        price_data = item.get("price", {})
        try:
            raw_price = price_data.get("value")
            price = Decimal(str(raw_price)) if raw_price is not None else None
        except InvalidOperation:
            price = None
        currency: str = price_data.get("currency", "EUR")

        # Shipping
        shipping_cost: Decimal | None = None
        shipping_options = item.get("shippingOptions", [])
        if shipping_options:
            ship_val = shipping_options[0].get("shippingCost", {}).get("value")
            if ship_val is not None:
                with suppress(InvalidOperation):
                    shipping_cost = Decimal(str(ship_val))

        # Condition
        condition_str = item.get("condition", "")
        condition_id = item.get("conditionId", "")
        condition = (
            _CONDITION_MAP.get(condition_str)
            or _CONDITION_MAP.get(str(condition_id))
            or ListingCondition.UNKNOWN
        )

        # URL
        url_raw: str = item.get("itemWebUrl") or item.get("itemHref", "")
        url = url_raw.split("?")[0] if url_raw else ""

        # Images
        image_urls: list[str] = []
        image_data = item.get("image", {})
        primary_img = image_data.get("imageUrl")
        if primary_img:
            image_urls.append(primary_img)

        for add_img in item.get("additionalImages", []):
            img_url = add_img.get("imageUrl")
            if img_url and img_url not in image_urls:
                image_urls.append(img_url)

        # Seller
        seller_data = item.get("seller", {})
        seller_username = seller_data.get("username", "")
        feedback_score = seller_data.get("feedbackScore")
        feedback_percentage = seller_data.get("feedbackPercentage")

        seller_info = seller_username
        if feedback_score is not None and feedback_percentage is not None:
            seller_info = f"{seller_username} ({feedback_score}, {feedback_percentage}%)"

        seller = seller_info

        # Location
        location_data = item.get("itemLocation", {})
        parts = [
            location_data.get("postalCode", ""),
            location_data.get("city", ""),
            location_data.get("country", ""),
        ]
        seller_location = ", ".join(p for p in parts if p)

        return NormalizedListing(
            source=Source.EBAY,
            external_id=item_id,
            title=title,
            description=description,
            url=url,
            image_urls=image_urls,
            price=price,
            currency=currency,
            shipping_cost=shipping_cost,
            condition=condition,
            seller=seller,
            seller_location=seller_location,
            published_at=datetime.now(UTC),
        )
