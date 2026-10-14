"""scripts/verify_checksums.py: a release folder must agree with its SHA256SUMS.txt."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_checksums.py"
VERSION = "1.2.3"
NAMES = [
    f"LCSC2Fusion-{VERSION}-addin.zip",
    f"LCSC2Fusion-{VERSION}-installer.zip",
    f"LCSC2Fusion-Setup-{VERSION}.exe",
]


@pytest.fixture(scope="module")
def verify() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("verify_checksums_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_release(
    folder: Path, names: list[str] | None = None, *, binary_marker: bool = False
) -> None:
    """A release folder with one small file per name and a matching SHA256SUMS.txt."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in names if names is not None else NAMES:
        data = f"content of {name}".encode()
        (folder / name).write_bytes(data)
        lines.append(f"{sha(data)} {'*' if binary_marker else ' '}{name}")
    (folder / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def test_a_consistent_folder_passes(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    assert verify.verify(tmp_path) == []
    assert verify.verify(tmp_path, VERSION) == []


def test_the_binary_marker_is_accepted(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path, binary_marker=True)
    assert verify.verify(tmp_path, VERSION) == []


def test_crlf_line_ends_and_upper_case_digests_are_accepted(
    verify: ModuleType, tmp_path: Path
) -> None:
    make_release(tmp_path)
    sums = tmp_path / "SHA256SUMS.txt"
    swapped = [
        line[:64].upper() + line[64:] for line in sums.read_text(encoding="utf-8").splitlines()
    ]
    sums.write_bytes(("\r\n".join(swapped) + "\r\n").encode("utf-8"))
    assert verify.verify(tmp_path, VERSION) == []


def test_a_byte_order_mark_is_accepted(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_bytes(b"\xef\xbb\xbf" + sums.read_bytes())
    assert verify.verify(tmp_path, VERSION) == []


def test_a_changed_file_is_reported(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    (tmp_path / NAMES[0]).write_bytes(b"tampered")
    problems = verify.verify(tmp_path)
    assert problems == [f"{NAMES[0]}: checksum does not match SHA256SUMS.txt"]


def test_a_listed_but_missing_file_is_reported(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    (tmp_path / NAMES[1]).unlink()
    assert verify.verify(tmp_path) == [f"{NAMES[1]} is listed in SHA256SUMS.txt but missing"]


def test_a_file_that_is_not_listed_is_reported(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    (tmp_path / "extra.txt").write_text("x", encoding="utf-8")
    assert verify.verify(tmp_path) == ["extra.txt is not listed in SHA256SUMS.txt"]


def test_folders_inside_the_release_folder_are_ignored(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    (tmp_path / "sub").mkdir()
    assert verify.verify(tmp_path) == []


def test_missing_sums_file(verify: ModuleType, tmp_path: Path) -> None:
    (tmp_path / NAMES[0]).write_bytes(b"x")
    assert verify.verify(tmp_path) == [f"SHA256SUMS.txt is missing in {tmp_path}"]


def test_empty_sums_file(verify: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "SHA256SUMS.txt").write_text("\n\n", encoding="utf-8")
    assert verify.verify(tmp_path) == ["SHA256SUMS.txt lists no files"]


@pytest.mark.parametrize(
    "line",
    [
        "not a checksum line",
        "abc123  file.zip",
        "z" * 64 + "  file.zip",
        "a" * 64,
        "a" * 64 + "file.zip",
    ],
)
def test_malformed_lines_are_reported(verify: ModuleType, tmp_path: Path, line: str) -> None:
    make_release(tmp_path)
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(sums.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    problems = verify.verify(tmp_path)
    assert len(problems) == 1 and "line 4" in problems[0]


@pytest.mark.parametrize("name", ["../outside.zip", "sub/file.zip", "sub\\file.zip", ".."])
def test_names_with_a_path_are_refused(verify: ModuleType, tmp_path: Path, name: str) -> None:
    make_release(tmp_path)
    (tmp_path.parent / "outside.zip").write_bytes(b"x")
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(sums.read_text(encoding="utf-8") + f"{sha(b'x')}  {name}\n", encoding="utf-8")
    problems = verify.verify(tmp_path)
    assert problems == [f"SHA256SUMS.txt line 4: {name!r} is not a plain file name"]


def test_a_name_listed_twice_is_reported(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path)
    sums = tmp_path / "SHA256SUMS.txt"
    first = sums.read_text(encoding="utf-8").splitlines()[0]
    sums.write_text(sums.read_text(encoding="utf-8") + first + "\n", encoding="utf-8")
    assert verify.verify(tmp_path) == [f"SHA256SUMS.txt lists {NAMES[0]} twice"]


def test_file_names_with_spaces_work(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path, ["a file with spaces.zip"])
    assert verify.verify(tmp_path) == []


def test_the_expected_downloads_of_a_version_must_be_listed(
    verify: ModuleType, tmp_path: Path
) -> None:
    make_release(tmp_path, NAMES[:2])
    assert verify.verify(tmp_path) == []
    assert verify.verify(tmp_path, VERSION) == [
        f"expected download {NAMES[2]} is not in SHA256SUMS.txt"
    ]
    assert verify.verify(tmp_path, VERSION, with_exe=False) == []


def test_files_of_another_version_do_not_satisfy_the_expectation(
    verify: ModuleType, tmp_path: Path
) -> None:
    make_release(tmp_path, [name.replace(VERSION, "9.9.9") for name in NAMES])
    assert len(verify.verify(tmp_path, VERSION)) == 3


def test_expected_assets_follow_the_published_names(verify: ModuleType) -> None:
    assert verify.expected_assets("0.1.0") == [
        "LCSC2Fusion-0.1.0-addin.zip",
        "LCSC2Fusion-0.1.0-installer.zip",
        "LCSC2Fusion-Setup-0.1.0.exe",
    ]
    assert verify.expected_assets("0.1.0", with_exe=False) == [
        "LCSC2Fusion-0.1.0-addin.zip",
        "LCSC2Fusion-0.1.0-installer.zip",
    ]


def test_cli_exit_codes_and_messages(
    verify: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_release(tmp_path)
    assert verify.main([str(tmp_path), "--version", f"v{VERSION}"]) == 0
    assert "is consistent" in capsys.readouterr().out
    (tmp_path / NAMES[2]).write_bytes(b"tampered")
    assert verify.main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert NAMES[2] in captured.err and "checksum does not match" in captured.err
    assert verify.main([str(tmp_path / "nowhere")]) == 1
    assert "is not a folder" in capsys.readouterr().err


def test_cli_no_exe_flag(verify: ModuleType, tmp_path: Path) -> None:
    make_release(tmp_path, NAMES[:2])
    assert verify.main([str(tmp_path), "--version", VERSION]) == 1
    assert verify.main([str(tmp_path), "--version", VERSION, "--no-exe"]) == 0
