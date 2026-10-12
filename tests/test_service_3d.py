"""convert_parts(with_3d=True) with a stub client: files, .3d.json, .lbr attribute, isolation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from step_samples import make_assembly_step, make_box_step

import lcsc2fusion.service as service
from lcsc2fusion.constants import SOURCE_LINKS, SOURCE_NAME
from lcsc2fusion.errors import ModelError, NetworkError, PartNotFoundError, RateLimitedError
from lcsc2fusion.lbr_writer import lint_library
from lcsc2fusion.model import Part
from lcsc2fusion.parser.model3d import MODEL_WARNING_PREFIX
from lcsc2fusion.parser.part import parse_part
from lcsc2fusion.service import ConversionResult, convert_parts
from lcsc2fusion.step import step_bbox

FIXTURES = Path(__file__).parent / "fixtures"
SYNTH = FIXTURES / "synthetic"
REAL = FIXTURES / "real"

IDS = {
    "r0603": "C9000001",
    "sot23": "C9000004",  # no SVGNODE
    "box3d_rot90": "C9000016",
    "box3d_z": "C9000017",
    "box3d_stale": "C9000018",
}
MODEL_NAMES = ("box3d_rot90", "box3d_z", "box3d_stale")
BOX_HEIGHT_MM = 0.9
# Where the stub's raw model sits: the service must move it from here onto the package frame.
RAW_PLACEMENT: Mapping[str, tuple[tuple[float, float], float]] = {
    "box3d_rot90": ((12.0, -7.5), -0.3),
    "box3d_z": ((-3.25, 40.0), 1.7),
    "box3d_stale": ((0.0, 0.0), 0.0),
    "r0603": ((0.4, 0.2), -0.01),
}
TOLERANCE_MM = 1e-6


# Fixtures and doubles


def load_doc(name: str) -> dict[str, Any]:
    data = json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def sidecar_model(name: str) -> dict[str, Any]:
    expected = json.loads((SYNTH / f"{name}.expected.json").read_text(encoding="utf-8"))
    model = expected["model3d"]
    assert isinstance(model, dict)
    return model


def box_step(name: str) -> bytes:
    """A STEP box with the unrotated native size from the sidecar, off centre and off the floor."""
    native = sidecar_model(name)["native_size_mm"]
    centre, zmin = RAW_PLACEMENT[name]
    return make_box_step(size=(native[0], native[1], BOX_HEIGHT_MM), center=centre, zmin=zmin)


def model_uuid(name: str) -> str:
    uuid = sidecar_model(name)["uuid"]
    assert isinstance(uuid, str)
    return uuid


class StubClient:
    """Serves synthetic part documents and STEP files, records every model request."""

    def __init__(
        self,
        models: Mapping[str, bytes | Exception] | None = None,
        docs: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.docs = dict(docs) if docs is not None else {IDS[n]: load_doc(n) for n in IDS}
        self.models: dict[str, bytes | Exception] = (
            dict(models)
            if models is not None
            else {model_uuid(n): box_step(n) for n in MODEL_NAMES}
        )
        self.model_calls: list[str] = []

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        doc = self.docs.get(lcsc_id.strip().upper())
        if doc is None:
            raise PartNotFoundError(f"Part {lcsc_id} not found")
        return doc

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        return ""

    def fetch_model_step(self, uuid: str) -> bytes:
        self.model_calls.append(uuid)
        outcome = self.models.get(uuid)
        if outcome is None:
            raise ModelError(f"3D model {uuid} not found on EasyEDA (HTTP 404).")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def lbr_attributes(path: Path, name: str) -> list[str]:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    return [a.get("value", "") for a in root.iter("attribute") if a.get("name") == name]


def expected_placed_bbox(
    name: str, size_z: float
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Placed bbox from the sidecar values alone: centred on offset, sizes swapped for 90/270."""
    model = sidecar_model(name)
    ox, oy = model["offset_mm"]
    sx, sy = model["native_size_mm"]
    rz = model["rotation_deg"][2]
    assert rz % 90 == 0
    swapped = round(rz / 90) % 2 == 1
    w, h = (sy, sx) if swapped else (sx, sy)
    z0 = model["bottom_z_mm"]
    return (ox - w / 2, oy - h / 2, z0), (ox + w / 2, oy + h / 2, z0 + size_z)


