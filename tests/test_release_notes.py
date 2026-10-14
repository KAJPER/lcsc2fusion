"""scripts/release_notes.py: the changelog section of a version plus the fixed footer."""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import lcsc2fusion

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_notes.py"
CHANGELOG = ROOT / "CHANGELOG.md"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

SAMPLE = """\
# Changelog

Intro text that belongs to no version.

## [Unreleased]

### Added

- Something not released yet.

## [0.3.0] - 2026-12-01

### Fixed

- Third thing.

## [0.2.0-rc1] - 2026-11-01

### Added

- Release candidate thing.

## [0.2.0] - 2026-11-10

### Added

- Second thing.

```
## [9.9.9] - not a heading, it is inside a code fence
```

## [0.1.0] - 2026-10-14

First release.

### Added

- First thing.

[Unreleased]: https://example.invalid/compare/v0.3.0...HEAD
[0.3.0]: https://example.invalid/releases/tag/v0.3.0
[0.2.0-rc1]: https://example.invalid/releases/tag/v0.2.0-rc1
[0.2.0]: https://example.invalid/releases/tag/v0.2.0
[0.1.0]: https://example.invalid/releases/tag/v0.1.0
"""


@pytest.fixture(scope="module")
def notes() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("release_notes_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


# Version arguments ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v0.1.0", "0.1.0"),
        ("0.1.0", "0.1.0"),
        ("V1.2.3", "1.2.3"),
        ("  v0.2.0-rc1 ", "0.2.0-rc1"),
        ("v10.20.30-beta.2", "10.20.30-beta.2"),
        ("v1.0.0+build.5", "1.0.0+build.5"),
    ],
)
def test_normalise_version_strips_the_v(notes: ModuleType, raw: str, expected: str) -> None:
    assert notes.normalise_version(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "v", "main", "Unreleased", "1.2", "v1.2.x", "vv1.2.3", "1.2.3.4", "v1.2.3-", "../x"]
)
def test_normalise_version_rejects_what_is_not_a_version(notes: ModuleType, raw: str) -> None:
    with pytest.raises(notes.ReleaseNotesError):
        notes.normalise_version(raw)


def test_prerelease_means_a_hyphen_after_the_numbers(notes: ModuleType) -> None:
    assert notes.is_prerelease("0.2.0-rc1")
    assert notes.is_prerelease("1.0.0-beta.2+build.5")
    assert not notes.is_prerelease("0.2.0")
    assert not notes.is_prerelease("1.0.0+build-5")


# Section extraction -----------------------------------------------------------------------------


def test_first_section_stops_at_the_next_heading(notes: ModuleType) -> None:
    text = notes.extract_section(SAMPLE, "v0.3.0")
    assert text == "### Fixed\n\n- Third thing."


def test_last_section_runs_to_the_end_without_the_link_block(notes: ModuleType) -> None:
    text = notes.extract_section(SAMPLE, "0.1.0")
    assert text.startswith("First release.")
    assert text.endswith("- First thing.")
    assert "example.invalid" not in text
    assert "[0.1.0]:" not in text


def test_the_link_block_is_never_part_of_a_section(notes: ModuleType) -> None:
    for version in ("0.3.0", "0.2.0-rc1", "0.2.0", "0.1.0"):
        text = notes.extract_section(SAMPLE, version)
        assert not re.search(r"^\[[^\]]+\]:\s*\S+", text, flags=re.MULTILINE), version


def test_unreleased_is_ignored_and_cannot_be_requested(notes: ModuleType) -> None:
    for version in ("0.3.0", "0.2.0", "0.1.0"):
        assert "not released yet" not in notes.extract_section(SAMPLE, version)
    with pytest.raises(notes.ReleaseNotesError):
        notes.extract_section(SAMPLE, "Unreleased")


def test_a_final_version_does_not_match_its_release_candidate(notes: ModuleType) -> None:
    assert "Release candidate thing" in notes.extract_section(SAMPLE, "v0.2.0-rc1")
    assert "Release candidate thing" not in notes.extract_section(SAMPLE, "v0.2.0")
    assert "Second thing" in notes.extract_section(SAMPLE, "v0.2.0")
    assert "Second thing" not in notes.extract_section(SAMPLE, "v0.2.0-rc1")


def test_a_heading_inside_a_code_fence_is_not_a_section(notes: ModuleType) -> None:
    with pytest.raises(notes.ReleaseNotesError, match="no section"):
        notes.extract_section(SAMPLE, "9.9.9")
    # the fence stays in the section it belongs to
    assert "inside a code fence" in notes.extract_section(SAMPLE, "0.2.0")


