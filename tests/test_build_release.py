"""Tests of scripts/build_release.py: versions, the release assets, and the end to end install.

Everything runs offline. The slow tests build the real release (including the IExpress setup exe on
Windows) and install it into a temp folder; `pytest -m "not slow"` skips them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from installer_support import (
    IS_WINDOWS,
    POWERSHELL,
    ROOT,
    WINDOWS_ONLY,
    Sandbox,
    load_build_release,
    run_script,
    tree_bytes,
)

from lcsc2fusion import __version__

VERSION = __version__
BOM = b"\xef\xbb\xbf"


@pytest.fixture(scope="module")
def br() -> ModuleType:
    return load_build_release()


@pytest.fixture(scope="module")
def build_addin(br: ModuleType) -> ModuleType:
    return br._load_build_addin()  # type: ignore[no-any-return]


def light_root(
    dest: Path, pyproject: str = "0.1.0", package: str = "0.1.0", manifest: str = "0.1.0"
) -> Path:
    """Only the three files that carry the version."""
    (dest / "lcsc2fusion").mkdir(parents=True)
    (dest / "addin" / "LCSC2Fusion").mkdir(parents=True)
    (dest / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    (dest / "lcsc2fusion" / "__init__.py").write_text(
        f'"""doc."""\n\n__version__ = "{package}"\n', encoding="utf-8"
    )
    (dest / "addin" / "LCSC2Fusion" / "LCSC2Fusion.manifest").write_text(
        json.dumps({"type": "addin", "version": manifest}), encoding="utf-8"
    )
    return dest


def copy_sources(dest: Path, *, lf_installer: bool = False) -> Path:
    """A copy of the repository sources that the builder reads."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "lcsc2fusion", dest / "lcsc2fusion", ignore=ignore)
    shutil.copytree(ROOT / "addin", dest / "addin", ignore=ignore)
    shutil.copytree(ROOT / "installer", dest / "installer")
    shutil.copyfile(ROOT / "pyproject.toml", dest / "pyproject.toml")
    shutil.copyfile(ROOT / "LICENSE", dest / "LICENSE")
    if lf_installer:  # what a checkout without the .gitattributes rules could look like
        for path in (dest / "installer").iterdir():
            text = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
            path.write_bytes(text.encode("utf-8"))
    return dest


# Versions ------------------------------------------------------------------------------------


def test_the_three_versions_of_the_repository_are_identical(br: ModuleType) -> None:
    versions = br.read_versions(ROOT)
    assert versions.pyproject == versions.package == versions.manifest == VERSION
    assert br.check_versions(versions) == VERSION


@pytest.mark.parametrize(
    ("where", "kwargs"),
    [
        ("pyproject.toml = '0.2.0'", {"pyproject": "0.2.0"}),
        ("__version__ = '0.2.0'", {"package": "0.2.0"}),
        ("addin manifest = '0.2.0'", {"manifest": "0.2.0"}),
    ],
)
def test_a_version_mismatch_is_reported_with_all_three_values(
    br: ModuleType, tmp_path: Path, where: str, kwargs: dict[str, str]
) -> None:
    root = light_root(tmp_path, **kwargs)
    with pytest.raises(br.ReleaseError) as caught:
        br.check_versions(br.read_versions(root))
    message = str(caught.value)
    assert where in message
    assert message.count("'0.1.0'") == 2
    assert "pyproject.toml" in message
    assert "__init__.py" in message
    assert "manifest" in message


def test_main_fails_with_a_clear_message_on_a_mismatch_and_builds_nothing(
    br: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = light_root(tmp_path / "src", manifest="9.9.9")
    out = tmp_path / "out"
    assert br.main(["--out", str(out), "--no-exe"], root=root) == 1
    assert "The version differs" in capsys.readouterr().err
    assert not out.exists()
    assert br.main(["--check-tag", "v0.1.0"], root=root) == 1


@pytest.mark.parametrize("version", ["../1.0", "1.0 beta", "v1.0", "1..0", "1.0/x", "-1"])
def test_a_version_that_cannot_be_part_of_a_file_name_is_refused(
    br: ModuleType, tmp_path: Path, version: str
) -> None:
    root = light_root(tmp_path, version, version, version)
    with pytest.raises(br.ReleaseError, match="not usable in file names"):
        br.check_versions(br.read_versions(root))


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "10.20.30", "1.2.3-rc.1", "2.0.0+build5"])
def test_ordinary_versions_are_accepted(br: ModuleType, tmp_path: Path, version: str) -> None:
    root = light_root(tmp_path, version, version, version)
    assert br.check_versions(br.read_versions(root)) == version


