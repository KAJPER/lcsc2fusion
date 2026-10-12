"""High level operations shared by the CLI and the Fusion add-in (no adsk, no printing)."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lcsc2fusion.client.easyeda import EasyEdaClient, normalize_lcsc_id
from lcsc2fusion.client.search import search_parts
from lcsc2fusion.constants import SOURCE_LINKS, SOURCE_NAME
from lcsc2fusion.errors import ConversionError, ModelError, NetworkError, PartNotFoundError
from lcsc2fusion.lbr_writer.writer import write_library
from lcsc2fusion.model import Model3D, ModelFiles, Part, SearchResult
from lcsc2fusion.parser.model3d import MODEL_WARNING_PREFIX
from lcsc2fusion.parser.part import parse_part
from lcsc2fusion.step import BBox3, place_step, step_bbox

# Technology attribute that names the placed STEP file in the .lbr (the writer is not changed).
MODEL3D_ATTRIBUTE = "MODEL3D_STEP"

RAW_STEP_SUFFIX = ".step"
PLACED_STEP_SUFFIX = ".placed.step"
INFO_SUFFIX = ".3d.json"

# Stated in every <id>.3d.json (D-012); the package data licence covers 3D models too.
MODEL_LICENSE_NOTE = "Personal use only; copyright belongs to JLCEDA/EasyEDA."
TRANSFORM_FORMULA = (
    "q_xy = Rz(placement.rotation_deg[2]) * (p_xy - c) + placement.offset_mm; "
    "q_z = p_z - zmin + placement.bottom_z_mm. p is a point of the raw model, c the XY centre "
    "and zmin the lowest Z of its bounding box (bbox_mm.raw), Rz is counter-clockwise seen from "
    "+Z. Units are millimetres, Z is up, the package origin is (0, 0). Rotations about X and Y "
    "are not applied."
)

_BBOX_DECIMALS = 6


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of `convert_parts`.

    `models` has one entry per part whose 3D files were written (only with `with_3d`).
    `model_warnings` holds ready made lines (with the LCSC id) about parts whose 3D model was
    missing or could not be written. `with_3d` records whether 3D output was requested: the
    parsers put 3D placement remarks into `Part.warnings` regardless, and those are reported
    only when the user asked for 3D.
    """

    path: Path
    parts: tuple[Part, ...]
    models: tuple[ModelFiles, ...] = ()
    model_warnings: tuple[str, ...] = ()
    with_3d: bool = False

    @property
    def warnings(self) -> tuple[str, ...]:
        lines: list[str] = []
        for part in self.parts:
            lines.extend(
                f"{part.lcsc_id}: {w}"
                for w in part.warnings
                if self.with_3d or not w.startswith(MODEL_WARNING_PREFIX)
            )
        for model in self.models:
            lines.extend(f"{model.lcsc_id}: {w}" for w in model.warnings)
        lines.extend(self.model_warnings)
        return tuple(lines)


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
    with_3d: bool = False,
    part_transform: Callable[[Part], Part] | None = None,
) -> ConversionResult:
    """Download parts and write ONE .lbr containing all of them.

    The file is `<library_name>.lbr`, or `<id>.lbr` for a single part without a library name.

    With `with_3d`, every part that has a 3D model also gets `<id>.step` (as downloaded),
    `<id>.placed.step` (moved into the package frame) and `<id>.3d.json` (placement and
    attribution) next to the library, and its device gets the technology attribute
    `MODEL3D_STEP=<id>.placed.step`. A missing or unusable model never fails the conversion: the
    part is written without 3D files and `ConversionResult.warnings` says why.

    `part_transform`, when given, is applied to every parsed part before anything is written
    (3D files, the .lbr and `ConversionResult.parts` all see the transformed part). The Fusion
    add-in uses it to rename a footprint that would clash with a different one already in its
    accumulating library. None means no change.
    """
    if not lcsc_ids:
        raise ConversionError("No part ids given")
    client = client or EasyEdaClient()
    parts = tuple(load_part(lcsc_id, client) for lcsc_id in lcsc_ids)
    if part_transform is not None:
        parts = tuple(part_transform(part) for part in parts)
    models: list[ModelFiles] = []
    model_warnings: list[str] = []
    if with_3d:
        written: list[Part] = []
        for lcsc_id, part in zip(lcsc_ids, parts, strict=True):
            files = _write_model(lcsc_id, part, client, out_dir, model_warnings)
            if files is None:
                written.append(part)
                continue
            models.append(files)
            attribute = (MODEL3D_ATTRIBUTE, files.placed_step_path.name)
            written.append(dataclasses.replace(part, attributes=(*part.attributes, attribute)))
        parts = tuple(written)
    # The file name never comes from the downloaded document (`lcsc.number` is network data):
    # a single part is named after the id that was requested and validated.
    if library_name:
        name = _plain_file_stem(library_name)
    elif len(parts) == 1:
        name = normalize_lcsc_id(lcsc_ids[0])
    else:
        name = "lcsc2fusion"
    description = "LCSC parts: " + ", ".join(p.lcsc_id for p in parts)
    path = write_library(out_dir / f"{name}.lbr", parts, description=description)
    return ConversionResult(
        path=path,
        parts=parts,
        models=tuple(models),
        model_warnings=tuple(model_warnings),
        with_3d=with_3d,
    )