def assert_bbox(box: Any, low: tuple[float, ...], high: tuple[float, ...]) -> None:
    got_low = (box.min_x, box.min_y, box.min_z)
    got_high = (box.max_x, box.max_y, box.max_z)
    assert got_low == pytest.approx(low, abs=TOLERANCE_MM)
    assert got_high == pytest.approx(high, abs=TOLERANCE_MM)


# Files written


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_writes_raw_placed_and_info_files_next_to_the_library(tmp_path: Path, name: str) -> None:
    client = StubClient()
    lcsc_id = IDS[name]
    result = convert_parts([lcsc_id], tmp_path, client=client, with_3d=True)

    assert listing(tmp_path) == sorted(
        [f"{lcsc_id}.lbr", f"{lcsc_id}.step", f"{lcsc_id}.placed.step", f"{lcsc_id}.3d.json"]
    )
    (files,) = result.models
    assert files.lcsc_id == lcsc_id
    assert files.step_path == tmp_path / f"{lcsc_id}.step"
    assert files.placed_step_path == tmp_path / f"{lcsc_id}.placed.step"
    assert files.info_path == tmp_path / f"{lcsc_id}.3d.json"
    assert files.warnings == ()
    assert result.path == tmp_path / f"{lcsc_id}.lbr"
    assert client.model_calls == [model_uuid(name)]


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_raw_file_is_byte_identical_to_the_download(tmp_path: Path, name: str) -> None:
    client = StubClient()
    result = convert_parts([IDS[name]], tmp_path, client=client, with_3d=True)
    (files,) = result.models
    downloaded = box_step(name)
    assert files.step_path.read_bytes() == downloaded
    assert b"\xb5\xc4\xbe\xb5\xcf\xf2" in files.step_path.read_bytes()  # GBK like bytes intact
    assert files.placed_step_path.read_bytes() != downloaded


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_placed_bbox_matches_the_independent_expectation(tmp_path: Path, name: str) -> None:
    (files,) = convert_parts([IDS[name]], tmp_path, client=StubClient(), with_3d=True).models
    low, high = expected_placed_bbox(name, BOX_HEIGHT_MM)
    assert_bbox(step_bbox(files.placed_step_path.read_bytes()), low, high)
    # The raw model really was elsewhere, so the check above proves a move happened.
    raw = step_bbox(files.step_path.read_bytes())
    assert raw.center_xy != pytest.approx(sidecar_model(name)["offset_mm"], abs=0.1)


def test_each_fixture_exercises_a_different_placement() -> None:
    models = [sidecar_model(n) for n in MODEL_NAMES]
    assert {m["rotation_deg"][2] for m in models} == {90.0, 0.0, 180.0}
    assert {m["placement_source"] for m in models} == {"c_origin", "outline_centre"}
    assert min(m["bottom_z_mm"] for m in models) < 0 < max(m["bottom_z_mm"] for m in models)


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_info_json_content(tmp_path: Path, name: str) -> None:
    lcsc_id = IDS[name]
    (files,) = convert_parts([lcsc_id], tmp_path, client=StubClient(), with_3d=True).models
    text = files.info_path.read_text(encoding="utf-8")
    assert text.startswith("{\n  ")  # indent 2
    assert text.endswith("}\n")
    info = json.loads(text)
    want = sidecar_model(name)

    assert info["lcsc_id"] == lcsc_id
    assert info["mpn"] == load_doc(name)["result"]["dataStr"]["head"]["c_para"]["Manufacturer Part"]
    assert info["model_uuid"] == want["uuid"]
    assert info["files"] == {"raw": f"{lcsc_id}.step", "placed": f"{lcsc_id}.placed.step"}

    placement = info["placement"]
    assert placement["uuid"] == want["uuid"]
    assert placement["name"] == want["name"]
    assert placement["offset_mm"] == pytest.approx(want["offset_mm"], abs=1e-4)
    assert placement["bottom_z_mm"] == pytest.approx(want["bottom_z_mm"], abs=1e-4)
    assert placement["rotation_deg"] == want["rotation_deg"]
    assert placement["native_size_mm"] == pytest.approx(want["native_size_mm"], abs=1e-4)
    assert placement["placement_source"] == want["placement_source"]

    assert "Rz(" in info["transform"]
    assert "offset_mm" in info["transform"]
    assert "bottom_z_mm" in info["transform"]
    assert info["sha256"] == hashlib.sha256(files.step_path.read_bytes()).hexdigest()
    assert info["sha256"] == hashlib.sha256(box_step(name)).hexdigest()

    low, high = expected_placed_bbox(name, BOX_HEIGHT_MM)
    centre, zmin = RAW_PLACEMENT[name]
    sx, sy = want["native_size_mm"]
    raw_box = info["bbox_mm"]["raw"]
    assert raw_box["min"] == pytest.approx([centre[0] - sx / 2, centre[1] - sy / 2, zmin], abs=1e-5)
    assert raw_box["max"] == pytest.approx(
        [centre[0] + sx / 2, centre[1] + sy / 2, zmin + BOX_HEIGHT_MM], abs=1e-5
    )
    assert info["bbox_mm"]["placed"]["min"] == pytest.approx(list(low), abs=1e-5)
    assert info["bbox_mm"]["placed"]["max"] == pytest.approx(list(high), abs=1e-5)

    assert info["source"]["name"] == SOURCE_NAME
    assert info["source"]["links"] == list(SOURCE_LINKS)
    assert info["source"]["notice"] == "Personal use only; copyright belongs to JLCEDA/EasyEDA."
    assert isinstance(info["warnings"], list)