def test_missing_version_names_the_fix(notes: ModuleType) -> None:
    with pytest.raises(notes.ReleaseNotesError) as error:
        notes.extract_section(SAMPLE, "v4.0.0")
    assert "## [4.0.0]" in str(error.value)
    assert "CHANGELOG.md" in str(error.value)


def test_a_changelog_without_any_section_fails(notes: ModuleType) -> None:
    with pytest.raises(notes.ReleaseNotesError):
        notes.extract_section("# Changelog\n\nNothing yet.\n", "0.1.0")
    with pytest.raises(notes.ReleaseNotesError):
        notes.extract_section("", "0.1.0")


def test_an_empty_section_fails(notes: ModuleType) -> None:
    text = "## [0.2.0] - 2026-01-01\n\n## [0.1.0] - 2025-01-01\n\n- Old.\n"
    with pytest.raises(notes.ReleaseNotesError, match="empty"):
        notes.extract_section(text, "0.2.0")
    assert notes.extract_section(text, "0.1.0") == "- Old."


def test_a_section_with_only_links_is_empty(notes: ModuleType) -> None:
    text = "## [0.1.0] - 2025-01-01\n\n[0.1.0]: https://example.invalid/x\n"
    with pytest.raises(notes.ReleaseNotesError, match="empty"):
        notes.extract_section(text, "0.1.0")


def test_a_duplicate_section_is_refused(notes: ModuleType) -> None:
    text = "## [0.1.0] - a\n\n- One.\n\n## [0.1.0] - b\n\n- Two.\n"
    with pytest.raises(notes.ReleaseNotesError, match="2 sections"):
        notes.extract_section(text, "0.1.0")


def test_a_v_in_the_heading_is_tolerated(notes: ModuleType) -> None:
    assert notes.extract_section("## [v0.1.0] - 2025-01-01\n\n- One.\n", "0.1.0") == "- One."


def test_text_between_a_section_and_a_link_in_the_middle_is_kept(notes: ModuleType) -> None:
    text = "## [0.2.0]\n\n- See [the docs][docs].\n\n[docs]: https://example.invalid/d\n\n- More.\n"
    section = notes.extract_section(text, "0.2.0")
    assert "[docs]: https://example.invalid/d" in section
    assert section.endswith("- More.")


def test_crlf_input_gives_the_same_section(notes: ModuleType) -> None:
    crlf = SAMPLE.replace("\n", "\r\n")
    for version in ("0.3.0", "0.2.0-rc1", "0.2.0", "0.1.0"):
        assert notes.extract_section(crlf, version) == notes.extract_section(SAMPLE, version)
        assert "\r" not in notes.extract_section(crlf, version)
    assert notes.build_notes(crlf, "v0.1.0") == notes.build_notes(SAMPLE, "v0.1.0")


def test_old_mac_line_breaks_and_a_bom_are_handled(notes: ModuleType) -> None:
    text = "﻿" + SAMPLE.replace("\n", "\r")
    assert notes.extract_section(text, "0.3.0") == "### Fixed\n\n- Third thing."


# Whole notes ------------------------------------------------------------------------------------


def test_build_notes_is_the_section_then_the_footer(notes: ModuleType) -> None:
    text = notes.build_notes(SAMPLE, "v0.2.0")
    section = notes.extract_section(SAMPLE, "0.2.0")
    assert text.startswith(section + "\n\n## Downloads\n")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text


def test_footer_names_the_assets_of_the_release(notes: ModuleType) -> None:
    text = notes.footer("1.2.3")
    for name in (
        "LCSC2Fusion-Setup-1.2.3.exe",
        "LCSC2Fusion-1.2.3-installer.zip",
        "LCSC2Fusion-1.2.3-addin.zip",
        "SHA256SUMS.txt",
        "Install-LCSC2Fusion.bat",
        "Uninstall-LCSC2Fusion.bat",
        r"%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion",
        "Get-FileHash",
        "SmartScreen",
        "More info",
        "Run anyway",
        "Shift+S",
        "not affiliated with Autodesk, EasyEDA, JLCPCB or LCSC",
        "personal use",
    ):
        assert name in text, name


