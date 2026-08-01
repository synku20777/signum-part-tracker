from irmscher_tracker.domain import Source, SourceSearchResult
from irmscher_tracker.sources.base import SourceAdapter


class OvokoAdapter(SourceAdapter):
    """Ovoko adapter stub. Not yet implemented."""

    @property
    def source_name(self) -> Source:
        return Source.OVOKO

    async def search(self, queries: list[str]) -> SourceSearchResult:
        raise NotImplementedError("Ovoko adapter not yet implemented")

    async def close(self) -> None:
        pass
