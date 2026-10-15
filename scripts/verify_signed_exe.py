r"""Check the Authenticode signature of the setup exe with Windows itself (Windows, stdlib only).

    python scripts/verify_signed_exe.py EXE [--mode release|test] [--expect-subject-env NAME]

Windows (Get-AuthenticodeSignature, called through the PowerShell helper of scripts/sign_release.py,
the code that `sign_release.py verify` uses and that tests/test_signing.py covers) must say:

* the file carries an embedded Authenticode signature with a SHA-256 digest and a timestamp;
* the status is `Valid` (mode `release`). Mode `test` is for the test signing policy of a
  dry run: its certificate is issued by a test root that Windows does not trust, so a chain that
  fails only because of an untrusted root is accepted. A changed file (`HashMismatch`) or an
  unsigned file never is;
* in mode `release`, when the environment variable named by --expect-subject-env is set and not
  empty, its text must occur in the signer certificate subject (case does not matter). The value
  is read from the environment so that no quoting of a subject can go wrong.

The signer subject, the thumbprint and the timestamp are printed in both modes, so the first real
run shows what to put into the variable. Exit code 0 when everything holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType
from typing import Any

MODES = ("release", "test")


class VerifyError(Exception):
    """The signature cannot be checked. The message says what to fix."""


def _load_sign_release() -> ModuleType:
    """scripts/sign_release.py, loaded by path (once, shared with build_release.py)."""
    key = "lcsc2fusion_script_sign_release"
    loaded = sys.modules.get(key)
    if loaded is not None:
        return loaded
    spec = importlib_util.spec_from_file_location(key, Path(__file__).with_name("sign_release.py"))
    if spec is None or spec.loader is None:
        raise VerifyError("Cannot load scripts/sign_release.py.")
    module = importlib_util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def subject_contains(subject: str, wanted: str) -> bool:
    """True when `wanted` (not empty) occurs in `subject`, ignoring case."""
    return bool(wanted.strip()) and wanted.strip().casefold() in subject.casefold()


def verify_exe(
    path: Path,
    *,
    mode: str = "release",
    subject_pin: str | None = None,
    require_timestamp: bool = True,
    runner: Any = None,
) -> tuple[Any, list[str]]:
    """(what Windows says about the file, the problems found). No problems means acceptable."""
    if mode not in MODES:
        raise VerifyError(f"mode must be one of {', '.join(MODES)}.")
    sign_release = _load_sign_release()
    try:
        info = sign_release.verify_signatures([path], runner=runner)[0]
    except sign_release.SigningError as exc:
        raise VerifyError(str(exc)) from exc
    policy = sign_release.VerifyPolicy(
        allow_untrusted_root=(mode == "test"), require_timestamp=require_timestamp
    )
    problems: list[str] = list(sign_release.check_signature(info, policy))
    if mode == "release" and subject_pin:
        if not info.signer_subject:
            problems.append(f"{info.label}: there is no signer certificate to compare with")
        elif not subject_contains(info.signer_subject, subject_pin):
            problems.append(
                f"{info.label}: the signer is {info.signer_subject!r}, which does not contain "
                f"the expected text {subject_pin!r}"
            )
    return info, problems


def describe(info: Any, mode: str, subject_pin: str | None) -> str:
    sign_release = _load_sign_release()
    digest = sign_release.DIGEST_NAMES.get(info.digest_oid or "", "unreadable")
    if mode != "release":
        pin = "not applied in test mode"
    else:
        pin = f"must contain {subject_pin!r}" if subject_pin else "not set (nothing is pinned)"
    return "\n".join(
        [
            f"File:       {info.label}",
            f"Mode:       {mode}",
            f"Status:     {info.status} (type {info.signature_type})",
            f"Signer:     {info.signer_subject or '(none)'}",
            f"Thumbprint: {info.signer_thumbprint or '(none)'}",
            f"Digest:     {digest}",
            f"Timestamp:  {'yes' if info.has_timestamp else 'NO'}",
            f"Subject check: {pin}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Authenticode signature of an exe.")
    parser.add_argument("exe", type=Path)
    parser.add_argument("--mode", choices=MODES, default="release")
    parser.add_argument(
        "--expect-subject-env",
        metavar="NAME",
        help="environment variable with a text the signer subject must contain (release mode)",
    )
    parser.add_argument(
        "--allow-no-timestamp",
        action="store_true",
        help="TESTS ONLY: accept a signature without a timestamp",
    )
    args = parser.parse_args(argv)
    pin = os.environ.get(args.expect_subject_env, "").strip() if args.expect_subject_env else ""
    if not args.exe.is_file():
        print(f"verify_signed_exe: {args.exe} is not a file", file=sys.stderr)
        return 1
    try:
        info, problems = verify_exe(
            args.exe,
            mode=args.mode,
            subject_pin=pin or None,
            require_timestamp=not args.allow_no_timestamp,
        )
    except VerifyError as exc:
        print(f"verify_signed_exe: {exc}", file=sys.stderr)
        return 1
    print(describe(info, args.mode, pin or None))
    for problem in problems:
        print(f"verify_signed_exe: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