def test_footer_names_match_the_checksum_script(notes: ModuleType) -> None:
    spec = importlib.util.spec_from_file_location(
        "verify_checksums_for_notes", ROOT / "scripts" / "verify_checksums.py"
    )
    assert spec is not None and spec.loader is not None
    verify = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = verify
    try:
        spec.loader.exec_module(verify)
    finally:
        sys.modules.pop(spec.name, None)
    text = notes.footer("0.9.9")
    for name in verify.expected_assets("0.9.9"):
        assert f"`{name}`" in text


def test_footer_marks_a_prerelease_only_when_it_is_one(notes: ModuleType) -> None:
    assert notes.build_notes(SAMPLE, "v0.2.0-rc1").startswith("### Added")
    assert "This is a pre-release" in notes.footer("0.2.0-rc1")
    assert "This is a pre-release" not in notes.footer("0.2.0")


def test_footer_has_no_long_dashes_and_no_double_hyphens_in_prose(notes: ModuleType) -> None:
    text = notes.footer("1.2.3")
    assert EM_DASH not in text and EN_DASH not in text
    assert " -- " not in re.sub(r"```.*?```", "", text, flags=re.DOTALL)


# Command line -----------------------------------------------------------------------------------


def write_changelog(tmp_path: Path, text: str = SAMPLE, *, crlf: bool = False) -> Path:
    path = tmp_path / "CHANGELOG.md"
    data = text.replace("\n", "\r\n") if crlf else text
    path.write_bytes(data.encode("utf-8"))
    return path


def test_cli_prints_the_notes(
    notes: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = write_changelog(tmp_path)
    assert notes.main(["v0.2.0", "--changelog", str(changelog)]) == 0
    captured = capsys.readouterr()
    assert captured.out == notes.build_notes(SAMPLE, "0.2.0")
    assert captured.err == ""


def test_cli_writes_a_file_with_lf_and_utf8(notes: ModuleType, tmp_path: Path) -> None:
    changelog = write_changelog(
        tmp_path, SAMPLE + "\n## [0.0.1]\n\n- Zażółć gęślą jaźń.\n", crlf=True
    )
    out = tmp_path / "deep" / "folder" / "notes.md"
    assert notes.main(["v0.0.1", "--changelog", str(changelog), "--out", str(out)]) == 0
    data = out.read_bytes()
    assert b"\r" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert "Zażółć gęślą jaźń." in data.decode("utf-8")


def test_cli_without_the_version_fails_with_a_message(
    notes: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = write_changelog(tmp_path)
    out = tmp_path / "notes.md"
    assert notes.main(["v7.7.7", "--changelog", str(changelog), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "7.7.7" in captured.err and captured.err.startswith("release_notes:")
    assert not out.exists()


def test_cli_with_a_missing_changelog_fails_with_a_message(
    notes: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert notes.main(["v0.1.0", "--changelog", str(tmp_path / "none.md")]) == 1
    assert "Cannot read" in capsys.readouterr().err


def test_cli_with_a_bad_version_fails_with_a_message(
    notes: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = write_changelog(tmp_path)
    assert notes.main(["Unreleased", "--changelog", str(changelog)]) == 1
    assert "not a version" in capsys.readouterr().err


def test_cli_handles_a_prerelease_tag(notes: ModuleType, tmp_path: Path) -> None:
    changelog = write_changelog(tmp_path)
    out = tmp_path / "notes.md"
    assert notes.main(["v0.2.0-rc1", "--changelog", str(changelog), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Release candidate thing" in text
    assert "LCSC2Fusion-Setup-0.2.0-rc1.exe" in text


# The real changelog -----------------------------------------------------------------------------


def test_the_real_changelog_has_a_section_for_the_current_version(notes: ModuleType) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    body = notes.build_notes(text, f"v{lcsc2fusion.__version__}")
    assert lcsc2fusion.__version__ in body
    assert "[Unreleased]:" not in body


def test_the_real_changelog_is_well_formed(notes: ModuleType) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    names = [name for _, name in notes._headings(notes._lines(text))]
    assert names[0] == "Unreleased"
    assert len(names) == len(set(names))
    for name in names:
        assert re.search(
            rf"^\[{re.escape(name)}\]: https://github\.com/KAJPER/lcsc2fusion/", text, re.M
        )
    assert "[Unreleased]: https://github.com/KAJPER/lcsc2fusion/compare/v" in text
    assert EM_DASH not in text and EN_DASH not in text
    assert " -- " not in re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def test_every_released_section_of_the_real_changelog_builds(notes: ModuleType) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    for _, name in notes._headings(notes._lines(text)):
        if name != "Unreleased":
            assert notes.build_notes(text, name).strip()
