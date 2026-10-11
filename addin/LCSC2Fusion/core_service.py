"""`PartService` backed by the lcsc2fusion core package (no `adsk` import).

The core is imported lazily, inside the methods, so the add-in modules stay importable (and the
controller testable) even while the core is incomplete or missing. These methods run on the
worker thread: they must not call Fusion.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from controller import AddedPart

if TYPE_CHECKING:
    from lcsc2fusion.client.easyeda import EasyEdaClient
    from lcsc2fusion.model import SearchResult


class CorePartService:
    """Searches and converts parts. One client is shared so rate limiting and cache apply."""

    def __init__(self, client: EasyEdaClient | None = None) -> None:
        self._client = client

    def _get_client(self) -> EasyEdaClient:
        if self._client is None:
            from lcsc2fusion.client.easyeda import EasyEdaClient

            self._client = EasyEdaClient()
        return self._client

    def search(self, query: str, limit: int) -> list[SearchResult]:
        from lcsc2fusion import service

        return service.search(query, limit, self._get_client())

    def add_part(self, lcsc_id: str, out_dir: Path) -> AddedPart:
        from lcsc2fusion import service

        result = service.convert_parts([lcsc_id], out_dir, client=self._get_client())
        part = result.parts[0]
        return AddedPart(path=result.path, mpn=part.mpn, warnings=result.warnings)