def test_info_json_keeps_non_ascii_text_as_utf8(tmp_path: Path) -> None:
    doc = load_doc("box3d_rot90")
    doc["result"]["dataStr"]["head"]["c_para"]["Manufacturer Part"] = "SYN-µC-テスト"
    client = StubClient(docs={IDS["box3d_rot90"]: doc})
    (files,) = convert_parts(["C9000016"], tmp_path, client=client, with_3d=True).models
    raw = files.info_path.read_bytes()
    assert "テスト".encode() in raw
    assert json.loads(raw.decode("utf-8"))["mpn"].endswith("テスト")


def test_stale_origin_warning_lands_in_info_json_and_result(tmp_path: Path) -> None:
    result = convert_parts(["C9000018"], tmp_path, client=StubClient(), with_3d=True)
    (files,) = result.models
    info = json.loads(files.info_path.read_text(encoding="utf-8"))
    assert [w for w in info["warnings"] if "c_origin is stale" in w]
    assert all(w.startswith(MODEL_WARNING_PREFIX) for w in info["warnings"])
    assert [w for w in result.warnings if "c_origin is stale" in w] == [
        w for w in result.warnings if w.startswith("C9000018: 3D model: c_origin is stale")
    ]
    assert len([w for w in result.warnings if "c_origin is stale" in w]) == 1


def test_wrong_model_size_is_reported_but_still_written(tmp_path: Path) -> None:
    models = {model_uuid("box3d_rot90"): make_box_step(size=(5.0, 5.0, 1.0))}
    result = convert_parts(["C9000016"], tmp_path, client=StubClient(models), with_3d=True)
    (files,) = result.models
    assert any("differs from the expected" in w for w in files.warnings)
    assert [w for w in result.warnings if "differs from the expected" in w] == [
        f"C9000016: {w}" for w in files.warnings if "differs from the expected" in w
    ]
    info = json.loads(files.info_path.read_text(encoding="utf-8"))
    assert [w for w in info["warnings"] if "differs from the expected" in w]


def test_ids_are_normalised_for_file_names(tmp_path: Path) -> None:
    result = convert_parts([" c9000016 "], tmp_path, client=StubClient(), with_3d=True)
    (files,) = result.models
    assert files.lcsc_id == "C9000016"
    assert files.step_path.name == "C9000016.step"


def test_file_names_never_come_from_a_path_like_id(tmp_path: Path) -> None:
    out = tmp_path / "out"
    doc = load_doc("box3d_rot90")
    client = StubClient(docs={"../EVIL": doc})
    # A lenient client could serve it; the conversion still refuses to name files after it.
    with pytest.raises(PartNotFoundError):
        convert_parts(["../evil"], out, client=client, with_3d=True)
    assert listing(tmp_path) in ([], ["out"])
    assert not out.exists() or list(out.iterdir()) == []


