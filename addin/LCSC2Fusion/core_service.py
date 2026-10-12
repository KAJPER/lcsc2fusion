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

    def add_part(self, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> AddedPart:
        from lcsc2fusion import service

        result = service.convert_parts(
            [lcsc_id], out_dir, client=self._get_client(), with_3d=with_3d
        )
        part = result.parts[0]
        warnings = result.warnings
        step_path: Path | None = None
        placed_step_path: Path | None = None
        if with_3d:  # `models` only exists to carry 3D output, so it is not read otherwise
            model = next((m for m in result.models if m.lcsc_id.upper() == lcsc_id.upper()), None)
            if model is not None:
                step_path = model.step_path
                placed_step_path = model.placed_step_path
                for text in (f"{model.lcsc_id}: {w}" for w in model.warnings):
                    if text not in warnings:  # the core may already have reported it
                        warnings += (text,)
        return AddedPart(
            path=result.path,
            mpn=part.mpn,
            warnings=warnings,
            step_path=step_path,
            placed_step_path=placed_step_path,
        )
