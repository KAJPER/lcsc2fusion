"""scripts/make_3d_test_pack.py (Gate 3 pack) with a stub client, no network."""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from test_service_3d import IDS, StubClient

from lcsc2fusion.step import step_bbox

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_3d_test_pack.py"
PART_ID = IDS["box3d_rot90"]
TOL = 1e-4


@pytest.fixture
def pack_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_3d_test_pack", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = StubClient()
    monkeypatch.setattr(
        module, "convert_parts", functools.partial(module.convert_parts, client=stub)
    )
    return module


def test_pack_contains_all_variants_for_each_part(pack_script: ModuleType, tmp_path: Path) -> None:
    files, _ = pack_script.build_pack([PART_ID], tmp_path)
    names = sorted(p.name for p in files)
    assert names == sorted(
        [
            f"{PART_ID}.lbr",
            f"{PART_ID}.step",
            f"{PART_ID}.placed.step",
            f"{PART_ID}.3d.json",
            f"{PART_ID}.placed_yup.step",
        ]
    )
    assert all(p.exists() for p in files)


def test_yup_variant_swaps_up_axis_from_z_to_y(pack_script: ModuleType, tmp_path: Path) -> None:
    pack_script.build_pack([PART_ID], tmp_path)
    placed = step_bbox((tmp_path / f"{PART_ID}.placed.step").read_bytes())
    yup = step_bbox((tmp_path / f"{PART_ID}.placed_yup.step").read_bytes())
    # (x, y, z) -> (x, z, -y)
    assert (yup.min_x, yup.max_x) == pytest.approx((placed.min_x, placed.max_x), abs=TOL)
    assert (yup.min_y, yup.max_y) == pytest.approx((placed.min_z, placed.max_z), abs=TOL)
    assert (yup.min_z, yup.max_z) == pytest.approx((-placed.max_y, -placed.min_y), abs=TOL)


def test_missing_model_is_a_warning_and_no_yup_file(
    pack_script: ModuleType, tmp_path: Path
) -> None:
    files, warnings = pack_script.build_pack([IDS["sot23"]], tmp_path)  # part without SVGNODE
    assert [p.suffix for p in files] == [".lbr"]
    assert any("no 3D model" in w for w in warnings)
    assert not list(tmp_path.glob("*.placed_yup.step"))


def test_main_prints_instructions_and_attribution(
    pack_script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pack_script.main([PART_ID.lower(), "-o", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Attach Existing 3D Model" in out
    assert "placed_yup.step" in out
    assert "JLCEDA/EasyEDA" in out
    assert (tmp_path / f"{PART_ID}.placed_yup.step").exists()


def test_main_reports_library_errors(
    pack_script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pack_script.main(["C1", "-o", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err
