from irmscher_tracker.domain import NormalizedListing, Source
from irmscher_tracker.sources.base import SourceAdapter


class AllegroAdapter(SourceAdapter):
    """Allegro adapter stub. Not yet implemented."""
    @property
    def source_name(self) -> Source:
        return Source.ALLEGRO
    async def search(self, queries: list[str]) -> list[NormalizedListing]:
        raise NotImplementedError("Allegro adapter not yet implemented")
    async def close(self) -> None:
        pass
