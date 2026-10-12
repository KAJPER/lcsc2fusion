"""convert_parts never takes a file name from the downloaded document (network data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.errors import ConversionError, PartNotFoundError
from lcsc2fusion.service import convert_parts

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
ID = "C9000001"


def hostile_document(number: str) -> dict[str, Any]:
    data = json.loads((SYNTH / "r0603.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    data["result"]["lcsc"]["number"] = number
    return data  # type: ignore[no-any-return]


class StubClient:
    def __init__(self, number: str) -> None:
        self._doc = hostile_document(number)

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        if lcsc_id.strip().upper() != ID:
            raise PartNotFoundError(lcsc_id)
        return self._doc

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        return ""


@pytest.mark.parametrize("number", ["../../evil", "..\\..\\evil", "C1/../../x", "con", "a:b"])
def test_a_hostile_lcsc_number_in_the_document_cannot_move_the_file(
    tmp_path: Path, number: str
) -> None:
    out = tmp_path / "out"
    result = convert_parts([ID], out, client=StubClient(number))  # type: ignore[arg-type]
    assert result.path == out / f"{ID}.lbr"
    assert [p.name for p in tmp_path.iterdir()] == ["out"]
    assert [p.name for p in out.iterdir()] == [f"{ID}.lbr"]


@pytest.mark.parametrize(
    "name", ["", " ", "..", ".", "a/b", "a\\b", "C:\\x", "x\0y", "bad.", " lead", "q?", "s*"]
)
def test_a_library_name_must_be_a_plain_file_name(tmp_path: Path, name: str) -> None:
    if name == "":  # empty means "no name given": the id is used
        result = convert_parts([ID], tmp_path, client=StubClient("C1"))  # type: ignore[arg-type]
        assert result.path.name == f"{ID}.lbr"
        return
    with pytest.raises(ConversionError):
        convert_parts([ID], tmp_path, client=StubClient("C1"), library_name=name)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_a_normal_library_name_is_kept(tmp_path: Path) -> None:
    result = convert_parts(
        [ID],
        tmp_path,
        client=StubClient("C1"),  # type: ignore[arg-type]
        library_name="My parts 2026",
    )
    assert result.path == tmp_path / "My parts 2026.lbr"
