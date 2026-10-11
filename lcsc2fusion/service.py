"""High level operations shared by the CLI and the Fusion add-in (no adsk, no printing)."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.client.search import search_parts
from lcsc2fusion.errors import ConversionError
from lcsc2fusion.lbr_writer.writer import write_library
from lcsc2fusion.model import Part, SearchResult
from lcsc2fusion.parser.part import parse_part


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    parts: tuple[Part, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(f"{p.lcsc_id}: {w}" for p in self.parts for w in p.warnings)


def load_part(
    lcsc_id: str, client: EasyEdaClient | None = None, *, with_datasheet: bool = True
) -> Part:
    """Download and parse one part."""
    client = client or EasyEdaClient()
    part = parse_part(client.fetch_part(lcsc_id))
    if with_datasheet and not part.datasheet:
        datasheet = client.fetch_datasheet_url(lcsc_id)
        if datasheet:
            part = dataclasses.replace(part, datasheet=datasheet)
    return part


def convert_parts(
    lcsc_ids: Sequence[str],
    out_dir: Path,
    *,
    client: EasyEdaClient | None = None,
    library_name: str | None = None,
) -> ConversionResult:
    """Download parts and write ONE .lbr containing all of them.

    The file is `<library_name>.lbr`, or `<id>.lbr` for a single part without a library name.
    """
    if not lcsc_ids:
        raise ConversionError("No part ids given")
    client = client or EasyEdaClient()
    parts = tuple(load_part(lcsc_id, client) for lcsc_id in lcsc_ids)
    name = library_name or (parts[0].lcsc_id if len(parts) == 1 else "lcsc2fusion")
    description = "LCSC parts: " + ", ".join(p.lcsc_id for p in parts)
    path = write_library(out_dir / f"{name}.lbr", parts, description=description)
    return ConversionResult(path=path, parts=parts)


def search(query: str, limit: int = 20, client: EasyEdaClient | None = None) -> list[SearchResult]:
    """Search by LCSC id or free text."""
    return search_parts(query, limit, client)