def test_output_directory_is_created(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "er"
    result = convert_parts(["C9000016"], out, client=StubClient(), with_3d=True)
    assert len(result.models) == 1
    assert (out / "C9000016.placed.step").is_file()


def test_a_second_run_overwrites_the_files(tmp_path: Path) -> None:
    convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    (tmp_path / "C9000016.placed.step").write_bytes(b"stale")
    convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    assert (tmp_path / "C9000016.placed.step").read_bytes() != b"stale"
    assert not [n for n in listing(tmp_path) if n.startswith(".tmp-")]


# .lbr


def test_lbr_gets_the_model3d_step_attribute(tmp_path: Path) -> None:
    result = convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    assert lbr_attributes(result.path, "MODEL3D_STEP") == ["C9000016.placed.step"]
    text = result.path.read_text(encoding="utf-8")
    assert [i for i in lint_library(text) if i.severity == "error"] == []
    assert dict(result.parts[0].attributes)["MODEL3D_STEP"] == "C9000016.placed.step"


def test_lbr_is_unchanged_apart_from_the_attribute(tmp_path: Path) -> None:
    plain = convert_parts(["C9000016"], tmp_path / "a", client=StubClient())
    with_3d = convert_parts(["C9000016"], tmp_path / "b", client=StubClient(), with_3d=True)
    plain_text = plain.path.read_text(encoding="utf-8")
    full_text = with_3d.path.read_text(encoding="utf-8")
    assert "MODEL3D_STEP" not in plain_text
    stripped = re.sub(r'[ \t]*<attribute name="MODEL3D_STEP"[^>]*/>\n', "", full_text)
    assert stripped == plain_text
    assert stripped != full_text


def test_without_3d_nothing_is_downloaded_or_written(tmp_path: Path) -> None:
    client = StubClient()
    result = convert_parts(["C9000016"], tmp_path, client=client)
    assert client.model_calls == []
    assert result.models == ()
    assert result.model_warnings == ()
    assert result.with_3d is False
    assert listing(tmp_path) == ["C9000016.lbr"]


def test_several_parts_each_get_their_own_files_and_attribute(tmp_path: Path) -> None:
    ids = ["C9000016", "C9000004", "C9000017", "C9000018"]
    result = convert_parts(ids, tmp_path, client=StubClient(), library_name="mylib", with_3d=True)
    assert [m.lcsc_id for m in result.models] == ["C9000016", "C9000017", "C9000018"]
    assert lbr_attributes(result.path, "MODEL3D_STEP") == [
        "C9000016.placed.step",
        "C9000017.placed.step",
        "C9000018.placed.step",
    ]
    assert listing(tmp_path) == sorted(
        ["mylib.lbr"]
        + [f"{i}{suffix}" for i in ids if i != "C9000004" for suffix in (".step", ".placed.step")]
        + [f"{i}.3d.json" for i in ids if i != "C9000004"]
    )
    assert "C9000004: no 3D model available" in result.warnings


# Failure isolation


def only_library(directory: Path, name: str = "C9000016") -> None:
    assert listing(directory) == [f"{name}.lbr"]


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        (ModelError("3D model abc not found on EasyEDA (HTTP 404)."), "not found"),
        (RateLimitedError("modules.easyeda.com blocked the request (HTTP 403)."), "blocked"),
        (NetworkError("Cannot reach modules.easyeda.com: down"), "Cannot reach"),
    ],
    ids=["model-error", "rate-limited", "network"],
)
def test_a_failed_download_never_fails_the_library(
    tmp_path: Path, failure: Exception, fragment: str
) -> None:
    client = StubClient({model_uuid("box3d_rot90"): failure})
    result = convert_parts(["C9000016"], tmp_path, client=client, with_3d=True)

    only_library(tmp_path)
    assert result.models == ()
    assert lbr_attributes(result.path, "MODEL3D_STEP") == []
    assert [i for i in lint_library(result.path.read_text("utf-8")) if i.severity == "error"] == []
    (warning,) = [w for w in result.warnings if "3D model not written" in w]
    assert warning.startswith("C9000016: 3D model not written: ")
    assert fragment in warning
    assert warning in result.model_warnings


