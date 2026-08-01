from decimal import Decimal

import httpx
import pytest
import respx

from irmscher_tracker.domain import ListingCondition, Source
from irmscher_tracker.sources.ebay import (
    EBAY_AUTH_URL,
    EBAY_SEARCH_URL,
    EbayAdapter,
    EbayAuthError,
)


def get_sample_response():
    return {
        "itemSummaries": [
            {
                "itemId": "v1|123456789|0",
                "title": "Irmscher Frontspoiler Signum i3401009",
                "price": {"value": "299.99", "currency": "EUR"},
                "itemWebUrl": "https://www.ebay.de/itm/123456789",
                "condition": "Used",
                "conditionId": "3000",
                "image": {"imageUrl": "https://i.ebayimg.com/images/test.jpg"},
                "seller": {"username": "test_seller"},
                "itemLocation": {"country": "DE", "postalCode": "12345"},
                "shippingOptions": [{"shippingCost": {"value": "9.99", "currency": "EUR"}}],
            }
        ],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }


@pytest.fixture
def ebay_adapter():
    return EbayAdapter(client_id="test", client_secret="test")


@pytest.mark.asyncio
@respx.mock
async def test_authentication(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )
    token = await ebay_adapter._get_token()
    assert token == "test_token"
    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_listings(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )
    respx.get(EBAY_SEARCH_URL).mock(return_value=httpx.Response(200, json=get_sample_response()))

    result = await ebay_adapter.search(["i3401009"])
    assert len(result.hits) == 1
    listing = result.hits[0].listing

    assert listing.source == Source.EBAY
    assert listing.external_id == "v1|123456789|0"
    assert listing.title == "Irmscher Frontspoiler Signum i3401009"
    assert listing.price == Decimal("299.99")
    assert listing.condition == ListingCondition.USED

    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_pagination(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )

    # 3 pages, first two have 50 items, third has 10
    resp_page_1 = get_sample_response()
    resp_page_1["itemSummaries"] = [resp_page_1["itemSummaries"][0].copy() for _ in range(50)]
    for i, item in enumerate(resp_page_1["itemSummaries"]):
        item["itemId"] = f"v1|page1_{i}|0"

    resp_page_2 = get_sample_response()
    resp_page_2["itemSummaries"] = [resp_page_2["itemSummaries"][0].copy() for _ in range(50)]
    for i, item in enumerate(resp_page_2["itemSummaries"]):
        item["itemId"] = f"v1|page2_{i}|0"

    resp_page_last = get_sample_response()
    resp_page_last["itemSummaries"] = [
        resp_page_last["itemSummaries"][0].copy() for _ in range(10)
    ]
    for i, item in enumerate(resp_page_last["itemSummaries"]):
        item["itemId"] = f"v1|last{i}|0"

    respx.get(EBAY_SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=resp_page_1),
            httpx.Response(200, json=resp_page_2),
            httpx.Response(200, json=resp_page_last),
        ]
    )

    # Limit per query default is 200, so we should get 50 + 50 + 10 = 110 items
    # Wait, the deduplication happens, so I need to make sure itemIds are unique across pages
    # I did that by mutating itemId. Wait, side_effect will reuse the exact dictionary.

    result = await ebay_adapter.search(["i3401009"])
    assert len(result.hits) == 110

    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_auth_failure_raises(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid"}))

    with pytest.raises(EbayAuthError):
        await ebay_adapter.search(["test"])

    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_retry(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )

    respx.get(EBAY_SEARCH_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=get_sample_response())]
    )

    result = await ebay_adapter.search(["test"])
    assert len(result.hits) == 1

    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_empty_results(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )
    respx.get(EBAY_SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"itemSummaries": [], "total": 0, "limit": 50, "offset": 0}
        )
    )

    result = await ebay_adapter.search(["test"])
    assert len(result.hits) == 0
    assert result.complete is True

    await ebay_adapter.close()


@pytest.mark.asyncio
@respx.mock
async def test_deduplication_across_queries(ebay_adapter):
    respx.post(EBAY_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test_token"})
    )
    respx.get(EBAY_SEARCH_URL).mock(return_value=httpx.Response(200, json=get_sample_response()))

    # query 1 and query 2 will return the same mocked item
    result = await ebay_adapter.search(["query1", "query2"])

    # same itemId should be deduplicated
    assert len(result.hits) == 1
    assert result.hits[0].queries == {"query1", "query2"}

    await ebay_adapter.close()