def test_missing_or_broken_version_sources_are_reported(br: ModuleType, tmp_path: Path) -> None:
    root = light_root(tmp_path)
    (root / "lcsc2fusion" / "__init__.py").write_text('"""no version"""\n', encoding="utf-8")
    with pytest.raises(br.ReleaseError, match="defines no __version__"):
        br.read_versions(root)
    (root / "lcsc2fusion" / "__init__.py").unlink()
    with pytest.raises(br.ReleaseError, match="is missing"):
        br.read_versions(root)


def test_check_tag_requires_v_and_the_exact_version(br: ModuleType) -> None:
    br.check_tag("v0.1.0", "0.1.0")
    for wrong in ("0.1.0", "v0.1.1", "v0.1", "V0.1.0", "v0.1.0-rc1", "", "refs/tags/v0.1.0"):
        with pytest.raises(br.ReleaseError, match="does not match the version"):
            br.check_tag(wrong, "0.1.0")


def test_check_tag_alone_only_checks(
    br: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = light_root(tmp_path)
    assert br.main(["--check-tag", "v0.1.0"], root=root) == 0
    assert "tag v0.1.0 matches" in capsys.readouterr().out
    assert not (root / "dist").exists()
    assert br.main(["--check-tag", "v0.2.0"], root=root) == 1
    assert "does not match the version" in capsys.readouterr().err
    assert not (root / "dist").exists()


def test_the_real_cli_checks_the_tag_in_a_subprocess() -> None:
    script = str(ROOT / "scripts" / "build_release.py")
    good = subprocess.run(
        [sys.executable, script, "--check-tag", f"v{VERSION}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0, good.stderr
    bad = subprocess.run(
        [sys.executable, script, "--check-tag", "v0.0.0-nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "error:" in bad.stderr


# Installer files and checksums ----------------------------------------------------------------


def test_text_normalization_gives_crlf_and_the_right_byte_order_mark(br: ModuleType) -> None:
    mixed = "a\nb\r\nc\rd\n"
    assert br.normalize_installer_text("x.bat", mixed.encode()) == b"a\r\nb\r\nc\r\nd\r\n"
    assert br.normalize_installer_text("x.cmd", b"@echo off\n") == b"@echo off\r\n"
    ps1 = br.normalize_installer_text("x.ps1", "zażółć\n".encode())
    assert ps1 == BOM + "zażółć\r\n".encode()
    assert br.normalize_installer_text("README.txt", b"x\n").startswith(BOM)
    assert br.normalize_installer_text("x.ps1", BOM + b"x\r\n") == BOM + b"x\r\n"  # no second BOM
    again = br.normalize_installer_text("x.ps1", ps1)
    assert again == ps1  # idempotent


def test_text_normalization_refuses_non_ascii_batch_files_and_bad_utf8(br: ModuleType) -> None:
    with pytest.raises(br.ReleaseError, match="ASCII"):
        br.normalize_installer_text("x.bat", "echo zażółć\n".encode())
    with pytest.raises(br.ReleaseError, match="UTF-8"):
        br.normalize_installer_text("x.ps1", b"\xff\xfe\x00")


def test_payload_checksums_format(br: ModuleType) -> None:
    files = {"b/two.py": b"2", "a.py": b"1", "b/one.py": b"", "A.py": b"upper"}
    lines = br.payload_checksums(files).splitlines(keepends=True)
    assert [line.split("  ", 1)[1] for line in lines] == [
        "A.py\n",
        "a.py\n",
        "b/one.py\n",
        "b/two.py\n",
    ]
    for line in lines:
        digest, name = line.rstrip("\n").split("  ", 1)
        assert digest == hashlib.sha256(files[name]).hexdigest()
        assert digest == digest.lower()
        assert len(digest) == 64
        assert line.endswith("\n")
        assert "\r" not in line


def test_the_installer_root_layout_is_the_documented_one(br: ModuleType) -> None:
    assert br.INSTALLER_ROOT_FILES == (
        "Install-LCSC2Fusion.bat",
        "Uninstall-LCSC2Fusion.bat",
        "install.ps1",
        "uninstall.ps1",
        "README.txt",
    )
    assert br.PAYLOAD_LIST == "payload.sha256"
    assert br.asset_names("1.2.3") == {
        "addin": "LCSC2Fusion-1.2.3-addin.zip",
        "installer": "LCSC2Fusion-1.2.3-installer.zip",
        "exe": "LCSC2Fusion-Setup-1.2.3.exe",
        "sums": "SHA256SUMS.txt",
    }


# IExpress helpers -----------------------------------------------------------------------------


def test_the_sed_names_the_two_files_the_commands_and_absolute_paths(br: ModuleType) -> None:
    sed = br.render_sed("1.2.3", Path(r"C:\work\package"), Path(r"C:\work\setup.exe"))
    lines = sed.splitlines()
    assert "TargetName=C:\\work\\setup.exe" in lines
    assert "SourceFiles0=C:\\work\\package\\" in lines
    assert 'FILE0="installer.zip"' in lines
    assert 'FILE1="bootstrap.cmd"' in lines
    assert "AppLaunched=cmd /c .\\bootstrap.cmd" in lines
    assert "AdminQuietInstCmd=cmd /c .\\bootstrap.cmd quiet" in lines
    assert "UserQuietInstCmd=cmd /c .\\bootstrap.cmd quiet" in lines
    assert "PostInstallCmd=<None>" in lines
    assert "FriendlyName=LCSC2Fusion 1.2.3 setup" in lines
    assert "ShowInstallProgramWindow=1" in lines  # hidden
    prompt = next(line for line in lines if line.startswith("InstallPrompt=") and "%" not in line)
    assert "Install the LCSC2Fusion add-in for Autodesk Fusion?" in prompt
    assert "Zainstalować" in prompt
    assert "{" not in sed
    assert "\u2014" not in sed
    assert "\u2013" not in sed


def test_a_finished_file_is_detected_and_a_missing_one_times_out(
    br: ModuleType, tmp_path: Path
) -> None:
    done = tmp_path / "done.exe"
    done.write_bytes(b"x" * 1000)
    assert br._wait_for_stable_file(done, 5.0, sleep=lambda _seconds: None) == 1000
    with pytest.raises(br.ReleaseError, match="did not produce"):
        br._wait_for_stable_file(tmp_path / "never.exe", 0.05, sleep=lambda _seconds: None)


def test_pe_files_are_recognized(br: ModuleType, tmp_path: Path) -> None:
    pe = tmp_path / "ok.exe"
    header = bytearray(b"MZ" + bytes(62))
    header[0x3C:0x40] = (64).to_bytes(4, "little")
    pe.write_bytes(bytes(header) + b"PE\0\0" + bytes(100))
    assert br._is_pe_file(pe)
    for name, data in (("text.exe", b"hello" * 50), ("short.exe", b"MZ"), ("empty.exe", b"")):
        (tmp_path / name).write_bytes(data)
        assert not br._is_pe_file(tmp_path / name)
    wrong = bytearray(header)
    wrong[0x3C:0x40] = (200).to_bytes(4, "little")
    (tmp_path / "wrong.exe").write_bytes(bytes(wrong) + bytes(300))
    assert not br._is_pe_file(tmp_path / "wrong.exe")


def test_there_is_no_setup_exe_builder_outside_windows(br: ModuleType) -> None:
    if IS_WINDOWS:
        assert br.find_iexpress() is not None
    else:
        assert br.find_iexpress() is None


# The release (without the exe) ----------------------------------------------------------------


@dataclass(frozen=True)
class Built:
    out: Path
    assets: list[object]
    names: dict[str, str]


@pytest.fixture(scope="module")
def built(br: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Built:
    out = tmp_path_factory.mktemp("release") / "out"
    assets = br.build_release(ROOT, out, want_exe=False)
    return Built(out, assets, br.asset_names(VERSION))


def zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_the_release_has_exactly_the_documented_assets(built: Built) -> None:
    assert sorted(p.name for p in built.out.iterdir()) == [
        f"LCSC2Fusion-{VERSION}-addin.zip",
        f"LCSC2Fusion-{VERSION}-installer.zip",
        "SHA256SUMS.txt",
    ]


def test_sha256sums_lists_every_asset_and_matches_the_files(built: Built) -> None:
    sums = (built.out / "SHA256SUMS.txt").read_bytes().decode("ascii")
    assert sums.endswith("\n")
    assert "\r" not in sums
    entries = [line.split("  ", 1) for line in sums.splitlines()]
    assert [name for _, name in entries] == [
        f"LCSC2Fusion-{VERSION}-addin.zip",
        f"LCSC2Fusion-{VERSION}-installer.zip",
    ]
    for digest, name in entries:
        assert digest == hashlib.sha256((built.out / name).read_bytes()).hexdigest()


def test_the_addin_zip_is_the_plain_build_of_the_addin(
    built: Built, build_addin: ModuleType, tmp_path: Path
) -> None:
    result = build_addin.build(root=ROOT, dist=tmp_path / "dist")
    shipped = (built.out / f"LCSC2Fusion-{VERSION}-addin.zip").read_bytes()
    assert shipped == result.zip_path.read_bytes()
    members = zip_members(built.out / f"LCSC2Fusion-{VERSION}-addin.zip")
    assert all(name.startswith("LCSC2Fusion/") for name in members)
    assert "LCSC2Fusion/LCSC2Fusion.manifest" in members


def test_the_installer_zip_has_the_release_root_layout(
    built: Built, build_addin: ModuleType, br: ModuleType, tmp_path: Path
) -> None:
    result = build_addin.build(root=ROOT, dist=tmp_path / "dist")
    path = built.out / f"LCSC2Fusion-{VERSION}-installer.zip"
    members = zip_members(path)
    payload = {name: (result.folder / name).read_bytes() for name in result.files}
    expected = {f"LCSC2Fusion/{name}": data for name, data in payload.items()}
    for name in br.INSTALLER_ROOT_FILES:
        expected[name] = br.read_installer_file(ROOT, name)
    expected["payload.sha256"] = br.payload_checksums(payload).encode("ascii")
    assert members == expected
    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist()]
        assert names == sorted(names, key=lambda item: item.encode("utf-8"))
        for info in archive.infolist():
            assert info.date_time == (2020, 1, 1, 0, 0, 0)
            assert info.external_attr == 0o644 << 16
            assert info.create_system == 3
            assert info.compress_type == zipfile.ZIP_DEFLATED
            assert not info.filename.endswith("/")
            assert "\\" not in info.filename
    assert "bootstrap.cmd" not in members  # only inside the setup exe


def test_payload_sha256_covers_every_add_in_file_and_nothing_else(built: Built) -> None:
    members = zip_members(built.out / f"LCSC2Fusion-{VERSION}-installer.zip")
    listed = members["payload.sha256"].decode("ascii").splitlines()
    pairs = [line.split("  ", 1) for line in listed]
    assert [name for _, name in pairs] == sorted(name for _, name in pairs)
    in_zip = {
        name.removeprefix("LCSC2Fusion/"): data
        for name, data in members.items()
        if name.startswith("LCSC2Fusion/")
    }
    assert {name for _, name in pairs} == set(in_zip)
    for digest, name in pairs:
        assert digest == hashlib.sha256(in_zip[name]).hexdigest()
    assert "LCSC2Fusion.manifest" in in_zip
    assert not [name for name in in_zip if name.endswith(".pyc") or "__pycache__" in name]


def test_installer_text_files_in_the_zip_are_crlf_with_bom_where_needed(built: Built) -> None:
    members = zip_members(built.out / f"LCSC2Fusion-{VERSION}-installer.zip")
    for name in ("install.ps1", "uninstall.ps1", "README.txt"):
        assert members[name].startswith(BOM), name
        assert members[name].replace(b"\r\n", b"").count(b"\n") == 0, name
    for name in ("Install-LCSC2Fusion.bat", "Uninstall-LCSC2Fusion.bat"):
        assert not members[name].startswith(BOM), name
        members[name].decode("ascii")
        assert members[name].replace(b"\r\n", b"").count(b"\n") == 0, name


def test_building_twice_gives_identical_zips(br: ModuleType, tmp_path: Path) -> None:
    digests = []
    for run in ("a", "b"):
        out = tmp_path / run
        br.build_release(ROOT, out, want_exe=False)
        digests.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()})
    assert digests[0] == digests[1]
    assert len(digests[0]) == 3


def test_an_lf_checkout_still_gives_crlf_and_bom_in_the_zip(br: ModuleType, tmp_path: Path) -> None:
    root = copy_sources(tmp_path / "src", lf_installer=True)
    assert b"\r\n" not in (root / "installer" / "install.ps1").read_bytes()
    out = tmp_path / "out"
    br.build_release(root, out, want_exe=False)
    members = zip_members(out / f"LCSC2Fusion-{VERSION}-installer.zip")
    assert members["install.ps1"].startswith(BOM)
    assert b"\r\n" in members["install.ps1"]
    assert members["install.ps1"].replace(b"\r\n", b"").count(b"\n") == 0
    reference = zip_members(reference_installer_zip(br, tmp_path))
    for name in br.INSTALLER_ROOT_FILES:
        assert members[name] == reference[name], name  # same bytes as from the CRLF checkout


def reference_installer_zip(br: ModuleType, tmp_path: Path) -> Path:
    out = tmp_path / "reference"
    br.build_release(ROOT, out, want_exe=False)
    return out / f"LCSC2Fusion-{VERSION}-installer.zip"


def test_old_assets_of_the_same_names_are_replaced_and_other_files_stay(
    br: ModuleType, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    stale = {
        f"LCSC2Fusion-{VERSION}-addin.zip": b"stale zip",
        f"LCSC2Fusion-{VERSION}-installer.zip": b"stale zip",
        f"LCSC2Fusion-Setup-{VERSION}.exe": b"stale exe",
        "SHA256SUMS.txt": b"stale sums",
    }
    for name, data in stale.items():
        (out / name).write_bytes(data)
    (out / "notes.txt").write_text("not an asset", encoding="utf-8")
    br.build_release(ROOT, out, want_exe=False)
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [
            f"LCSC2Fusion-{VERSION}-addin.zip",
            f"LCSC2Fusion-{VERSION}-installer.zip",
            "SHA256SUMS.txt",
            "notes.txt",
        ]
    )  # the stale exe is gone: it would not match the new checksums
    assert (out / "notes.txt").read_text(encoding="utf-8") == "not an asset"
    assert b"stale" not in (out / "SHA256SUMS.txt").read_bytes()
    assert zipfile.is_zipfile(out / f"LCSC2Fusion-{VERSION}-addin.zip")


def test_main_prints_the_asset_list_with_sizes(
    br: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert br.main(["--out", str(tmp_path / "out"), "--no-exe"]) == 0
    out = capsys.readouterr().out
    assert f"Release {VERSION} in" in out
    for name in (
        f"LCSC2Fusion-{VERSION}-addin.zip",
        f"LCSC2Fusion-{VERSION}-installer.zip",
        "SHA256SUMS.txt",
    ):
        assert name in out
    assert "KiB" in out


def test_a_build_failure_is_an_error_message_and_exit_1(
    br: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = copy_sources(tmp_path / "src")
    (root / "installer" / "install.ps1").unlink()
    assert br.main(["--out", str(tmp_path / "out"), "--no-exe"], root=root) == 1
    assert "Installer file missing" in capsys.readouterr().err
    root = copy_sources(tmp_path / "src2")
    (root / "addin" / "LCSC2Fusion" / "LCSC2Fusion.py").unlink()
    assert br.main(["--out", str(tmp_path / "out2"), "--no-exe"], root=root) == 1
    assert "Building the add-in failed" in capsys.readouterr().err


def test_the_existing_add_in_build_script_is_untouched_by_the_release_builder(
    br: ModuleType, build_addin: ModuleType, tmp_path: Path
) -> None:
    zip_path = ROOT / "dist" / "LCSC2Fusion.zip"
    before = zip_path.stat().st_mtime_ns if zip_path.exists() else None
    br.build_release(ROOT, tmp_path / "out", want_exe=False)
    after = zip_path.stat().st_mtime_ns if zip_path.exists() else None
    assert after == before  # the release is built in a temporary dist, not in dist/
    assert hasattr(build_addin, "build")
    assert br.__name__ != build_addin.__name__


def test_the_release_builder_is_stdlib_only() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


# End to end: the real release installed into a temp folder --------------------------------------


@dataclass(frozen=True)
class FullRelease:
    out: Path
    exe: Path | None
    addin_files: dict[str, bytes]


@pytest.fixture(scope="module")
def full_release(
    br: ModuleType, build_addin: ModuleType, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[FullRelease]:
    work = tmp_path_factory.mktemp("full")
    out = work / "out"
    with_exe = IS_WINDOWS and br.find_iexpress() is not None
    br.build_release(ROOT, out, want_exe=with_exe)
    result = build_addin.build(root=ROOT, dist=work / "dist")
    files = {name: (result.folder / name).read_bytes() for name in result.files}
    exe = out / f"LCSC2Fusion-Setup-{VERSION}.exe"
    yield FullRelease(out, exe if exe.exists() else None, files)


def extract(zip_path: Path, folder: Path) -> Path:
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(folder)
    return folder


@WINDOWS_ONLY
@pytest.mark.slow
def test_end_to_end_the_installer_zip_installs_exactly_the_add_in_build(
    full_release: FullRelease, tmp_path: Path
) -> None:
    sandbox = Sandbox(tmp_path / "profile")
    extracted = extract(
        full_release.out / f"LCSC2Fusion-{VERSION}-installer.zip", tmp_path / "unpacked here"
    )
    target = sandbox.root / "Fusion Addins"
    done = run_script(
        extracted / "install.ps1", "-TargetRoot", str(target), "-Language", "en", sandbox=sandbox
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"add-in version {VERSION}" in done.stdout
    installed = tree_bytes(target / "LCSC2Fusion")
    assert installed == full_release.addin_files  # every file, byte for byte
    assert sorted(p.name for p in target.iterdir()) == ["LCSC2Fusion"]
    manifest = json.loads(installed["LCSC2Fusion.manifest"])
    assert manifest["type"] == "addin"
    assert manifest["version"] == VERSION
    # the same install again (an update over the same version) and then the uninstaller
    again = run_script(extracted / "install.ps1", "-TargetRoot", str(target), sandbox=sandbox)
    assert again.returncode == 0, again.stdout + again.stderr
    assert tree_bytes(target / "LCSC2Fusion") == full_release.addin_files
    gone = run_script(extracted / "uninstall.ps1", "-TargetRoot", str(target), sandbox=sandbox)
    assert gone.returncode == 0, gone.stdout + gone.stderr
    assert list(target.iterdir()) == []


@WINDOWS_ONLY
@pytest.mark.slow
def test_end_to_end_with_expand_archive_and_the_bat_file(
    full_release: FullRelease, tmp_path: Path
) -> None:
    """Expand-Archive is what the setup exe uses; the bat file is what a person double clicks."""
    sandbox = Sandbox(tmp_path / "profile")
    unpacked = tmp_path / "Rozpakowane (x86) & dodatki"
    expanded = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-Command",
            "Expand-Archive -LiteralPath $env:ZIP -DestinationPath $env:DEST -Force",
        ],
        capture_output=True,
        text=True,
        env=sandbox.env(
            {
                "ZIP": str(full_release.out / f"LCSC2Fusion-{VERSION}-installer.zip"),
                "DEST": str(unpacked),
            }
        ),
        check=False,
    )
    assert expanded.returncode == 0, expanded.stdout + expanded.stderr
    target = sandbox.root / "FusionAddins"
    env = sandbox.env({"LCSC2FUSION_FUSIONADDINS_DIR": str(target)})
    done = subprocess.run(
        f'cmd /s /c ""{unpacked / "Install-LCSC2Fusion.bat"}" -Language en"',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input="",
        env=env,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_bytes(target / "LCSC2Fusion") == full_release.addin_files


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_setup_exe_is_a_pe_file_with_exactly_the_two_expected_files(
    full_release: FullRelease, br: ModuleType, tmp_path: Path
) -> None:
    exe = full_release.exe
    if exe is None:
        pytest.skip("IExpress is not available: the release was built without the setup exe")
    installer_zip = full_release.out / f"LCSC2Fusion-{VERSION}-installer.zip"
    assert br._is_pe_file(exe)
    assert exe.stat().st_size > installer_zip.stat().st_size
    unpacked = tmp_path / "exe_contents"
    unpacked.mkdir()
    done = subprocess.run([str(exe), "/C", f"/T:{unpacked}", "/Q"], timeout=120, check=False)
    assert done.returncode == 0
    assert sorted(p.name for p in unpacked.iterdir()) == ["bootstrap.cmd", "installer.zip"]
    assert (unpacked / "installer.zip").read_bytes() == installer_zip.read_bytes()
    assert (unpacked / "bootstrap.cmd").read_bytes() == br.read_installer_file(
        ROOT, "bootstrap.cmd"
    )


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_setup_exe_installs_quietly_into_a_temp_target(
    full_release: FullRelease, tmp_path: Path
) -> None:
    exe = full_release.exe
    if exe is None:
        pytest.skip("IExpress is not available: the release was built without the setup exe")
    sandbox = Sandbox(tmp_path / "profile")
    target = sandbox.root / "FusionAddins"
    temp = tmp_path / "temp"
    temp.mkdir()
    log = tmp_path / "setup.log"
    env = sandbox.env(
        {
            "LCSC2FUSION_FUSIONADDINS_DIR": str(target),
            "LCSC2FUSION_SETUP_LOG": str(log),
            "TEMP": str(temp),
            "TMP": str(temp),
        }
    )
    done = subprocess.run([str(exe), "/Q"], env=env, timeout=180, check=False)
    # IExpress returns 0 whenever it could start the command; it ignores the command's own code.
    assert done.returncode == 0
    assert tree_bytes(target / "LCSC2Fusion") == full_release.addin_files
    text = log.read_text(encoding="utf-8", errors="replace")
    # the exe has no language switch: it follows the Windows display language of the machine
    assert (
        f"Done. LCSC2Fusion {VERSION} is installed in" in text
        or f"Gotowe. LCSC2Fusion {VERSION} jest zainstalowany w" in text
    )
    leftover = [p.name for p in temp.iterdir() if p.name.startswith(("IXP", "LCSC2Fusion-setup"))]
    assert leftover == []  # the extraction folder and the work folder are cleaned up


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_setup_exe_leaves_the_old_version_when_the_install_fails(
    full_release: FullRelease, tmp_path: Path
) -> None:
    exe = full_release.exe
    if exe is None:
        pytest.skip("IExpress is not available: the release was built without the setup exe")
    sandbox = Sandbox(tmp_path / "profile")
    target = sandbox.root / "FusionAddins"
    (target / "LCSC2Fusion").mkdir(parents=True)
    (target / "LCSC2Fusion" / "LCSC2Fusion.py").write_text("old", encoding="utf-8")
    log = tmp_path / "setup.log"
    env = sandbox.env(
        {
            "LCSC2FUSION_FUSIONADDINS_DIR": str(target),
            "LCSC2FUSION_SETUP_LOG": str(log),
            "LCSC2FUSION_TEST_FAULT": "after-swap",
        }
    )
    done = subprocess.run([str(exe), "/Q"], env=env, timeout=180, check=False)
    assert done.returncode in (0, 13)  # IExpress does not pass the exit code on (see the report)
    assert tree_bytes(target / "LCSC2Fusion") == {"LCSC2Fusion.py": b"old"}
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Exit code: 13" in text or "Kod wyjścia: 13" in text
    assert "The previous version was restored." in text or "Przywrócono poprzednią wersję." in text


@WINDOWS_ONLY
@pytest.mark.slow
@pytest.mark.parametrize("folder", ["Użytkownik Łódź", "用户 中文"])
def test_the_setup_exe_builds_when_the_temp_folder_has_unusual_characters(
    full_release: FullRelease,
    br: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
) -> None:
    """IExpress fails silently on non ASCII paths: the builder works in an ASCII folder."""
    iexpress = br.find_iexpress()
    if iexpress is None:
        pytest.skip("IExpress is not available")
    temp = tmp_path / folder
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    exe = tmp_path / "built.exe"
    installer_zip = full_release.out / f"LCSC2Fusion-{VERSION}-installer.zip"
    br.build_setup_exe(ROOT, installer_zip, exe, VERSION, iexpress)
    assert br._is_pe_file(exe)
    assert exe.stat().st_size > installer_zip.stat().st_size
    assert list(temp.iterdir()) == []  # the work folder is removed again
