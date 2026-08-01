from __future__ import annotations

import abc

from irmscher_tracker.domain import NormalizedListing, Source


class SourceAdapter(abc.ABC):
    """Abstract base class for marketplace source adapters."""

    @property
    @abc.abstractmethod
    def source_name(self) -> Source:
        """Return the source identifier."""

    @abc.abstractmethod
    async def search(self, queries: list[str]) -> list[NormalizedListing]:
        """Search the marketplace with the given queries and return normalized listings."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
