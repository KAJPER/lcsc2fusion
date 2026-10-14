"""Build the files of a GitHub release (stdlib only).

    python scripts/build_release.py [--out DIR] [--no-exe] [--check-tag vX.Y.Z]

Assets written to the output folder (default `dist/release`), `<ver>` is the version:

    LCSC2Fusion-<ver>-addin.zip       the plain add-in zip (same bytes as dist/LCSC2Fusion.zip),
                                      for manual install and for macOS
    LCSC2Fusion-<ver>-installer.zip   one click installer for Windows: Install-LCSC2Fusion.bat,
                                      Uninstall-LCSC2Fusion.bat, install.ps1, uninstall.ps1,
                                      README.txt, payload.sha256 and the LCSC2Fusion/ add-in folder
    LCSC2Fusion-Setup-<ver>.exe       the same installer as a single file, made with Windows' own
                                      IExpress (Windows only, skipped with --no-exe)
    SHA256SUMS.txt                    "<sha256>  <file name>" for every asset above

The version must be identical in pyproject.toml, lcsc2fusion/__init__.py and the add-in manifest.
`--check-tag vX.Y.Z` also requires the tag to equal `v<version>`; alone it only checks (exit 0 or
1), together with `--out` or `--no-exe` it checks and then builds. Both zips are deterministic:
sorted members, fixed timestamps and permissions (byte identical for the same sources and the same
zlib). The installer text files are written with CRLF line ends, the .ps1 and README.txt with a
UTF-8 BOM, whatever the checkout looks like.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ADDIN_NAME = "LCSC2Fusion"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist" / "release"

# Files of the installer folder that go to the root of the installer zip.
INSTALLER_ROOT_FILES = (
    "Install-LCSC2Fusion.bat",
    "Uninstall-LCSC2Fusion.bat",
    "install.ps1",
    "uninstall.ps1",
    "README.txt",
)
BOOTSTRAP_FILE = "bootstrap.cmd"  # only inside the setup exe, not in the installer zip
PAYLOAD_LIST = "payload.sha256"
SUMS_FILE = "SHA256SUMS.txt"
EMBEDDED_ZIP_NAME = "installer.zip"  # the name of the installer zip inside the setup exe
UTF8_BOM = b"\xef\xbb\xbf"
BOM_SUFFIXES = (".ps1", ".txt")  # UTF-8 with BOM, so that Windows PowerShell 5.1 and Notepad agree
VERSION_PATTERN = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")

INSTALL_PROMPT = (
    "Install the LCSC2Fusion add-in for Autodesk Fusion? "
    "(Zainstalować dodatek LCSC2Fusion dla Autodesk Fusion?)"
)
# IExpress runs the command with the extraction folder as the current folder. Windows can be set
# up so that cmd does not search the current folder (NoDefaultCurrentDirectoryInExePath), so the
# command names the file with a leading ".\".
IEXPRESS_COMMAND = "cmd /c .\\" + BOOTSTRAP_FILE
IEXPRESS_QUIET_COMMAND = IEXPRESS_COMMAND + " quiet"
IEXPRESS_TIMEOUT_SECONDS = 120.0
IEXPRESS_FILE_WAIT_SECONDS = 30.0  # the exe normally exists the moment iexpress.exe returns
IEXPRESS_STABLE_POLLS = 4
IEXPRESS_POLL_SECONDS = 0.25
MIN_EXE_OVERHEAD = 50_000  # the self extractor stub alone is about 190 KB

SED_TEMPLATE = """[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt={install_prompt}
DisplayLicense=
FinishMessage=
TargetName={target}
FriendlyName={friendly_name}
AppLaunched={command}
PostInstallCmd=<None>
AdminQuietInstCmd={quiet_command}
UserQuietInstCmd={quiet_command}
FILE0="{zip_name}"
FILE1="{bootstrap_name}"
[SourceFiles]
SourceFiles0={source_dir}\\
[SourceFiles0]
%FILE0%=
%FILE1%=
"""


class ReleaseError(Exception):
    """The release cannot be built. The message says what to fix."""


@dataclass(frozen=True)
class Versions:
    pyproject: str
    package: str
    manifest: str


@dataclass(frozen=True)
class Asset:
    name: str
    path: Path
    size: int
    sha256: str


# Versions ------------------------------------------------------------------------------------


def read_versions(root: Path) -> Versions:
    """The version as written in pyproject.toml, lcsc2fusion/__init__.py and the add-in manifest."""
    pyproject_path = root / "pyproject.toml"
    init_path = root / "lcsc2fusion" / "__init__.py"
    manifest_path = root / "addin" / ADDIN_NAME / f"{ADDIN_NAME}.manifest"
    for path in (pyproject_path, init_path, manifest_path):
        if not path.is_file():
            raise ReleaseError(f"Cannot read the version: {path} is missing.")
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise ReleaseError(f"pyproject.toml has no readable [project] version: {exc!r}") from exc
    found = re.search(
        r"""^__version__\s*=\s*["']([^"']+)["']""",
        init_path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if found is None:
        raise ReleaseError(f"{init_path} defines no __version__.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ReleaseError(f"{manifest_path.name} has no readable version: {exc!r}") from exc
    return Versions(str(pyproject), found.group(1), str(manifest))


def check_versions(versions: Versions) -> str:
    """The common version, or a ReleaseError that names the three values."""
    if not versions.pyproject == versions.package == versions.manifest:
        raise ReleaseError(
            "The version differs between the three places that carry it, make them identical: "
            f"pyproject.toml = {versions.pyproject!r}, "
            f"lcsc2fusion/__init__.py __version__ = {versions.package!r}, "
            f"addin manifest = {versions.manifest!r}."
        )
    version = versions.pyproject
    if not VERSION_PATTERN.match(version) or ".." in version:
        raise ReleaseError(
            f"Version {version!r} is not usable in file names: start with a digit and use only "
            "letters, digits and . + - (for example 1.2.3 or 1.2.3-rc.1)."
        )
    return version


def check_tag(tag: str, version: str) -> None:
    if tag != f"v{version}":
        raise ReleaseError(
            f"The tag {tag!r} does not match the version {version!r}: expected 'v{version}'."
        )


# Installer files -----------------------------------------------------------------------------


def normalize_installer_text(name: str, data: bytes) -> bytes:
    """CRLF line ends; UTF-8 with BOM for .ps1 and .txt; plain ASCII for .bat and .cmd."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"{name} is not valid UTF-8: {exc}") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if name.endswith(BOM_SUFFIXES):
        return UTF8_BOM + text.encode("utf-8")
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ReleaseError(f"{name} must contain ASCII characters only: {exc}") from exc


def read_installer_file(root: Path, name: str) -> bytes:
    path = root / "installer" / name
    if not path.is_file():
        raise ReleaseError(f"Installer file missing: {path}")
    return normalize_installer_text(name, path.read_bytes())


def payload_checksums(files: dict[str, bytes]) -> str:
    """payload.sha256: "<sha256 lowercase hex>  <relative path>" per file, sorted by path."""
    lines = [
        f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n"
        for name in sorted(files, key=lambda item: item.encode("utf-8"))
    ]
    return "".join(lines)


def write_deterministic_zip(
    zip_path: Path, members: dict[str, bytes], build_addin: ModuleType
) -> None:
    """Sorted members, fixed timestamp, permissions and creator system, deflate."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members, key=lambda item: item.encode("utf-8")):
            info = zipfile.ZipInfo(name, build_addin.ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = build_addin.ZIP_FILE_MODE
            info.create_system = build_addin.ZIP_CREATOR_UNIX
            archive.writestr(info, members[name])


def build_installer_zip(
    root: Path, addin_folder: Path, addin_files: tuple[str, ...], zip_path: Path
) -> dict[str, bytes]:
    """The release root layout, zipped. Returns the members (name to bytes)."""
    build_addin = _load_build_addin()
    payload = {name: (addin_folder / name).read_bytes() for name in addin_files}
    members = {f"{ADDIN_NAME}/{name}": data for name, data in payload.items()}
    for name in INSTALLER_ROOT_FILES:
        members[name] = read_installer_file(root, name)
    members[PAYLOAD_LIST] = payload_checksums(payload).encode("ascii")
    write_deterministic_zip(zip_path, members, build_addin)
    return members


# Setup exe (IExpress) ------------------------------------------------------------------------


def find_iexpress() -> Path | None:
    """Windows' own self extractor builder, or None (not Windows, or not installed)."""
    if sys.platform != "win32":
        return None
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")  # not case sensitive
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "iexpress.exe"
    return candidate if candidate.is_file() else None


def render_sed(version: str, source_dir: Path, target: Path) -> str:
    """The IExpress package description. All paths are absolute (IExpress needs that)."""
    return SED_TEMPLATE.format(
        install_prompt=INSTALL_PROMPT,
        target=target,
        friendly_name=f"LCSC2Fusion {version} setup",
        command=IEXPRESS_COMMAND,
        quiet_command=IEXPRESS_QUIET_COMMAND,
        zip_name=EMBEDDED_ZIP_NAME,
        bootstrap_name=BOOTSTRAP_FILE,
        source_dir=source_dir,
    )


def _make_ascii_workdir() -> Path:
    """IExpress fails silently on non ASCII paths (tried with Polish letters), and the default temp
    folder lies in the user profile: fall back to the public profile or the drive root."""
    candidates = [Path(tempfile.gettempdir())]
    for variable, suffix in (("PUBLIC", ""), ("SYSTEMDRIVE", "\\")):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value + suffix))
    for base in candidates:
        try:
            work = Path(tempfile.mkdtemp(prefix="lcsc2fusion-iexpress-", dir=base))
        except OSError:
            continue
        if str(work).isascii():
            return work
        shutil.rmtree(work, ignore_errors=True)
    raise ReleaseError(
        "IExpress cannot read non ASCII paths and no ASCII temp folder is available. "
        "Set TEMP to a plain folder such as C:\\Temp, or build with --no-exe."
    )


