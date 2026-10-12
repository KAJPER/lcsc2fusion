"""Tests for the add-in library registry: state file, name resolution, transform, .lbr scan."""

from __future__ import annotations

import dataclasses
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import library_registry as registry
import pytest
from library_registry import (
    LibraryState,
    PartLbrError,
    PartRecord,
    StateStore,
    default_state_path,
    forget_library,
    inspect_part_lbr,
    make_part_transform,
    resolve_package_name,
)

from lcsc2fusion.errors import ConversionError
from lcsc2fusion.lbr_writer import build_library, package_fingerprint, write_library
from lcsc2fusion.lbr_writer.writer import sanitize_name
from lcsc2fusion.model import Package, Pad, PadShape, Part
from lcsc2fusion.parser.part import parse_part

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
FP_A = "a" * 64
FP_B = "b1c2d3e4" + "0" * 56
FP_C = "b1c2d3ff" + "0" * 56  # shares the first 6 characters with FP_B


def load_part(name: str) -> Part:
    return parse_part(json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8")))


def record(lcsc_id: str = "C1", package: str = "PKG", fingerprint: str = FP_A) -> PartRecord:
    return PartRecord(lcsc_id, f"DEV-{lcsc_id}", package, fingerprint, 1700000000.5)


def state_with(*packages: tuple[str, str]) -> LibraryState:
    return LibraryState(packages=dict(packages))


# ----- default_state_path -----------------------------------------------------------------------


def test_default_state_path_windows_uses_localappdata() -> None:
    path = default_state_path({"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}, "win32", Path("H"))
    assert path == Path(r"C:\Users\x\AppData\Local") / "LCSC2Fusion" / "state.json"


def test_default_state_path_windows_falls_back_to_home() -> None:
    path = default_state_path({}, "win32", Path("H"))
    assert path == Path("H") / "AppData" / "Local" / "LCSC2Fusion" / "state.json"


def test_default_state_path_other_systems() -> None:
    mac = default_state_path({}, "darwin", Path("H"))
    assert mac == Path("H") / "Library" / "Application Support" / "LCSC2Fusion" / "state.json"
    linux = default_state_path({}, "linux", Path("H"))
    assert linux == Path("H") / ".local" / "state" / "lcsc2fusion" / "state.json"
    xdg = default_state_path({"XDG_STATE_HOME": "S"}, "linux", Path("H"))
    assert xdg == Path("S") / "lcsc2fusion" / "state.json"


def test_default_state_path_env_override_wins() -> None:
    path = default_state_path({registry.STATE_ENV_VAR: "X/y.json", "LOCALAPPDATA": "L"}, "win32")
    assert path == Path("X/y.json")


def test_default_state_path_reads_the_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(registry.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "L")
    assert default_state_path(platform="win32", home=Path("H")) == (
        Path("L") / "LCSC2Fusion" / "state.json"
    )


# ----- state value ------------------------------------------------------------------------------


def test_state_is_immutable_and_updates_return_copies() -> None:
    empty = LibraryState()
    updated = empty.with_library("id-1", "Lib").with_part(record("c1"), {"PKG": FP_A})
    assert empty == LibraryState()
    assert updated.library_file_id == "id-1"
    assert updated.library_name == "Lib"
    assert updated.has_part("C1")
    assert updated.has_part(" c1 ")  # ids are normalised
    assert not updated.has_part("C2")
    assert updated.packages == {"PKG": FP_A}
    with pytest.raises(dataclasses.FrozenInstanceError):
        updated.library_name = "x"  # type: ignore[misc]


def test_without_contents_forgets_everything() -> None:
    state = LibraryState("id", "Lib").with_part(record(), {"PKG": FP_A})
    assert state.without_contents() == LibraryState()


# ----- StateStore -------------------------------------------------------------------------------


def test_missing_file_is_an_empty_state_without_warnings(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "none" / "state.json")
    assert store.load() == LibraryState()
    assert store.warnings == ()


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "sub" / "state.json")  # the folder does not exist yet
    state = (
        LibraryState()
        .with_library("urn:adsk.wipprod:fs.file:vf.X", "LCSC2Fusion Library")
        .with_part(record("C1"), {"PKG": FP_A})
        .with_part(record("C2", "PKG_B1C2D3", FP_B), {"PKG_B1C2D3": FP_B})
    )
    store.save(state)
    assert StateStore(store.path).load() == state
    assert store.load() == state
    assert store.warnings == ()


def test_saved_file_is_plain_readable_json(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(LibraryState("id", "Lib").with_part(record("C1"), {"PKG": FP_A}))
    text = store.path.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    document = json.loads(text)
    assert document["version"] == registry.STATE_VERSION
    assert document["library_file_id"] == "id"
    assert document["parts"]["C1"] == {
        "lcsc_id": "C1",
        "deviceset": "DEV-C1",
        "package": "PKG",
        "fingerprint": FP_A,
        "added_at": 1700000000.5,
    }
    assert document["packages"] == {"PKG": FP_A}


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    for index in range(3):
        store.save(LibraryState().with_part(record(f"C{index}"), {}))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_failed_save_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state.json")
    old = LibraryState("old", "Lib")
    store.save(old)
    before = store.path.read_bytes()

    def broken_replace(src: object, dst: object) -> None:
        raise PermissionError("locked by another process")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(PermissionError):
        store.save(LibraryState("new", "Lib"))
    monkeypatch.undo()

    assert store.path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert store.load() == old


def test_save_raises_oserror_when_the_folder_cannot_be_made(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        StateStore(blocker / "state.json").save(LibraryState())


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "",
        "[1, 2, 3]",
        '"a string"',
        '{"version": 99}',
        '{"parts": {}}',
        '{"version": 1',  # truncated
    ],
)
def test_corrupt_file_gives_an_empty_state_and_a_warning(tmp_path: Path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    store = StateStore(path)
    assert store.load() == LibraryState()
    (warning,) = store.warnings
    assert str(path) in warning
    assert "empty registry" in warning


def test_a_file_that_is_not_utf8_is_corrupt_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe\x00garbage\x80")
    store = StateStore(path)
    assert store.load() == LibraryState()
    assert len(store.warnings) == 1


def test_a_directory_in_place_of_the_file_is_corrupt_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.mkdir()
    store = StateStore(path)
    assert store.load() == LibraryState()
    assert "cannot be read" in store.warnings[0]


def test_malformed_entries_are_dropped_and_the_rest_kept(tmp_path: Path) -> None:
    good = dataclasses.asdict(record("C1"))
    document = {
        "version": registry.STATE_VERSION,
        "library_file_id": "id",
        "library_name": "Lib",
        "parts": {
            "C1": good,
            "C2": {"lcsc_id": "C2"},  # incomplete
            "C3": "nonsense",
            "C4": {**good, "lcsc_id": "C4", "added_at": True},  # bool is not a time
        },
        "packages": {"PKG": FP_A, "BAD": 5, "": FP_A},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    store = StateStore(path)
    state = store.load()
    assert state.library_file_id == "id"
    assert list(state.parts) == ["C1"]
    assert state.packages == {"PKG": FP_A}
    assert len(store.warnings) == 5  # three bad records and two bad package entries


def test_load_resets_the_warnings_of_the_previous_load(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("garbage", encoding="utf-8")
    store = StateStore(path)
    store.load()
    assert store.warnings
    store.save(LibraryState())
    store.load()
    assert store.warnings == ()


def test_forget_library_clears_the_stored_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(LibraryState("id", "Lib").with_part(record(), {"PKG": FP_A}))
    forget_library(store)
    assert store.load() == LibraryState()


def test_state_ids_are_stored_normalised(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(LibraryState().with_part(record("c7"), {}))
    assert list(json.loads(store.path.read_text(encoding="utf-8"))["parts"]) == ["C7"]
    assert store.load().has_part("C7")


# ----- resolve_package_name ---------------------------------------------------------------------


def test_free_name_is_kept() -> None:
    assert resolve_package_name(LibraryState(), "SOIC-8", FP_A) == "SOIC-8"
    assert resolve_package_name(state_with(("OTHER", FP_B)), "SOIC-8", FP_A) == "SOIC-8"


def test_same_fingerprint_keeps_the_name() -> None:
    assert resolve_package_name(state_with(("SOIC-8", FP_A)), "SOIC-8", FP_A) == "SOIC-8"


def test_clash_appends_the_first_six_fingerprint_characters_in_upper_case() -> None:
    name = resolve_package_name(state_with(("SOIC-8", FP_A)), "SOIC-8", FP_B)
    assert name == "SOIC-8_B1C2D3"
    assert name == sanitize_name(name)  # the writer would not change it


def test_the_renamed_package_is_found_again_for_the_same_shape() -> None:
    state = state_with(("SOIC-8", FP_A), ("SOIC-8_B1C2D3", FP_B))
    assert resolve_package_name(state, "SOIC-8", FP_B) == "SOIC-8_B1C2D3"


def test_a_taken_suffix_name_gets_more_characters() -> None:
    state = state_with(("SOIC-8", FP_A), ("SOIC-8_B1C2D3", FP_B))
    name = resolve_package_name(state, "SOIC-8", FP_C)
    assert name == "SOIC-8_B1C2D3F"  # 7 characters tell FP_B and FP_C apart
    assert name.upper() == name


def test_names_are_compared_case_insensitively() -> None:
    assert resolve_package_name(state_with(("soic-8", FP_A)), "SOIC-8", FP_A) == "SOIC-8"
    assert resolve_package_name(state_with(("soic-8", FP_A)), "SOIC-8", FP_B) == "SOIC-8_B1C2D3"


def test_short_non_hash_fingerprints_still_resolve() -> None:
    state = state_with(("N", "ab"), ("N_AB", "cd"))
    name = resolve_package_name(state, "N", "ef")
    assert name.startswith("N_EF")
    assert name.upper() not in {"N", "N_AB"}


# ----- make_part_transform ----------------------------------------------------------------------


def test_transform_returns_the_very_same_part_when_nothing_clashes() -> None:
    part = load_part("soic8")
    assert make_part_transform(LibraryState())(part) is part
    known = state_with((part.package.name, package_fingerprint(part.package)))
    assert make_part_transform(known)(part) is part


def test_transform_renames_only_the_package_of_a_clashing_part() -> None:
    part = load_part("soic8")
    base = sanitize_name(part.package.name)
    clashing = state_with((base, FP_A))  # same name, other shape
    renamed = make_part_transform(clashing)(part)

    suffix = package_fingerprint(part.package)[:6].upper()
    assert renamed.package.name == f"{base}_{suffix}"
    # nothing else moved: undo the rename and the parts are equal
    assert (
        dataclasses.replace(
            renamed, package=dataclasses.replace(renamed.package, name=part.package.name)
        )
        == part
    )
    assert part.package.name != renamed.package.name  # the input is untouched


def test_transform_checks_the_name_the_writer_will_use() -> None:
    part = load_part("soic8")
    lower = dataclasses.replace(part, package=dataclasses.replace(part.package, name="soic-8"))
    renamed = make_part_transform(state_with(("SOIC-8", FP_A)))(lower)
    assert renamed.package.name.startswith("SOIC-8_")


def test_transform_makes_two_different_shapes_of_one_batch_distinct() -> None:
    first = load_part("soic8")
    moved = dataclasses.replace(
        first.package, pads=(dataclasses.replace(first.package.pads[0], x=9.0),)
    )
    second = dataclasses.replace(first, lcsc_id="C9", mpn="OTHER", package=moved)
    transform = make_part_transform(LibraryState())
    one, two = transform(first), transform(second)
    assert one.package.name == first.package.name
    assert two.package.name != one.package.name
    assert two.package.name.startswith(sanitize_name(first.package.name))
    # a repeat of the first shape keeps its name
    assert transform(first).package.name == first.package.name


def test_transform_leaves_a_package_the_writer_rejects_alone() -> None:
    part = load_part("soic8")
    bad = Package("BAD", "", pads=(Pad("1", "1", 0.0, 0.0, 0.0, 0.5, PadShape.RECT),))
    broken = dataclasses.replace(part, package=bad)
    assert make_part_transform(state_with(("BAD", FP_A)))(broken) is broken
    with pytest.raises(ConversionError):  # and the writer reports it, with the part id
        build_library([broken])


def test_renamed_package_is_what_ends_up_in_the_written_library() -> None:
    part = load_part("soic8")
    clashing = state_with((sanitize_name(part.package.name), FP_A))
    renamed = make_part_transform(clashing)(part)
    root = ET.fromstring(build_library([renamed]))
    (package,) = root.iter("package")
    assert package.get("name") == renamed.package.name
    (device,) = root.iter("device")
    assert device.get("package") == renamed.package.name


# ----- inspect_part_lbr -------------------------------------------------------------------------


def test_inspect_reads_deviceset_packages_and_symbols(tmp_path: Path) -> None:
    part = load_part("soic8")
    path = write_library(tmp_path / "C9000005.lbr", [part])
    info = inspect_part_lbr(path)
    assert info.deviceset == sanitize_name(part.mpn, part.lcsc_id)
    assert info.packages == {sanitize_name(part.package.name): package_fingerprint(part.package)}
    assert info.symbols == (info.deviceset,)


def test_inspect_accepts_a_string_path(tmp_path: Path) -> None:
    path = write_library(tmp_path / "x.lbr", [load_part("r0603")])
    assert inspect_part_lbr(str(path)).deviceset == inspect_part_lbr(path).deviceset


def test_inspect_of_a_multi_unit_part_lists_every_symbol(tmp_path: Path) -> None:
    part = load_part("opamp2")
    assert len(part.symbols) > 1
    info = inspect_part_lbr(write_library(tmp_path / "op.lbr", [part]))
    assert len(info.symbols) == len(part.symbols)
    assert len(set(info.symbols)) == len(info.symbols)


def test_inspect_uses_the_first_deviceset_of_a_library_with_several(tmp_path: Path) -> None:
    first, second = load_part("r0603"), load_part("soic8")
    info = inspect_part_lbr(write_library(tmp_path / "two.lbr", [first, second]))
    assert info.deviceset == sanitize_name(first.mpn, first.lcsc_id)
    assert len(info.packages) == 2


@pytest.mark.parametrize(
    "name",
    sorted(
        p.name.removesuffix(".json")
        for p in SYNTH.glob("*.json")
        if not p.name.endswith(".expected.json")
    ),
)
def test_inspect_fingerprints_agree_with_package_fingerprint_for_every_fixture(
    tmp_path: Path, name: str
) -> None:
    part = load_part(name)
    info = inspect_part_lbr(write_library(tmp_path / f"{name}.lbr", [part]))
    ((package_name, fingerprint),) = info.packages.items()
    assert package_name == sanitize_name(part.package.name, "PACKAGE")
    assert fingerprint == package_fingerprint(part.package)


def test_transform_and_inspect_agree_on_the_renamed_package(tmp_path: Path) -> None:
    part = load_part("tqfp32")
    base = sanitize_name(part.package.name)
    state = state_with((base, FP_A))
    renamed = make_part_transform(state)(part)
    info = inspect_part_lbr(write_library(tmp_path / "t.lbr", [renamed]))
    expected_name = resolve_package_name(state, base, package_fingerprint(part.package))
    assert info.packages == {expected_name: package_fingerprint(part.package)}


def test_inspect_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PartLbrError, match="does not exist"):
        inspect_part_lbr(tmp_path / "nope.lbr")


def test_inspect_not_xml(tmp_path: Path) -> None:
    path = tmp_path / "bad.lbr"
    path.write_text("this is not xml", encoding="utf-8")
    with pytest.raises(PartLbrError, match=r"bad.lbr"):
        inspect_part_lbr(path)


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<eagle/>", "no <library>"),
        ("<eagle><drawing><library/></drawing></eagle>", "no named <deviceset>"),
        (
            '<eagle><drawing><library><devicesets><deviceset name=""/></devicesets>'
            "</library></drawing></eagle>",
            "no named <deviceset>",
        ),
        (
            "<eagle><drawing><library><packages><package/></packages><devicesets>"
            '<deviceset name="D"/></devicesets></library></drawing></eagle>',
            "<package> without a name",
        ),
    ],
)
def test_inspect_rejects_files_that_are_not_part_libraries(
    tmp_path: Path, xml: str, message: str
) -> None:
    path = tmp_path / "odd.lbr"
    path.write_text(xml, encoding="utf-8")
    with pytest.raises(PartLbrError, match=message):
        inspect_part_lbr(path)