def test_the_failure_is_isolated_per_part(tmp_path: Path) -> None:
    models: dict[str, bytes | Exception] = {
        model_uuid("box3d_rot90"): RateLimitedError("blocked"),
        model_uuid("box3d_z"): box_step("box3d_z"),
    }
    result = convert_parts(
        ["C9000016", "C9000017"],
        tmp_path,
        client=StubClient(models),
        library_name="two",
        with_3d=True,
    )
    assert [m.lcsc_id for m in result.models] == ["C9000017"]
    assert lbr_attributes(result.path, "MODEL3D_STEP") == ["C9000017.placed.step"]
    assert listing(tmp_path) == sorted(
        ["two.lbr", "C9000017.step", "C9000017.placed.step", "C9000017.3d.json"]
    )
    assert [w for w in result.warnings if "not written" in w] == [
        "C9000016: 3D model not written: blocked"
    ]


@pytest.mark.parametrize(
    "body",
    [b"<html><body>Access denied</body></html>" + b" " * 2000, make_assembly_step(), b""],
    ids=["html", "assembly", "empty"],
)
def test_an_unusable_model_body_is_a_warning_not_an_error(tmp_path: Path, body: bytes) -> None:
    client = StubClient({model_uuid("box3d_rot90"): body})
    result = convert_parts(["C9000016"], tmp_path, client=client, with_3d=True)
    only_library(tmp_path)
    assert result.models == ()
    assert [w for w in result.warnings if w.startswith("C9000016: 3D model not written: ")]


def test_a_model_the_server_does_not_have_is_a_warning(tmp_path: Path) -> None:
    result = convert_parts(["C9000016"], tmp_path, client=StubClient({}), with_3d=True)
    only_library(tmp_path)
    assert "not found" in " ".join(result.warnings)


def test_part_without_svgnode_says_so(tmp_path: Path) -> None:
    client = StubClient()
    result = convert_parts(["C9000004"], tmp_path, client=client, with_3d=True)
    assert result.warnings == ("C9000004: no 3D model available",)
    assert client.model_calls == []
    assert result.models == ()
    only_library(tmp_path, "C9000004")
    assert lbr_attributes(result.path, "MODEL3D_STEP") == []


def test_part_with_a_broken_svgnode_has_no_model_and_explains_why(tmp_path: Path) -> None:
    doc = load_doc("r0603")
    shapes = doc["result"]["packageDetail"]["dataStr"]["shape"]
    shapes[:] = ["SVGNODE~{broken" if s.startswith("SVGNODE") else s for s in shapes]
    client = StubClient(docs={"C9000001": doc})
    on = convert_parts(["C9000001"], tmp_path / "on", client=client, with_3d=True)
    assert on.models == ()
    assert any(w.startswith("C9000001: 3D model: ignored") for w in on.warnings)
    assert "C9000001: no 3D model available" in on.warnings
    off = convert_parts(["C9000001"], tmp_path / "off", client=client)
    assert not [w for w in off.warnings if "3D" in w]


# Warning prefix filter


def test_model_warnings_from_the_parser_are_hidden_unless_3d_is_requested(tmp_path: Path) -> None:
    off = convert_parts(["C9000018"], tmp_path / "off", client=StubClient())
    on = convert_parts(["C9000018"], tmp_path / "on", client=StubClient(), with_3d=True)

    stale = [w for w in off.parts[0].warnings if w.startswith(MODEL_WARNING_PREFIX)]
    assert len(stale) == 1 and "stale" in stale[0]  # Part.warnings is left untouched
    assert off.warnings == ()
    assert [w for w in on.warnings if "stale" in w] == [f"C9000018: {stale[0]}"]


def _part_with_warnings(*warnings: str) -> Part:
    import dataclasses

    part = parse_part(load_doc("r0603"))
    return dataclasses.replace(part, warnings=warnings)