def _plain_file_stem(name: str) -> str:
    """`name` when it is a plain file name (no folders, no device names), else `ConversionError`."""
    stripped = name.strip()
    forbidden = set('<>:"/\\|?*\0')
    if (
        not stripped
        or stripped in {".", ".."}
        or stripped != name
        or stripped.endswith(".")
        or any(char in forbidden or ord(char) < 32 for char in stripped)
    ):
        raise ConversionError(f"{name!r} is not usable as a library file name")
    return stripped


def search(query: str, limit: int = 20, client: EasyEdaClient | None = None) -> list[SearchResult]:
    """Search by LCSC id or free text."""
    return search_parts(query, limit, client)


# 3D output


def _write_model(
    requested_id: str,
    part: Part,
    client: EasyEdaClient,
    out_dir: Path,
    problems: list[str],
) -> ModelFiles | None:
    """Download, place and write the 3D files of one part; None (and a line in `problems`) if not.

    File names come from the normalised LCSC id, never from anything the user typed as a path.
    """
    label = part.lcsc_id or requested_id
    model = part.model3d
    if model is None:
        problems.append(f"{label}: no 3D model available")
        return None
    try:
        lcsc_id = normalize_lcsc_id(requested_id)
    except PartNotFoundError:
        problems.append(
            f"{label}: 3D model not written: {requested_id!r} is not an LCSC part number"
        )
        return None
    try:
        raw = client.fetch_model_step(model.uuid)
        placed, place_warnings = place_step(raw, model)
        raw_box = step_bbox(raw)
        placed_box = step_bbox(placed)
        files = ModelFiles(
            lcsc_id=lcsc_id,
            step_path=out_dir / f"{lcsc_id}{RAW_STEP_SUFFIX}",
            placed_step_path=out_dir / f"{lcsc_id}{PLACED_STEP_SUFFIX}",
            info_path=out_dir / f"{lcsc_id}{INFO_SUFFIX}",
            warnings=tuple(place_warnings),
        )
        info = _info_json(part, model, files, raw, raw_box, placed_box)
        _write_all(
            [
                (files.step_path, raw),
                (files.placed_step_path, placed),
                (files.info_path, info),
            ]
        )
    except (ModelError, NetworkError, OSError) as exc:
        problems.append(f"{label}: 3D model not written: {exc}")
        return None
    return files


def _info_json(
    part: Part,
    model: Model3D,
    files: ModelFiles,
    raw: bytes,
    raw_box: BBox3,
    placed_box: BBox3,
) -> bytes:
    document: dict[str, Any] = {
        "lcsc_id": files.lcsc_id,
        "mpn": part.mpn,
        "model_uuid": model.uuid,
        "files": {"raw": files.step_path.name, "placed": files.placed_step_path.name},
        "placement": dataclasses.asdict(model),
        "transform": TRANSFORM_FORMULA,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bbox_mm": {"raw": _bbox_json(raw_box), "placed": _bbox_json(placed_box)},
        "warnings": [
            *(w for w in part.warnings if w.startswith(MODEL_WARNING_PREFIX)),
            *files.warnings,
        ],
        "source": {
            "name": SOURCE_NAME,
            "links": list(SOURCE_LINKS),
            "notice": MODEL_LICENSE_NOTE,
        },
    }
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _bbox_json(box: BBox3) -> dict[str, list[float]]:
    def rounded(values: tuple[float, float, float]) -> list[float]:
        return [round(v, _BBOX_DECIMALS) + 0.0 for v in values]

    return {
        "min": rounded((box.min_x, box.min_y, box.min_z)),
        "max": rounded((box.max_x, box.max_y, box.max_z)),
    }


def _write_all(files: Sequence[tuple[Path, bytes]]) -> None:
    """Write several files so that a failure leaves none of them half written.

    Every file is first written completely to a temporary file in the target directory; only when
    all of them exist are they moved into place with `os.replace`. If a move fails, the targets
    already moved are removed again (a mixed old and new set would be worse than none). Raises
    OSError on failure.
    """
    staged: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for target, data in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".part")
            os.close(fd)  # an open descriptor would block deleting the file on Windows
            temp = Path(temp_name)
            staged.append((temp, target))
            temp.write_bytes(data)
        while staged:
            temp, target = staged[0]
            os.replace(temp, target)
            staged.pop(0)
            committed.append(target)
    except BaseException:
        for target in committed:
            with contextlib.suppress(OSError):
                target.unlink()
        raise
    finally:
        for temp, _ in staged:
            with contextlib.suppress(OSError):
                temp.unlink()
