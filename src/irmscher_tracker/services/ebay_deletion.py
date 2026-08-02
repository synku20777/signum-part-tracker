from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import textwrap
import time
from contextlib import suppress
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from irmscher_tracker.db.models import EbayDeletionNotificationRow
from irmscher_tracker.db.repositories import EbayDeletionRepository
from irmscher_tracker.sources.ebay_client import (
    EbayApplicationTokenProvider,
    EbayAuthError,
    EbayEndpoints,
)

logger = logging.getLogger(__name__)
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


class EbaySignatureError(Exception):
    """The notification signature is missing, malformed, or invalid."""


class EbayVerificationUnavailable(Exception):
    """eBay key retrieval is temporarily unavailable."""


def notification_correlation(notification_id: str) -> str:
    return hashlib.sha256(notification_id.encode()).hexdigest()[:12]


def serialize_ebay_payload(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class EbayNotificationVerifier:
    def __init__(
        self,
        token_provider: EbayApplicationTokenProvider,
        endpoints: EbayEndpoints,
        timeout: float,
    ) -> None:
        self._token_provider = token_provider
        self._endpoints = endpoints
        self._client = httpx.AsyncClient(timeout=min(timeout, 10.0))
        self._keys: dict[str, tuple[float, ec.EllipticCurvePublicKey]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def verify(self, payload: Any, signature_header: str | None) -> None:
        key_id, signature = self._parse_signature(signature_header)
        public_key = await self._get_public_key(key_id)
        try:
            public_key.verify(signature, serialize_ebay_payload(payload), ec.ECDSA(hashes.SHA1()))
        except (InvalidSignature, ValueError) as exc:
            raise EbaySignatureError("Invalid eBay notification signature") from exc

    @staticmethod
    def _parse_signature(value: str | None) -> tuple[str, bytes]:
        if not value or len(value) > 8192:
            raise EbaySignatureError("Invalid eBay notification signature")
        try:
            decoded = base64.b64decode(value, validate=True)
            if len(decoded) > 6144:
                raise ValueError
            header = json.loads(decoded)
            key_id = header["kid"]
            encoded_signature = header["signature"]
            if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
                raise ValueError
            if not isinstance(encoded_signature, str) or len(encoded_signature) > 4096:
                raise ValueError
            signature = base64.b64decode(encoded_signature, validate=True)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise EbaySignatureError("Invalid eBay notification signature") from exc
        return key_id, signature

    async def _get_public_key(self, key_id: str) -> ec.EllipticCurvePublicKey:
        cached = self._keys.get(key_id)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        lock = self._locks.setdefault(key_id, asyncio.Lock())
        try:
            async with lock:
                cached = self._keys.get(key_id)
                if cached is not None and cached[0] > time.monotonic():
                    return cached[1]
                key = await self._fetch_public_key(key_id)
                self._keys[key_id] = (time.monotonic() + 3600, key)
                return key
        finally:
            if not lock.locked():
                self._locks.pop(key_id, None)

    async def _fetch_public_key(self, key_id: str) -> ec.EllipticCurvePublicKey:
        try:
            token = await self._token_provider.get_token()
            response = await self._client.get(
                f"{self._endpoints.notification_public_key_base_url}{key_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                await self._token_provider.invalidate()
                token = await self._token_provider.get_token()
                response = await self._client.get(
                    f"{self._endpoints.notification_public_key_base_url}{key_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            response.raise_for_status()
            key_text = str(response.json()["key"])
            key_body = key_text.replace("-----BEGIN PUBLIC KEY-----", "").replace(
                "-----END PUBLIC KEY-----", ""
            )
            key_body = "".join(key_body.split())
            key_text = (
                "-----BEGIN PUBLIC KEY-----\n"
                + "\n".join(textwrap.wrap(key_body, 64))
                + "\n-----END PUBLIC KEY-----\n"
            )
            key = serialization.load_pem_public_key(key_text.encode())
            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise TypeError
            return key
        except (EbayAuthError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EbayVerificationUnavailable("eBay public key retrieval failed") from exc

    async def close(self) -> None:
        await self._client.aclose()


class EbayDeletionWorker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._repository = EbayDeletionRepository()
        self._wakeup = asyncio.Event()

    async def wake(self) -> None:
        self._wakeup.set()

    async def run(self) -> None:
        async with self._session_factory() as session:
            await self._repository.recover_expired(session)
            await session.commit()
        while True:
            processed = await self._process_next()
            if processed:
                continue
            self._wakeup.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=5.0)

    async def _process_next(self) -> bool:
        async with self._session_factory() as session:
            row = await self._repository.claim_next(session)
            await session.commit()
            if row is None:
                return False
            row_id = row.id
            correlation = notification_correlation(row.notification_id)

        try:
            async with self._session_factory() as session:
                current = await session.get(EbayDeletionNotificationRow, row_id)
                if current is None or current.status != "processing":
                    return True
                await self._repository.anonymize(session, current)
                await self._repository.mark_processed(session, current)
                await session.commit()
            logger.info("Deletion notification processed correlation=%s", correlation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._session_factory() as session:
                current = await session.get(EbayDeletionNotificationRow, row_id)
                if current is not None and current.status == "processing":
                    await self._repository.retry(session, current, type(exc).__name__)
                    await session.commit()
            logger.error(
                "Deletion notification processing deferred correlation=%s error=%s",
                correlation,
                type(exc).__name__,
            )
        return True
