from irmscher_tracker.domain import NormalizedListing, Source
from irmscher_tracker.sources.base import SourceAdapter


class SscomAdapter(SourceAdapter):
    """SS.com adapter stub. Not yet implemented."""
    @property
    def source_name(self) -> Source:
        return Source.SSCOM
    async def search(self, queries: list[str]) -> list[NormalizedListing]:
        raise NotImplementedError("SS.com adapter not yet implemented")
    async def close(self) -> None:
        pass