def _is_pe_file(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            return False
        offset = int.from_bytes(header[0x3C:0x40], "little")
        handle.seek(offset)
        return handle.read(4) == b"PE\0\0"


def _wait_for_stable_file(
    path: Path, timeout: float, sleep: Callable[[float], None] = time.sleep
) -> int:
    """IExpress may return before the exe is complete: wait until the size stops changing."""
    deadline = time.monotonic() + timeout
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
        size = path.stat().st_size if path.is_file() else -1
        if size > 0 and size == last_size:
            stable += 1
            if stable >= IEXPRESS_STABLE_POLLS:
                return size
        else:
            stable = 0
        last_size = size
        sleep(IEXPRESS_POLL_SECONDS)
    raise ReleaseError(f"IExpress did not produce a finished {path.name} within {timeout:.0f} s.")


def build_setup_exe(
    root: Path, installer_zip: Path, destination: Path, version: str, iexpress: Path
) -> None:
    """LCSC2Fusion-Setup-<ver>.exe: IExpress packs the installer zip and bootstrap.cmd."""
    work = _make_ascii_workdir()
    try:
        package = work / "package"
        package.mkdir()
        shutil.copyfile(installer_zip, package / EMBEDDED_ZIP_NAME)
        (package / BOOTSTRAP_FILE).write_bytes(read_installer_file(root, BOOTSTRAP_FILE))
        built = work / "setup.exe"
        sed_path = work / "setup.sed"
        sed_text = render_sed(version, package, built).replace("\n", "\r\n")
        # ANSI text. The paths are ASCII safe; an exotic code page may lose a letter of the prompt.
        sed_path.write_bytes(sed_text.encode("mbcs", errors="replace"))
        try:
            completed = subprocess.run(
                [str(iexpress), "/N", "/Q", str(sed_path)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=IEXPRESS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseError("IExpress did not finish in time.") from exc
        # IExpress sets odd exit codes, so the finished file is the only reliable result.
        size = _wait_for_stable_file(built, IEXPRESS_FILE_WAIT_SECONDS)
        zip_size = installer_zip.stat().st_size
        if not _is_pe_file(built) or size < zip_size + MIN_EXE_OVERHEAD:
            raise ReleaseError(
                f"IExpress produced an invalid {destination.name} ({size} bytes, exit code "
                f"{completed.returncode}); the installer zip has {zip_size} bytes."
            )
        shutil.copyfile(built, destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# Release -------------------------------------------------------------------------------------


def _load_build_addin() -> ModuleType:
    """scripts/ is not a package: load build_addin.py by path (once)."""
    name = "lcsc2fusion_build_addin"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("build_addin.py"))
    if spec is None or spec.loader is None:
        raise ReleaseError("Cannot load scripts/build_addin.py.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the defining module up here
    spec.loader.exec_module(module)
    return module


def asset_names(version: str) -> dict[str, str]:
    return {
        "addin": f"{ADDIN_NAME}-{version}-addin.zip",
        "installer": f"{ADDIN_NAME}-{version}-installer.zip",
        "exe": f"{ADDIN_NAME}-Setup-{version}.exe",
        "sums": SUMS_FILE,
    }


def _describe(path: Path) -> Asset:
    return Asset(
        name=path.name,
        path=path,
        size=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def build_release(root: Path, out: Path, *, want_exe: bool = True) -> list[Asset]:
    """Check the version, build everything into `out` and return the assets (sums file last)."""
    version = check_versions(read_versions(root))
    names = asset_names(version)
    iexpress = find_iexpress() if want_exe else None
    out.mkdir(parents=True, exist_ok=True)
    for name in names.values():  # old assets of the same names must not survive
        (out / name).unlink(missing_ok=True)

    build_addin = _load_build_addin()
    with tempfile.TemporaryDirectory(prefix="lcsc2fusion-release-") as temp:
        try:
            result = build_addin.build(root=root, dist=Path(temp) / "dist")
        except build_addin.BuildError as exc:
            raise ReleaseError(f"Building the add-in failed: {exc}") from exc
        addin_zip = out / names["addin"]
        shutil.copyfile(result.zip_path, addin_zip)
        installer_zip = out / names["installer"]
        build_installer_zip(root, result.folder, result.files, installer_zip)
    assets = [_describe(addin_zip), _describe(installer_zip)]
    if want_exe and iexpress is not None:
        exe = out / names["exe"]
        build_setup_exe(root, installer_zip, exe, version, iexpress)
        assets.append(_describe(exe))
    assets.sort(key=lambda asset: asset.name)
    sums = out / names["sums"]
    sums.write_bytes("".join(f"{a.sha256}  {a.name}\n" for a in assets).encode("ascii"))
    assets.append(_describe(sums))
    return assets


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Build the files of a LCSC2Fusion GitHub release.")
    parser.add_argument("--out", type=Path, default=None, help="output folder (dist/release)")
    parser.add_argument("--no-exe", action="store_true", help="do not build the setup exe")
    parser.add_argument("--check-tag", metavar="TAG", help="require TAG to equal v<version>")
    args = parser.parse_args(argv)
    try:
        version = check_versions(read_versions(root))
        if args.check_tag is not None:
            check_tag(args.check_tag, version)
            print(
                f"Version {version} is the same in all three places; tag {args.check_tag} matches."
            )
            if args.out is None and not args.no_exe:
                return 0
        out = args.out if args.out is not None else root / "dist" / "release"
        want_exe = not args.no_exe
        if want_exe and find_iexpress() is None:
            print("note: no setup exe (IExpress is only available on Windows).")
        assets = build_release(root, out, want_exe=want_exe)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Release {version} in {out}:")
    for asset in assets:
        print(f"  {asset.name:<40} {asset.size / 1024:9.1f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
