"""convert_parts(part_transform=...): the hook is applied to every parsed part before writing."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from step_samples import make_box_step

from lcsc2fusion.errors import ConversionError, PartNotFoundError
from lcsc2fusion.model import Part
from lcsc2fusion.service import MODEL3D_ATTRIBUTE, convert_parts

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
IDS = {"r0603": "C9000001", "soic8": "C9000005", "box3d_rot90": "C9000016"}


def load_doc(name: str) -> dict[str, Any]:
    data = json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class StubClient:
    """Serves synthetic part documents and one STEP box for every model request."""

    def __init__(self) -> None:
        self.docs = {IDS[name]: load_doc(name) for name in IDS}

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        doc = self.docs.get(lcsc_id.strip().upper())
        if doc is None:
            raise PartNotFoundError(f"Part {lcsc_id} not found")
        return doc

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        return ""

    def fetch_model_step(self, uuid: str) -> bytes:
        return make_box_step(size=(1.0, 2.0, 0.9))


def package_names(path: Path) -> list[str]:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    return [p.get("name", "") for p in root.iter("package")]


def rename_package(new_name: str) -> Any:
    def transform(part: Part) -> Part:
        return replace(part, package=replace(part.package, name=new_name))

    return transform


def test_without_a_hook_the_output_is_unchanged(tmp_path: Path) -> None:
    plain = convert_parts([IDS["r0603"]], tmp_path / "a", client=StubClient())
    identity = convert_parts(
        [IDS["r0603"]], tmp_path / "b", client=StubClient(), part_transform=lambda part: part
    )
    unset = convert_parts([IDS["r0603"]], tmp_path / "c", client=StubClient(), part_transform=None)
    assert plain.path.read_bytes() == identity.path.read_bytes() == unset.path.read_bytes()
    assert plain.parts == identity.parts == unset.parts


def test_the_hook_changes_what_is_written_and_what_is_returned(tmp_path: Path) -> None:
    result = convert_parts(
        [IDS["r0603"]],
        tmp_path,
        client=StubClient(),
        part_transform=rename_package("MY_PACKAGE"),
    )
    assert package_names(result.path) == ["MY_PACKAGE"]
    (part,) = result.parts
    assert part.package.name == "MY_PACKAGE"


def test_the_hook_sees_every_part_once_in_order(tmp_path: Path) -> None:
    seen: list[str] = []

    def transform(part: Part) -> Part:
        seen.append(part.lcsc_id)
        return part

    convert_parts(
        [IDS["soic8"], IDS["r0603"]],
        tmp_path,
        client=StubClient(),
        library_name="both",
        part_transform=transform,
    )
    assert seen == [IDS["soic8"], IDS["r0603"]]


def test_the_hook_gets_the_parsed_part_before_the_3d_attribute_is_added(tmp_path: Path) -> None:
    received: list[Part] = []

    def transform(part: Part) -> Part:
        received.append(part)
        return replace(part, mpn="HOOKED-MPN")

    result = convert_parts(
        [IDS["box3d_rot90"]],
        tmp_path,
        client=StubClient(),
        with_3d=True,
        part_transform=transform,
    )
    (seen,) = received
    assert MODEL3D_ATTRIBUTE not in dict(seen.attributes)
    (part,) = result.parts
    assert part.mpn == "HOOKED-MPN"
    assert dict(part.attributes)[MODEL3D_ATTRIBUTE] == f"{IDS['box3d_rot90']}.placed.step"
    (files,) = result.models
    info = json.loads(files.info_path.read_text(encoding="utf-8"))
    assert info["mpn"] == "HOOKED-MPN"  # the 3D files were made from the transformed part


def test_an_error_in_the_hook_propagates_and_nothing_is_written(tmp_path: Path) -> None:
    def transform(part: Part) -> Part:
        raise ConversionError("hook refused the part")

    with pytest.raises(ConversionError, match="hook refused"):
        convert_parts([IDS["r0603"]], tmp_path, client=StubClient(), part_transform=transform)
    assert list(tmp_path.iterdir()) == []


def test_the_hook_is_not_called_when_the_download_fails(tmp_path: Path) -> None:
    calls: list[Part] = []

    def transform(part: Part) -> Part:
        calls.append(part)
        return part

    with pytest.raises(PartNotFoundError):
        convert_parts(["C1"], tmp_path, client=StubClient(), part_transform=transform)
    assert calls == []
