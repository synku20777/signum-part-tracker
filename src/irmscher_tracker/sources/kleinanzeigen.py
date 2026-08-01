from irmscher_tracker.domain import Source, SourceSearchResult
from irmscher_tracker.sources.base import SourceAdapter


class KleinanzeigenAdapter(SourceAdapter):
    """Kleinanzeigen adapter stub. Not yet implemented."""

    @property
    def source_name(self) -> Source:
        return Source.KLEINANZEIGEN

    async def search(self, queries: list[str]) -> SourceSearchResult:
        raise NotImplementedError("Kleinanzeigen adapter not yet implemented")

    async def close(self) -> None:
        pass
