"""Put a signed setup exe into a release folder and rewrite SHA256SUMS.txt (stdlib only).

    python scripts/release_checksums.py replace-exe DIR SIGNED_EXE --version 0.1.0
    python scripts/release_checksums.py write DIR [--version 0.1.0] [--no-exe] [--only-changed NAME]

The release workflow signs the setup exe with SignPath in a separate job, then this script runs in
a job that holds no secret (RELEASING.md, section "SignPath Foundation"):

* `replace-exe` swaps `DIR/LCSC2Fusion-Setup-<ver>.exe` for the signed file, but only when the two
  are the same program: the signed file must be the unsigned one plus an Authenticode signature
  (certificate table appended at the end, the PE CheckSum and the security directory entry
  changed, nothing else). A different or damaged exe is refused before anything is replaced.
* `write` computes SHA256SUMS.txt again over every file of DIR, in the format that
  scripts/build_release.py writes and scripts/verify_checksums.py reads. With `--only-changed` it
  also refuses to go on when any file other than the named ones differs from the old
  SHA256SUMS.txt (a zip that changed by accident would otherwise be hidden by the new list).

Exit code 0 when all is fine, 1 with one line per problem otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType

EXE_NAME_PATTERN = "LCSC2Fusion-Setup-{version}.exe"
PE_CHECKSUM_OFFSET = 64  # CheckSum field, from the start of the optional header (PE32 and PE32+)
SECURITY_DIRECTORY_INDEX = 4  # IMAGE_DIRECTORY_ENTRY_SECURITY: the certificate table
PE32_MAGIC = 0x10B
PE32_PLUS_MAGIC = 0x20B
CERT_TABLE_ALIGNMENT = 8


class ReleaseChecksumError(Exception):
    """The release folder cannot be changed. The message says what to fix."""


def _load_sibling(name: str) -> ModuleType:
    """scripts/ is not a package: load a sibling script by path (once, shared with its users)."""
    key = f"lcsc2fusion_script_{name}"
    loaded = sys.modules.get(key)
    if loaded is not None:
        return loaded
    spec = importlib_util.spec_from_file_location(key, Path(__file__).with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise ReleaseChecksumError(f"Cannot load scripts/{name}.py.")
    module = importlib_util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _checksums() -> ModuleType:
    return _load_sibling("verify_checksums")


# Sums file -----------------------------------------------------------------------------------


def release_files(folder: Path) -> list[Path]:
    """The files that SHA256SUMS.txt must list, sorted by name. Plain files only: a sub folder or
    a link in a release folder is a mistake that must not be listed or published silently."""
    sums_name = _checksums().SUMS_NAME
    if not folder.is_dir():
        raise ReleaseChecksumError(f"{folder} is not a folder.")
    files: list[Path] = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.name == sums_name:
            continue
        if entry.is_symlink() or not entry.is_file():
            raise ReleaseChecksumError(f"{entry.name} in {folder} is not a plain file.")
        files.append(entry)
    return files


def compute_digests(folder: Path) -> dict[str, str]:
    """File name to SHA-256 (lowercase hex) of every release file, in name order."""
    checksums = _checksums()
    digests: dict[str, str] = {}
    for path in release_files(folder):
        if not path.name.isascii():
            raise ReleaseChecksumError(f"{path.name} is not an ASCII file name.")
        digests[path.name] = checksums.sha256_of(path)
    return digests


def render_sums(digests: dict[str, str]) -> str:
    """`<sha256>  <name>` per line, the same bytes scripts/build_release.py writes."""
    return "".join(f"{digest}  {name}\n" for name, digest in sorted(digests.items()))


def read_sums(folder: Path) -> dict[str, str] | None:
    """The digests listed in the current SHA256SUMS.txt, None when there is no such file."""
    checksums = _checksums()
    sums = folder / checksums.SUMS_NAME
    if not sums.is_file():
        return None
    listed: dict[str, str] = {}
    for number, line in enumerate(sums.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        match = checksums.LINE_RE.match(line)
        if match is None:
            raise ReleaseChecksumError(f"{checksums.SUMS_NAME} line {number} is unreadable.")
        listed[match.group("name")] = match.group("digest").lower()
    return listed


def describe_changes(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """One line per added, removed or changed file."""
    lines = [f"{name}: added" for name in new if name not in old]
    lines += [f"{name}: removed" for name in old if name not in new]
    lines += [f"{name}: changed" for name in new if name in old and new[name] != old[name]]
    return lines


def write_sums(
    folder: Path,
    version: str | None = None,
    *,
    with_exe: bool = True,
    only_changed: tuple[str, ...] = (),
) -> list[str]:
    """Rewrite DIR/SHA256SUMS.txt and return the change lines. Raises ReleaseChecksumError.

    The new file is written only after every check passed. `only_changed` needs an old file."""
    checksums = _checksums()
    digests = compute_digests(folder)
    if not digests:
        raise ReleaseChecksumError(f"{folder} has no release files.")
    if version is not None:
        missing = [n for n in checksums.expected_assets(version, with_exe=with_exe) if n not in digests]
        if missing:
            raise ReleaseChecksumError(f"{folder} lacks the expected downloads: {', '.join(missing)}")
    old = read_sums(folder)
    changes: list[str] = []
    if old is not None:
        changes = describe_changes(old, digests)
    if only_changed:
        if old is None:
            raise ReleaseChecksumError(f"There is no old {checksums.SUMS_NAME} to compare with.")
        unexpected = [
            line for line in changes if line.split(":", 1)[0] not in set(only_changed)
        ]
        if unexpected:
            raise ReleaseChecksumError(
                "Files changed that must not have changed since the build: " + "; ".join(unexpected)
            )
    _atomic_write(folder / checksums.SUMS_NAME, render_sums(digests).encode("ascii"))
    return changes


def _atomic_write(path: Path, data: bytes) -> None:
    """Write `data` to a temporary file beside `path`, then replace `path` with it."""
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# Signed exe ----------------------------------------------------------------------------------


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def pe_signature_fields(data: bytes) -> tuple[int, int]:
    """File offsets of the PE CheckSum field and of the security directory entry (8 bytes:
    offset and size of the certificate table). Raises ReleaseChecksumError for a non PE file."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ReleaseChecksumError("the file is not a Windows program (no MZ header)")
    pe_offset = _u32(data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ReleaseChecksumError("the file is not a Windows program (no PE header)")
    optional = pe_offset + 24
    magic = _u16(data, optional)
    if magic == PE32_MAGIC:
        directories = optional + 96
    elif magic == PE32_PLUS_MAGIC:
        directories = optional + 112
    else:
        raise ReleaseChecksumError(f"unknown PE optional header magic 0x{magic:X}")
    count = _u32(data, directories - 4)
    if count <= SECURITY_DIRECTORY_INDEX:
        raise ReleaseChecksumError("the program has no security data directory")
    entry = directories + 8 * SECURITY_DIRECTORY_INDEX
    if entry + 8 > len(data):
        raise ReleaseChecksumError("the PE headers run past the end of the file")
    return optional + PE_CHECKSUM_OFFSET, entry


def only_signature_added(unsigned: bytes, signed: bytes) -> list[str]:
    """Why `signed` is not `unsigned` plus an Authenticode signature (empty list: it is).

    Signing appends the certificate table to the file (padded to 8 bytes) and changes two header
    fields, the CheckSum and the security directory entry. Every other byte must be the same."""
    try:
        checksum_at, entry_at = pe_signature_fields(unsigned)
    except ReleaseChecksumError as exc:
        return [f"unsigned exe: {exc}"]
    try:
        signed_checksum_at, signed_entry_at = pe_signature_fields(signed)
    except ReleaseChecksumError as exc:
        return [f"signed exe: {exc}"]
    problems: list[str] = []
    if (checksum_at, entry_at) != (signed_checksum_at, signed_entry_at):
        return ["the PE headers of the two files have a different layout"]
    if _u32(unsigned, entry_at) or _u32(unsigned, entry_at + 4):
        problems.append("the built exe is already signed, so it is not the file that was sent")
    offset, size = _u32(signed, entry_at), _u32(signed, entry_at + 4)
    if not offset or not size:
        return [*problems, "the exe carries no signature"]
    if offset < len(unsigned):
        problems.append(f"the certificate table starts inside the program (offset {offset})")
    if offset % CERT_TABLE_ALIGNMENT:
        problems.append(f"the certificate table is not 8 byte aligned (offset {offset})")
    if offset + size != len(signed):
        problems.append("the certificate table does not end the file")
    if problems:
        return problems
    mask = (slice(checksum_at, checksum_at + 4), slice(entry_at, entry_at + 8))
    expected = bytearray(unsigned)
    actual = bytearray(signed[: len(unsigned)])
    for part in mask:
        expected[part] = bytes(len(expected[part]))
        actual[part] = bytes(len(actual[part]))
    if expected != actual:
        first = next(i for i, (a, b) in enumerate(zip(expected, actual, strict=True)) if a != b)
        problems.append(f"the program bytes differ from the build at offset {first}")
    if any(signed[len(unsigned) : offset]):
        problems.append("the padding before the certificate table is not zero")
    return problems


def replace_exe(folder: Path, signed_exe: Path, version: str) -> None:
    """Replace the unsigned setup exe in `folder` with `signed_exe` after the checks."""
    name = EXE_NAME_PATTERN.format(version=version)
    if signed_exe.name != name:
        raise ReleaseChecksumError(f"The signed file is called {signed_exe.name}, expected {name}.")
    target = folder / name
    for label, path in (("signed exe", signed_exe), ("unsigned exe", target)):
        if path.is_symlink() or not path.is_file():
            raise ReleaseChecksumError(f"The {label} {path} is not a plain file.")
    problems = only_signature_added(target.read_bytes(), signed_exe.read_bytes())
    if problems:
        raise ReleaseChecksumError(
            f"{name} from the signing service is not the exe that was built: " + "; ".join(problems)
        )
    _atomic_write(target, signed_exe.read_bytes())


# Command line --------------------------------------------------------------------------------


def _clean_version(text: str) -> str:
    return text[1:] if text.startswith("v") else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release folder helper for the signed exe.")
    commands = parser.add_subparsers(dest="command", required=True)
    replace = commands.add_parser("replace-exe", help="swap in the signed setup exe")
    replace.add_argument("folder", type=Path)
    replace.add_argument("signed_exe", type=Path)
    replace.add_argument("--version", required=True)
    write = commands.add_parser("write", help="rewrite SHA256SUMS.txt over the folder")
    write.add_argument("folder", type=Path)
    write.add_argument("--version", help="the three downloads of this version must be present")
    write.add_argument("--no-exe", action="store_true", help="do not require the Setup exe")
    write.add_argument(
        "--only-changed",
        action="append",
        default=[],
        metavar="NAME",
        help="fail if a file other than NAME (repeatable) differs from the old SHA256SUMS.txt",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "replace-exe":
            version = _clean_version(args.version)
            replace_exe(args.folder, args.signed_exe, version)
            print(f"release_checksums: {EXE_NAME_PATTERN.format(version=version)} replaced")
            return 0
        version = _clean_version(args.version) if args.version else None
        changes = write_sums(
            args.folder,
            version,
            with_exe=not args.no_exe,
            only_changed=tuple(args.only_changed),
        )
    except ReleaseChecksumError as exc:
        print(f"release_checksums: {exc}", file=sys.stderr)
        return 1
    for line in changes:
        print(f"release_checksums: {line}")
    problems = _checksums().verify(args.folder, version, with_exe=not args.no_exe)
    for problem in problems:
        print(f"release_checksums: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"release_checksums: {_checksums().SUMS_NAME} written for {args.folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
