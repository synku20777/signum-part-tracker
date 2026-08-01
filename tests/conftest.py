from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from irmscher_tracker.db.models import Base
from irmscher_tracker.domain import ListingCondition, NormalizedListing, Source
from irmscher_tracker.matcher import PartMatcher
from irmscher_tracker.settings import Settings


@pytest.fixture
def settings():
    return Settings(
        api_token="test-api-token-test-api-token-123456",
        database_url="sqlite+aiosqlite:///:memory:",
        ebay_client_id="dummy_id",
        ebay_client_secret="dummy_secret",
        telegram_bot_token="dummy_token",
        telegram_chat_id="dummy_chat",
        minimum_match_score=50,
        price_drop_percent=Decimal("5.0"),
        config_directory="config",
        scan_on_startup=False,
    )


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def db_session(session_factory):
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
def sample_listing():
    return NormalizedListing(
        source=Source.EBAY,
        external_id="12345",
        title="Irmscher Frontspoiler Signum i3401009",
        description="Used front spoiler",
        url="https://ebay.de/itm/12345",
        price=Decimal("299.99"),
        currency="EUR",
        condition=ListingCondition.USED,
        published_at=datetime.now(UTC),
    )


@pytest.fixture
def parts_config_path():
    return Path(__file__).resolve().parents[1] / "config" / "parts.yaml"


@pytest.fixture
def matcher(parts_config_path):
    return PartMatcher(parts_config_path)