def test_the_prefix_filter_leaves_other_warnings_alone(tmp_path: Path) -> None:
    part = _part_with_warnings(
        "pad 3 skipped", f"{MODEL_WARNING_PREFIX}c_origin is stale", "3D models are cool"
    )
    off = ConversionResult(path=tmp_path / "x.lbr", parts=(part,))
    on = ConversionResult(path=tmp_path / "x.lbr", parts=(part,), with_3d=True)
    assert off.warnings == ("C9000001: pad 3 skipped", "C9000001: 3D models are cool")
    assert on.warnings == (
        "C9000001: pad 3 skipped",
        "C9000001: 3D model: c_origin is stale",
        "C9000001: 3D models are cool",
    )
    assert part.warnings[1] == "3D model: c_origin is stale"


def test_result_warnings_order_part_model_then_failures(tmp_path: Path) -> None:
    part = _part_with_warnings("symbol note")
    result = ConversionResult(
        path=tmp_path / "x.lbr",
        parts=(part,),
        model_warnings=("C9000001: 3D model not written: boom",),
        with_3d=True,
    )
    assert result.warnings == ("C9000001: symbol note", "C9000001: 3D model not written: boom")


def test_conversion_result_stays_constructible_the_old_way(tmp_path: Path) -> None:
    part = _part_with_warnings("note")
    result = ConversionResult(tmp_path / "a.lbr", (part,))
    assert result.models == ()
    assert result.warnings == ("C9000001: note",)


# Atomic writes


class FailOnNthCall:
    """Wraps a function and raises OSError from the n-th call on; counts calls."""

    def __init__(self, real: Callable[..., Any], n: int) -> None:
        self.real = real
        self.n = n
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls >= self.n:
            raise OSError(28, "No space left on device")
        return self.real(*args, **kwargs)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_failure_while_moving_files_into_place_leaves_no_partial_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    monkeypatch.setattr(service.os, "replace", FailOnNthCall(os.replace, n))
    result = convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    only_library(tmp_path)  # no 3D file, no temp file
    assert result.models == ()
    assert lbr_attributes(result.path, "MODEL3D_STEP") == []
    (warning,) = [w for w in result.warnings if "3D model not written" in w]
    assert "No space left" in warning


@pytest.mark.parametrize("n", [1, 2, 3])
def test_failure_while_staging_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    monkeypatch.setattr(service.tempfile, "mkstemp", FailOnNthCall(service.tempfile.mkstemp, n))
    result = convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    only_library(tmp_path)
    assert result.models == ()
    assert [w for w in result.warnings if "3D model not written" in w]


def test_failure_while_writing_data_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write_bytes = Path.write_bytes
    failing = FailOnNthCall(lambda path, data: real_write_bytes(path, data), 2)
    monkeypatch.setattr(Path, "write_bytes", lambda path, data: failing(path, data))
    result = convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    only_library(tmp_path)
    assert result.models == ()


def test_a_failed_run_does_not_touch_files_of_an_earlier_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for suffix in (".step", ".placed.step", ".3d.json"):
        (tmp_path / f"C9000016{suffix}").write_bytes(b"OLD" + suffix.encode())
    monkeypatch.setattr(service.tempfile, "mkstemp", FailOnNthCall(service.tempfile.mkstemp, 3))
    convert_parts(["C9000016"], tmp_path, client=StubClient(), with_3d=True)
    for suffix in (".step", ".placed.step", ".3d.json"):
        assert (tmp_path / f"C9000016{suffix}").read_bytes() == b"OLD" + suffix.encode()
    assert not [n for n in listing(tmp_path) if n.startswith(".tmp-")]


def test_no_temp_files_remain_after_success(tmp_path: Path) -> None:
    convert_parts(["C9000016", "C9000017"], tmp_path, client=StubClient(), with_3d=True)
    assert not [n for n in listing(tmp_path) if n.startswith(".tmp-")]


def test_invalid_model_data_never_creates_any_3d_file(tmp_path: Path) -> None:
    """Placement is computed completely before the first byte is written."""
    client = StubClient({model_uuid("box3d_rot90"): make_assembly_step()})
    convert_parts(["C9000016"], tmp_path, client=client, with_3d=True)
    only_library(tmp_path)


# Add-in contract


