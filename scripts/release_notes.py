"""Write the GitHub release notes of one version (stdlib only).

The body is the section of CHANGELOG.md for that version, followed by a fixed footer with the list
of downloads, the install steps, how to verify a download and the legal note.

    python scripts/release_notes.py v0.1.0 [--changelog CHANGELOG.md] [--out FILE]

Without --out the notes go to standard output. Exit code 1 with a message on standard error when the
changelog or the section is missing, or when the section is empty. A leading `v` on the version is
ignored. Headings must look like `## [0.1.0] - 2026-10-14` (Keep a Changelog); `## [Unreleased]` is
never used. Link definitions at the very end of a section (the block at the bottom of the file) are
dropped, so write links inside the text as `[text](https://...)`.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANGELOG = ROOT / "CHANGELOG.md"
REPOSITORY_URL = "https://github.com/KAJPER/lcsc2fusion"
# GitHub anchors of the README sections that the footer links to (the heading text, lower case,
# spaces as hyphens). tests/test_release_notes.py checks them against the real README headings.
CODE_SIGNING_ANCHOR = "code-signing-policy"
PRIVACY_ANCHOR = "privacy"

# Semantic version with an optional pre-release part (`0.2.0-rc1`) and build metadata.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:\+[0-9A-Za-z.-]+)?$")
HEADING_RE = re.compile(r"^## \[(?P<name>[^\]]+)\](?P<rest>.*)$")
ANY_H2_RE = re.compile(r"^## ")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
LINK_DEFINITION_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+")


class ReleaseNotesError(Exception):
    """The notes cannot be made. The message says what to fix."""


def normalise_version(raw: str) -> str:
    """`v0.2.0-rc1` and `0.2.0-rc1` both give `0.2.0-rc1`; anything that is not a version raises."""
    version = raw.strip()
    if version[:1] in ("v", "V"):
        version = version[1:]
    if not VERSION_RE.match(version):
        raise ReleaseNotesError(
            f"{raw!r} is not a version like v1.2.3 or v1.2.3-rc1 (Unreleased has no release notes)"
        )
    return version


def is_prerelease(version: str) -> bool:
    """A hyphen after the numbers (`0.2.0-rc1`) marks a pre-release, as in the release workflow."""
    return "-" in version.split("+", 1)[0]


def _lines(text: str) -> list[str]:
    """The lines of a text with any of LF, CRLF or CR as the line break, without a BOM."""
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _headings(lines: list[str]) -> list[tuple[int, str]]:
    """Line index and version name of every `## [name]` heading outside fenced code blocks."""
    found: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            found.append((index, match.group("name").strip()))
    return found


def _next_h2(lines: list[str], start: int) -> int:
    """Index of the first level 2 heading after `start`, or the number of lines."""
    in_fence = False
    for index in range(start, len(lines)):
        if FENCE_RE.match(lines[index]):
            in_fence = not in_fence
        elif not in_fence and ANY_H2_RE.match(lines[index]):
            return index
    return len(lines)


def _strip_trailing_links(lines: list[str]) -> list[str]:
    """Drop blank lines and link definitions (`[0.1.0]: https://...`) from the end."""
    end = len(lines)
    while end > 0 and (not lines[end - 1].strip() or LINK_DEFINITION_RE.match(lines[end - 1])):
        end -= 1
    return lines[:end]


def extract_section(changelog: str, version: str) -> str:
    """The Markdown body of the changelog section of `version`, without its heading."""
    wanted = normalise_version(version)
    lines = _lines(changelog)
    matches = [
        index
        for index, name in _headings(lines)
        if name.lower() != "unreleased" and name.removeprefix("v").removeprefix("V") == wanted
    ]
    if not matches:
        raise ReleaseNotesError(
            f"CHANGELOG.md has no section '## [{wanted}]'. Add it (Keep a Changelog style) and "
            "move the entries from [Unreleased] there before tagging."
        )
    if len(matches) > 1:
        raise ReleaseNotesError(f"CHANGELOG.md has {len(matches)} sections for {wanted}; keep one.")
    start = matches[0] + 1
    body = _strip_trailing_links(lines[start : _next_h2(lines, start)])
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise ReleaseNotesError(f"The CHANGELOG.md section for {wanted} is empty.")
    return text


def footer(version: str) -> str:
    """The fixed part of every release: downloads, install, verification, warnings, legal."""
    exe = f"LCSC2Fusion-Setup-{version}.exe"
    installer = f"LCSC2Fusion-{version}-installer.zip"
    addin = f"LCSC2Fusion-{version}-addin.zip"
    policy_url = f"{REPOSITORY_URL}#{CODE_SIGNING_ANCHOR}"
    privacy_url = f"{REPOSITORY_URL}#{PRIVACY_ANCHOR}"
    parts = [
        "## Downloads",
        "",
        "LCSC2Fusion is an open source (MIT) add-in for Autodesk Fusion Electronics: it searches "
        "LCSC parts and puts their symbol, footprint and optional 3D model into your Fusion "
        "library.",
        "",
        "Most people need only the first file.",
        "",
        "| File | What it is |",
        "| --- | --- |",
        f"| `{exe}` | Windows installer in one file. Recommended. |",
        f"| `{installer}` | The same installer as readable scripts: unzip it and double click "
        "`Install-LCSC2Fusion.bat`. Use it if the exe is blocked on your computer. |",
        f"| `{addin}` | The plain add-in folder for a manual install. Also the only option on "
        "macOS, which is untested. |",
        "| `SHA256SUMS.txt` | SHA-256 checksums of the files above. |",
        "",
        "## Install (Windows)",
        "",
        "1. Download the installer and run it. It needs no administrator rights, no network and no "
        "Python. It copies the add-in to `%APPDATA%\\Autodesk\\FusionAddins\\LCSC2Fusion`.",
        "2. Start Fusion. The add-in runs on startup. If Fusion was already running, press "
        "Shift+S, open Add-Ins, select LCSC2Fusion and press Stop, then Run.",
        "3. To update, run the newer installer. To uninstall, run `Uninstall-LCSC2Fusion.bat` from "
        "the installer zip, or delete the folder above. Your data in `%LOCALAPPDATA%\\LCSC2Fusion` "
        "stays until you delete it.",
        "",
        "## Windows warnings",
        "",
        'The installer is not code signed yet, so Windows SmartScreen may say "unknown publisher": '
        'choose "More info", then "Run anyway". Some antivirus programs are also wary of self '
        "extracting exe files. The installer zip holds the same steps as plain scripts that you "
        "can read first.",
        "",
        f"The [Code signing policy]({policy_url}) in the README says what the status of code "
        "signing is, who signs and approves releases, and how to check a signature. What the "
        f"program sends over the network is listed under [Privacy]({privacy_url}).",
        "",
        "## Verify a download",
        "",
        "```powershell",
        f"Get-FileHash .\\{exe} -Algorithm SHA256",
        "```",
        "",
        "The hash must equal the line for that file in `SHA256SUMS.txt`.",
        "",
        "## Legal",
        "",
        "Unofficial tool, not affiliated with Autodesk, EasyEDA, JLCPCB or LCSC. The part data "
        "belongs to JLCEDA/EasyEDA and is for personal use only. See the "
        f"[README]({REPOSITORY_URL}#limitations-and-legal) for details.",
    ]
    if is_prerelease(version):
        parts[0:0] = ["This is a pre-release for testing.", ""]
    return "\n".join(parts)


def build_notes(changelog: str, version: str) -> str:
    """The whole release body for `version` (a leading `v` is fine), ending with a newline."""
    normalised = normalise_version(version)
    return extract_section(changelog, normalised) + "\n\n" + footer(normalised) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the GitHub release notes of one version from CHANGELOG.md."
    )
    parser.add_argument("version", help="version or tag, for example v0.1.0 or 0.2.0-rc1")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=DEFAULT_CHANGELOG,
        help="changelog file (default: CHANGELOG.md in the repository root)",
    )
    parser.add_argument("--out", type=Path, help="write the notes to this file, not to stdout")
    args = parser.parse_args(argv)
    try:
        try:
            changelog = args.changelog.read_text(encoding="utf-8-sig")
        except OSError as error:
            raise ReleaseNotesError(f"Cannot read {args.changelog}: {error}") from error
        notes = build_notes(changelog, args.version)
        if args.out is None:
            with contextlib.suppress(AttributeError, ValueError, OSError):
                sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
            sys.stdout.write(notes)
        else:
            try:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(notes, encoding="utf-8", newline="\n")
            except OSError as error:
                raise ReleaseNotesError(f"Cannot write {args.out}: {error}") from error
    except ReleaseNotesError as error:
        print(f"release_notes: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
