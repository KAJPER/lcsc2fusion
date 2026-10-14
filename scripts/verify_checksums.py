"""Check a release folder against its SHA256SUMS.txt (stdlib only).

    python scripts/verify_checksums.py DIR [--version 0.1.0] [--no-exe]

Every line of `DIR/SHA256SUMS.txt` (`<64 hex digits>  <file name>`, a `*` before the name is
allowed) must name a plain file in DIR whose SHA-256 matches, and every other file in DIR must be
listed. With --version the three downloads of that release must be there too:
`LCSC2Fusion-<v>-addin.zip`, `LCSC2Fusion-<v>-installer.zip` and `LCSC2Fusion-Setup-<v>.exe`
(--no-exe skips the exe, which is only built on Windows). Exit code 0 when everything is fine, 1
with one line per problem otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

SUMS_NAME = "SHA256SUMS.txt"
LINE_RE = re.compile(r"^(?P<digest>[0-9A-Fa-f]{64})[ \t]+\*?(?P<name>\S.*?)\s*$")
CHUNK = 1024 * 1024


def expected_assets(version: str, *, with_exe: bool = True) -> list[str]:
    """The download names of a release, as `scripts/build_release.py` produces them."""
    names = [f"LCSC2Fusion-{version}-addin.zip", f"LCSC2Fusion-{version}-installer.zip"]
    if with_exe:
        names.append(f"LCSC2Fusion-Setup-{version}.exe")
    return names


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(folder: Path, version: str | None = None, *, with_exe: bool = True) -> list[str]:
    """Problems found in `folder` (an empty list means the release folder is consistent)."""
    sums = folder / SUMS_NAME
    if not sums.is_file():
        return [f"{SUMS_NAME} is missing in {folder}"]
    problems: list[str] = []
    listed: dict[str, str] = {}
    text = sums.read_text(encoding="utf-8-sig")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        match = LINE_RE.match(line)
        if match is None:
            problems.append(f"{SUMS_NAME} line {number} is not '<sha256>  <file>': {line!r}")
            continue
        name = match.group("name")
        if "/" in name or "\\" in name or name in (".", ".."):
            problems.append(f"{SUMS_NAME} line {number}: {name!r} is not a plain file name")
            continue
        if name in listed:
            problems.append(f"{SUMS_NAME} lists {name} twice")
            continue
        listed[name] = match.group("digest").lower()
    if not listed:
        problems.append(f"{SUMS_NAME} lists no files")
    for name, digest in listed.items():
        path = folder / name
        if not path.is_file():
            problems.append(f"{name} is listed in {SUMS_NAME} but missing")
        elif sha256_of(path) != digest:
            problems.append(f"{name}: checksum does not match {SUMS_NAME}")
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.name != SUMS_NAME and path.name not in listed:
            problems.append(f"{path.name} is not listed in {SUMS_NAME}")
    if version is not None:
        for name in expected_assets(version, with_exe=with_exe):
            if name not in listed:
                problems.append(f"expected download {name} is not in {SUMS_NAME}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a release folder against SHA256SUMS.txt.")
    parser.add_argument("folder", type=Path, help="folder with the release files")
    parser.add_argument("--version", help="release version whose three downloads must be present")
    parser.add_argument("--no-exe", action="store_true", help="do not require the Setup exe")
    args = parser.parse_args(argv)
    version = args.version[1:] if args.version and args.version.startswith("v") else args.version
    if not args.folder.is_dir():
        print(f"verify_checksums: {args.folder} is not a folder", file=sys.stderr)
        return 1
    problems = verify(args.folder, version, with_exe=not args.no_exe)
    for problem in problems:
        print(f"verify_checksums: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"verify_checksums: {args.folder} is consistent with {SUMS_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