def test_core_part_service_reads_the_models_from_the_result(tmp_path: Path) -> None:
    from core_service import CorePartService

    service_ = CorePartService(client=StubClient())  # type: ignore[arg-type]
    added = service_.add_part("C9000016", tmp_path, with_3d=True)
    assert added.step_path == tmp_path / "C9000016.step"
    assert added.placed_step_path == tmp_path / "C9000016.placed.step"
    assert added.step_path.is_file() and added.placed_step_path.is_file()

    plain = service_.add_part("C9000017", tmp_path / "plain")
    assert plain.step_path is None and plain.placed_step_path is None
    assert not any(p.suffix == ".step" for p in (tmp_path / "plain").iterdir())

    missing = service_.add_part("C9000004", tmp_path / "missing", with_3d=True)
    assert missing.step_path is None
    assert "C9000004: no 3D model available" in missing.warnings


# Real data (local only)

REAL_CASES = ("C25804", "C165948")
REAL_READY = all(
    (REAL / f"{i}.json").is_file() and (REAL / f"{i}.step").is_file() for i in REAL_CASES
)
real_only = pytest.mark.skipif(
    not REAL_READY, reason="needs tests/fixtures/real/C25804.* and C165948.* (json and step)"
)


class RealClient:
    """Serves the local real JSON and STEP files, keyed the way the service asks."""

    def __init__(self) -> None:
        self.model_files: dict[str, Path] = {}

    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        doc = json.loads((REAL / f"{lcsc_id}.json").read_text(encoding="utf-8"))
        part = parse_part(doc)
        assert part.model3d is not None
        self.model_files[part.model3d.uuid] = REAL / f"{lcsc_id}.step"
        assert isinstance(doc, dict)
        return doc

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        return ""

    def fetch_model_step(self, uuid: str) -> bytes:
        return self.model_files[uuid].read_bytes()


@real_only
@pytest.mark.real
@pytest.mark.parametrize("lcsc_id", REAL_CASES)
def test_real_placed_model_sits_on_the_footprint(tmp_path: Path, lcsc_id: str) -> None:
    result = convert_parts([lcsc_id], tmp_path, client=RealClient(), with_3d=True)  # type: ignore[arg-type]
    model = result.parts[0].model3d
    assert model is not None
    (files,) = result.models

    assert files.step_path.read_bytes() == (REAL / f"{lcsc_id}.step").read_bytes()
    placed = step_bbox(files.placed_step_path.read_bytes())
    raw = step_bbox(files.step_path.read_bytes())

    cx, cy = placed.center_xy
    assert cx == pytest.approx(model.offset_mm[0], abs=0.05)
    assert cy == pytest.approx(model.offset_mm[1], abs=0.05)
    assert placed.min_z == pytest.approx(model.bottom_z_mm, abs=0.05)
    # Rotation by 90 or 270 degrees swaps the sides, 0 and 180 keep them.
    swapped = round(model.rotation_deg[2] / 90) % 2 == 1
    expected_size = raw.size_xy[::-1] if swapped else raw.size_xy
    assert placed.size_xy == pytest.approx(expected_size, abs=0.01)
    assert placed.size[2] == pytest.approx(raw.size[2], abs=0.01)
    assert result.models[0].warnings == ()

    info = json.loads(files.info_path.read_text(encoding="utf-8"))
    assert info["placement"]["offset_mm"] == pytest.approx(list(model.offset_mm), abs=1e-6)
    assert info["sha256"] == hashlib.sha256((REAL / f"{lcsc_id}.step").read_bytes()).hexdigest()
    assert lbr_attributes(result.path, "MODEL3D_STEP") == [f"{lcsc_id}.placed.step"]


@real_only
@pytest.mark.real
def test_real_resistor_model_is_centred_between_its_pads(tmp_path: Path) -> None:
    result = convert_parts(["C25804"], tmp_path, client=RealClient(), with_3d=True)  # type: ignore[arg-type]
    pads = result.parts[0].package.pads
    pad_cx = (min(p.x - p.width / 2 for p in pads) + max(p.x + p.width / 2 for p in pads)) / 2
    pad_cy = (min(p.y - p.height / 2 for p in pads) + max(p.y + p.height / 2 for p in pads)) / 2
    placed = step_bbox(result.models[0].placed_step_path.read_bytes())
    assert placed.center_xy == pytest.approx((pad_cx, pad_cy), abs=0.05)
    assert placed.min_z == pytest.approx(0.0, abs=0.05)
