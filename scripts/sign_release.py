r"""Authenticode signing of the release files and publishing them (Windows, stdlib only).

    python scripts/sign_release.py verify DIR [--expect-subject TEXT] [--expect-thumbprint HEX]
                                       [--allow-untrusted-root] [--allow-no-timestamp]
    python scripts/sign_release.py upload TAG DIR [--repo OWNER/REPO] [--dry-run] [--yes]
                                       [--expect-subject TEXT] [--expect-thumbprint HEX]

The signing itself runs inside `python scripts/build_release.py --sign ...`, which imports this
module (RELEASING.md, section "Code signing", explains the whole flow). This module

* picks the certificate from the CurrentUser\My store by thumbprint or by exact subject, or reads
  a PFX file whose password comes from the environment variable LCSC2FUSION_SIGN_PFX_PASSWORD
  and from nowhere else (the PFX is loaded in memory only, never imported into a store);
* checks that the certificate has the Code Signing purpose, a private key and a valid period;
* signs with signtool.exe (SHA-256, RFC 3161 timestamp) or with the PowerShell cmdlet
  Set-AuthenticodeSignature and verifies every signature right after signing;
* `verify` checks a release folder: the setup exe and the .ps1 files inside the installer zip;
* `upload` replaces the four assets of an existing GitHub release (`gh release upload --clobber`)
  after `verify` and `verify_checksums.py` passed. It never creates or deletes a release or a tag.

Secrets: the PFX password is never put on a command line, never printed and removed from any
text that comes back from a child process. Private keys never leave the certificate store or the
token that holds them. Only Windows can sign or verify (the Authenticode APIs are Windows only);
the certificate selection logic and the `upload` logic run anywhere.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "KAJPER/lcsc2fusion"

CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"
SHA256_OID = "2.16.840.1.101.3.4.2.1"
DIGEST_NAMES = {
    "1.2.840.113549.2.5": "md5",
    "1.3.14.3.2.26": "sha1",
    SHA256_OID: "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}

PFX_PASSWORD_ENV = "LCSC2FUSION_SIGN_PFX_PASSWORD"
SPEC_ENV = "LCSC2FUSION_SIGN_SPEC"  # JSON input of a PowerShell helper
OUT_ENV = "LCSC2FUSION_SIGN_OUT"  # file the helper writes its JSON result to

# RFC 3161 timestamp servers, tried in this order. DigiCert first: free, no account, answers on
# plain http, very widely used, and independent of the CA that issued the certificate (a timestamp
# from another company is normal and equally valid). The others are fallbacks. The list is only
# a default: --timestamp-url (repeatable) replaces it.
DEFAULT_TIMESTAMP_URLS = (
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
    "http://time.certum.pl",
    "http://timestamp.globalsign.com/tsa/r6advanced1",
)
TIMESTAMP_ROUNDS = 2  # the whole list is tried this many times
TIMESTAMP_PAUSE_SECONDS = 3.0  # between two rounds
SIGNTOOL_TIMEOUT_SECONDS = 300.0  # a token that asks for a PIN needs time
POWERSHELL_TIMEOUT_SECONDS = 300.0
GH_TIMEOUT_SECONDS = 900.0
UTF8_BOM = b"\xef\xbb\xbf"  # Windows PowerShell 5.1 reads a .ps1 without it as ANSI

BACKENDS = ("auto", "signtool", "powershell")
SCRIPT_SUFFIX = ".ps1"
REQUIRED_SCRIPTS = ("install.ps1", "uninstall.ps1")
TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?")
REPO_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
EXE_PATTERN = re.compile(r"^LCSC2Fusion-Setup-(?P<version>.+)\.exe$")
ZIP_PATTERN = re.compile(r"^LCSC2Fusion-(?P<version>.+)-(?:installer|addin)\.zip$")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVISIBLE_MARKS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202c\ufeff"))  # pasted from certmgr


class SigningError(Exception):
    """Signing, verifying or publishing cannot go on. The message says what to fix."""


class TimestampError(SigningError):
    """The timestamp server did not answer (or answered with something unusable)."""


@dataclass(frozen=True)
class CertInfo:
    """What the Windows certificate store tells about a certificate. No key material."""

    thumbprint: str
    subject: str
    issuer: str
    not_before: datetime
    not_after: datetime
    has_private_key: bool
    ekus: tuple[str, ...]


@dataclass(frozen=True)
class SigningConfig:
    thumbprint: str | None = None
    subject: str | None = None
    pfx: Path | None = None
    timestamp_urls: tuple[str, ...] = DEFAULT_TIMESTAMP_URLS
    timestamp: bool = True
    backend: str = "auto"
    allow_untrusted_root: bool = False  # a self signed test certificate never becomes "Valid"

    def validated(self) -> SigningConfig:
        """A normalised copy, or a SigningError that names the wrong option."""
        chosen = [x for x in (self.thumbprint, self.subject, self.pfx) if x is not None]
        if len(chosen) != 1:
            raise SigningError(
                "Choose the certificate with exactly one of --sign-thumbprint, --sign-subject, "
                "--sign-pfx."
            )
        if self.backend not in BACKENDS:
            raise SigningError(f"--sign-backend must be one of {', '.join(BACKENDS)}.")
        thumbprint = self.thumbprint
        if thumbprint is not None:
            thumbprint = normalize_thumbprint(thumbprint)
        if self.subject is not None and not self.subject.strip():
            raise SigningError("--sign-subject is empty.")
        if self.pfx is not None:
            if self.backend == "signtool":
                raise SigningError(
                    "A PFX file is signed through PowerShell only: signtool would need the "
                    "password on its command line."
                )
            check_pfx_file(self.pfx)
        if self.timestamp and not self.timestamp_urls:
            raise SigningError("There is no timestamp server (use --no-timestamp for tests).")
        for url in self.timestamp_urls:
            check_timestamp_url(url)
        return replace(self, thumbprint=thumbprint)


@dataclass(frozen=True)
class SignatureInfo:
    """What Windows says about one signed (or unsigned) file."""

    label: str
    path: Path
    status: str
    signature_type: str
    signer_subject: str
    signer_thumbprint: str
    has_timestamp: bool
    digest_oid: str | None
    chain_flags: tuple[str, ...]


@dataclass(frozen=True)
class VerifyPolicy:
    expect_thumbprint: str | None = None
    expect_subject: str | None = None
    allow_untrusted_root: bool = False
    require_timestamp: bool = True


@dataclass(frozen=True)
class VerifyReport:
    infos: tuple[SignatureInfo, ...]
    problems: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


Runner = Callable[
    [Sequence[str], Mapping[str, str], float, str], "subprocess.CompletedProcess[str]"
]


# Small pure helpers --------------------------------------------------------------------------


def normalize_thumbprint(text: str) -> str:
    """40 uppercase hex digits. Spaces, colons and the invisible marks of certmgr are dropped."""
    cleaned = re.sub(r"[\s:]", "", text.translate(INVISIBLE_MARKS)).upper()
    if not re.fullmatch(r"[0-9A-F]{40}", cleaned):
        raise SigningError(
            f"{text!r} is not a certificate thumbprint: expected 40 hex digits (SHA-1). "
            r"List them with: Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert"
        )
    return cleaned


def parse_distinguished_name(subject: str) -> list[tuple[str, str]]:
    """`CN=A, O="B, C", C=PL` to [("CN", "A"), ("O", "B, C"), ("C", "PL")] (keys uppercase)."""
    pairs: list[tuple[str, str]] = []
    buffer: list[str] = []
    key: str | None = None
    quoted = False
    index = 0

    def finish() -> None:
        nonlocal key, buffer
        if key is not None:
            pairs.append((key.strip().upper(), "".join(buffer).strip()))
        key = None
        buffer = []

    while index < len(subject):
        char = subject[index]
        if quoted:
            if char == '"':
                if index + 1 < len(subject) and subject[index + 1] == '"':
                    buffer.append('"')
                    index += 1
                else:
                    quoted = False
            else:
                buffer.append(char)
        elif char == '"' and key is not None:
            quoted = True
        elif char == "\\" and index + 1 < len(subject):
            buffer.append(subject[index + 1])
            index += 1
        elif char == "=" and key is None:
            key = "".join(buffer)
            buffer = []
        elif char == ",":
            finish()
        else:
            buffer.append(char)
        index += 1
    finish()
    return pairs


def subject_matches(subject: str, text: str) -> bool:
    """Exact match (case does not matter) of the whole subject, of its CN or O value, or of
    `CN=value` / `O=value`. A part of a name never matches."""
    wanted = text.strip().casefold()
    if not wanted:
        return False
    if wanted == subject.strip().casefold():
        return True
    for key, value in parse_distinguished_name(subject):
        if key in ("CN", "O") and wanted in (value.casefold(), f"{key}={value}".casefold()):
            return True
    return False


def usability_problems(cert: CertInfo, now: datetime) -> list[str]:
    problems: list[str] = []
    if CODE_SIGNING_EKU not in cert.ekus:
        problems.append(f"it has no Code Signing purpose (EKU {CODE_SIGNING_EKU})")
    if not cert.has_private_key:
        problems.append(
            "there is no private key for it on this PC (is the smart card or the SimplySign "
            "app connected and logged in?)"
        )
    if now < cert.not_before:
        problems.append(f"it is not valid before {_day(cert.not_before)}")
    if now > cert.not_after:
        problems.append(f"it expired on {_day(cert.not_after)}")
    return problems


def _day(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def describe_certificate(cert: CertInfo) -> str:
    return f"{cert.thumbprint}  {cert.subject}  (valid until {_day(cert.not_after)})"


def select_certificate(
    certs: Sequence[CertInfo],
    *,
    thumbprint: str | None = None,
    subject: str | None = None,
    now: datetime | None = None,
) -> CertInfo:
    """The one usable certificate that matches. Zero or several matches are errors."""
    if (thumbprint is None) == (subject is None):
        raise SigningError("Give exactly one of a thumbprint and a subject.")
    moment = now if now is not None else datetime.now(UTC)
    if thumbprint is not None:
        wanted = normalize_thumbprint(thumbprint)
        matches = [c for c in certs if c.thumbprint.upper() == wanted]
        what = f"thumbprint {wanted}"
    else:
        assert subject is not None
        matches = [c for c in certs if subject_matches(c.subject, subject)]
        what = f"subject {subject!r}"
    if not matches:
        capable = [c for c in certs if CODE_SIGNING_EKU in c.ekus]
        if capable:
            hint = "Code signing certificates in the store:\n  " + "\n  ".join(
                describe_certificate(c) for c in capable[:10]
            )
        else:
            hint = (
                "The store holds no certificate with the Code Signing purpose. With a cloud or "
                "token certificate, start its app (for example SimplySign Desktop) and log in "
                "first."
            )
        raise SigningError(f"No certificate with {what} in Cert:\\CurrentUser\\My. {hint}")
    usable = [c for c in matches if not usability_problems(c, moment)]
    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1:
        listing = "\n  ".join(describe_certificate(c) for c in usable)
        raise SigningError(
            f"{len(usable)} certificates match {what}; select one with --sign-thumbprint:\n  "
            + listing
        )
    details = "; ".join(
        f"{c.thumbprint}: {', '.join(usability_problems(c, moment))}" for c in matches
    )
    raise SigningError(f"The certificate with {what} cannot be used for signing: {details}")


def check_timestamp_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or re.search(r"\s", url):
        raise SigningError(f"{url!r} is not an http(s) URL of a timestamp server.")
    if parts.username or parts.password:
        raise SigningError("A timestamp URL must not contain a user name or a password.")


def check_pfx_file(path: Path) -> None:
    if not path.is_file():
        raise SigningError(f"The PFX file {path.name} does not exist.")
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT.resolve()):
        raise SigningError(
            "The PFX file lies inside the repository folder. Keep a private key file outside "
            "the checkout so that it can never be committed or published by accident."
        )
    if not os.environ.get(PFX_PASSWORD_ENV):
        raise SigningError(
            f"The PFX password is read from the environment variable {PFX_PASSWORD_ENV} only. "
            "Set it in the current shell (never on a command line or in a file of the repo)."
        )


# Reading the signature out of a file (to know the digest algorithm) ---------------------------


def _der(data: bytes, position: int) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at `position`."""
    if position + 2 > len(data):
        raise ValueError("truncated element")
    tag = data[position]
    first = data[position + 1]
    start = position + 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or start + count > len(data):
            raise ValueError("unsupported length")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise ValueError("element runs past the end")
    return tag, start, end


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    items: list[tuple[int, int, int]] = []
    position = start
    while position < end:
        tag, content_start, content_end = _der(data, position)
        items.append((tag, content_start, content_end))
        position = content_end
    return items


def _decode_oid(content: bytes) -> str:
    if not content:
        raise ValueError("empty OID")
    parts = [str(content[0] // 40), str(content[0] % 40)]
    value = 0
    for byte in content[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value = 0
    return ".".join(parts)


def pkcs7_digest_oid(blob: bytes) -> str:
    """The digest algorithm OID of the first signer in a PKCS #7 SignedData blob."""
    _tag, start, end = _der(blob, 0)  # ContentInfo
    content_info = _children(blob, start, end)
    _tag, start, end = content_info[1]  # [0] EXPLICIT
    signed_data = _children(blob, *_der(blob, start)[1:])
    signer_set = next(c for c in reversed(signed_data) if c[0] == 0x31)
    signer_infos = _children(blob, signer_set[1], signer_set[2])
    signer = _children(blob, signer_infos[0][1], signer_infos[0][2])
    algorithm = _children(blob, signer[2][1], signer[2][2])
    tag, oid_start, oid_end = algorithm[0]
    if tag != 0x06:
        raise ValueError("no digest algorithm identifier")
    return _decode_oid(blob[oid_start:oid_end])


def _pe_signature(data: bytes) -> bytes | None:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe : pe + 4] != b"PE\0\0":
        return None
    optional = pe + 24
    magic = int.from_bytes(data[optional : optional + 2], "little")
    if magic == 0x10B:
        count_at, table = optional + 92, optional + 96
    elif magic == 0x20B:
        count_at, table = optional + 108, optional + 112
    else:
        return None
    if int.from_bytes(data[count_at : count_at + 4], "little") < 5:
        return None
    entry = table + 4 * 8  # the security directory
    offset = int.from_bytes(data[entry : entry + 4], "little")
    size = int.from_bytes(data[entry + 4 : entry + 8], "little")
    if offset == 0 or size < 8 or offset + size > len(data):
        return None
    length = int.from_bytes(data[offset : offset + 4], "little")
    kind = int.from_bytes(data[offset + 6 : offset + 8], "little")
    if kind != 2 or length < 8 or length > size:  # 2 = WIN_CERT_TYPE_PKCS_SIGNED_DATA
        return None
    return data[offset + 8 : offset + length]


_SCRIPT_BEGIN = "# SIG # Begin signature block"
_SCRIPT_END = "# SIG # End signature block"


def _script_signature(data: bytes) -> bytes | None:
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    try:
        begin = lines.index(_SCRIPT_BEGIN)
        end = lines.index(_SCRIPT_END, begin)
    except ValueError:
        return None
    payload = "".join(line[2:] for line in lines[begin + 1 : end] if line.startswith("# "))
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None


def signature_digest_oid(path: Path) -> str | None:
    """The digest algorithm named inside the signature of the file, or None if unreadable."""
    data = path.read_bytes()
    blob = _script_signature(data) if path.suffix.lower() == SCRIPT_SUFFIX else _pe_signature(data)
    if blob is None:
        return None
    try:
        return pkcs7_digest_oid(blob)
    except (ValueError, IndexError, StopIteration):
        return None


def check_signature(info: SignatureInfo, policy: VerifyPolicy) -> list[str]:
    """The reasons why a signature is not acceptable (empty list: fine)."""
    name = info.label
    if info.status == "NotSigned" or info.signature_type == "None":
        return [f"{name}: not signed"]
    problems: list[str] = []
    if info.signature_type != "Authenticode":
        problems.append(
            f"{name}: the signature type is {info.signature_type}, an embedded Authenticode "
            "signature is required"
        )
    if info.status == "Valid":
        pass
    elif info.status == "HashMismatch":
        problems.append(f"{name}: the file was changed after it was signed (hash mismatch)")
    elif (
        policy.allow_untrusted_root
        and info.status in ("UnknownError", "NotTrusted")
        and info.chain_flags == ("UntrustedRoot",)
    ):
        pass  # a self signed certificate, accepted on purpose
    else:
        flags = f" ({', '.join(info.chain_flags)})" if info.chain_flags else ""
        problems.append(
            f"{name}: Windows does not trust the signature, status {info.status}{flags}"
        )
    if policy.expect_thumbprint is not None:
        wanted = normalize_thumbprint(policy.expect_thumbprint)
        if info.signer_thumbprint.upper() != wanted:
            problems.append(
                f"{name}: signed by certificate {info.signer_thumbprint or '(none)'}, "
                f"expected {wanted}"
            )
    if policy.expect_subject is not None and not subject_matches(
        info.signer_subject, policy.expect_subject
    ):
        problems.append(
            f"{name}: signer is {info.signer_subject!r}, expected {policy.expect_subject!r}"
        )
    if info.digest_oid != SHA256_OID:
        shown = DIGEST_NAMES.get(info.digest_oid or "", info.digest_oid or "unreadable")
        problems.append(f"{name}: the signature digest is {shown}, sha256 is required")
    if policy.require_timestamp and not info.has_timestamp:
        problems.append(
            f"{name}: the signature has no timestamp, so it stops being valid when the "
            "certificate expires"
        )
    return problems


# Files that may be signed --------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    status = path.lstat()
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def ensure_plain_file_inside(path: Path, root: Path) -> None:
    """Refuse anything but a plain file below `root`: no other folder, no symbolic link, no
    junction or other reparse point on the way, no hard link (it would change another file)."""
    root_abs = Path(os.path.abspath(root))
    path_abs = Path(os.path.abspath(path))
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError:
        raise SigningError(f"Refusing to sign {path.name}: it is not inside {root_abs}.") from None
    current = root_abs
    for part in ("", *relative.parts):
        current = current / part if part else current
        try:
            link = _is_link(current)
        except OSError as exc:
            raise SigningError(
                f"Refusing to sign {path.name}: cannot read {current}: {exc}"
            ) from exc
        if link:
            raise SigningError(f"Refusing to sign {path.name}: {current} is a link.")
    status = path_abs.lstat()
    if not stat.S_ISREG(status.st_mode):
        raise SigningError(f"Refusing to sign {path.name}: it is not a regular file.")
    if status.st_nlink > 1:
        raise SigningError(f"Refusing to sign {path.name}: it has several hard links.")


# Child processes -----------------------------------------------------------------------------


def run_process(
    argv: Sequence[str], env: Mapping[str, str], timeout: float, encoding: str = "utf-8"
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="replace",
            env=dict(env),
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SigningError(f"{Path(argv[0]).name} did not finish within {timeout:.0f} s.") from exc
    except OSError as exc:
        raise SigningError(f"Cannot run {argv[0]}: {exc}") from exc


def child_env(*, with_password: bool = False) -> dict[str, str]:
    """The environment of a child process. The PFX password goes only to the one that needs it."""
    env = dict(os.environ)
    if not with_password:
        env.pop(PFX_PASSWORD_ENV, None)
    return env


def redact(text: str) -> str:
    secret = os.environ.get(PFX_PASSWORD_ENV)
    if secret:
        text = text.replace(secret, "***")
    return text


def tail(text: str, limit: int = 1500) -> str:
    text = redact(text).strip()
    return text if len(text) <= limit else text[:limit] + " ..."


def powershell_path() -> Path:
    """Windows PowerShell 5.1, which every supported Windows has."""
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return candidate
    found = shutil.which("powershell")
    if found:
        return Path(found)
    raise SigningError("Windows PowerShell (powershell.exe) was not found.")


def find_signtool(environ: Mapping[str, str] | None = None) -> Path | None:
    """signtool.exe from PATH, else the newest one of the Windows SDK (x64), else None."""
    env = os.environ if environ is None else environ
    on_path = shutil.which("signtool", path=env.get("PATH", ""))
    if on_path:
        return Path(on_path)
    found: list[tuple[tuple[int, ...], Path]] = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = env.get(variable)
        if not base:
            continue
        kit = Path(base) / "Windows Kits" / "10"
        for candidate in (kit / "bin").glob("*/x64/signtool.exe"):
            numbers = tuple(int(n) for n in re.findall(r"\d+", candidate.parent.parent.name))
            found.append((numbers, candidate))
        certification = kit / "App Certification Kit" / "signtool.exe"
        if certification.is_file():
            found.append(((0,), certification))
    found.sort(key=lambda item: item[0], reverse=True)
    return found[0][1] if found else None


# PowerShell helpers --------------------------------------------------------------------------

POWERSHELL_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Out-Result($Value) {
    $json = ConvertTo-Json -InputObject $Value -Compress -Depth 6
    [System.IO.File]::WriteAllText($env:LCSC2FUSION_SIGN_OUT, $json, (New-Object System.Text.UTF8Encoding($false)))
}
function Get-CertData($Cert) {
    $ekus = @()
    foreach ($extension in $Cert.Extensions) {
        if ($extension -is [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]) {
            foreach ($oid in $extension.EnhancedKeyUsages) { $ekus += [string]$oid.Value }
        }
    }
    return [ordered]@{
        thumbprint = [string]$Cert.Thumbprint
        subject = [string]$Cert.Subject
        issuer = [string]$Cert.Issuer
        not_before = ([DateTimeOffset]$Cert.NotBefore).ToUnixTimeSeconds()
        not_after = ([DateTimeOffset]$Cert.NotAfter).ToUnixTimeSeconds()
        has_private_key = [bool]$Cert.HasPrivateKey
        ekus = $ekus
    }
}
function Open-Pfx([string]$Path) {
    # In memory only: EphemeralKeySet (.NET 4.7.2 and later) writes no key file, the default
    # key set is the fallback and Reset() removes its temporary key again.
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::DefaultKeySet
    try { $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]'EphemeralKeySet' } catch { }
    return New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList $Path, $env:LCSC2FUSION_SIGN_PFX_PASSWORD, $flags
}
"""  # noqa: E501

LIST_BODY = r"""# step: list
$items = @()
foreach ($cert in (Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My')) { $items += ,(Get-CertData $cert) }
Out-Result @($items)
"""  # noqa: E501

PFX_BODY = r"""# step: pfx
$spec = $env:LCSC2FUSION_SIGN_SPEC | ConvertFrom-Json
$cert = Open-Pfx ([string]$spec.pfx)
try { Out-Result (Get-CertData $cert) } finally { $cert.Reset() }
"""

SIGN_BODY = r"""# step: sign
$spec = $env:LCSC2FUSION_SIGN_SPEC | ConvertFrom-Json
$cert = $null
try {
    if ($spec.pfx) { $cert = Open-Pfx ([string]$spec.pfx) }
    else { $cert = Get-Item -LiteralPath ('Cert:\CurrentUser\My\' + [string]$spec.thumbprint) }
    $params = @{ LiteralPath = [string]$spec.path; Certificate = $cert; HashAlgorithm = 'SHA256' }
    if ($spec.timestamp_url) { $params['TimestampServer'] = [string]$spec.timestamp_url }
    [void](Set-AuthenticodeSignature @params)
    # An unreachable timestamp server is no error for the cmdlet: the file stays signed without
    # a timestamp. Look at the result.
    $signature = Get-AuthenticodeSignature -LiteralPath ([string]$spec.path)
    $missing = ([bool]$spec.timestamp_url) -and ($null -eq $signature.TimeStamperCertificate)
    Out-Result ([ordered]@{ status = [string]$signature.Status; timestamp_missing = [bool]$missing; message = [string]$signature.StatusMessage })
}
finally { if ($spec.pfx -and $cert) { $cert.Reset() } }
"""  # noqa: E501

VERIFY_BODY = r"""# step: verify
$paths = $env:LCSC2FUSION_SIGN_SPEC | ConvertFrom-Json   # an array stays one object in 5.1
$results = @()
foreach ($path in $paths) {
    $signature = Get-AuthenticodeSignature -LiteralPath ([string]$path)
    $signer = $signature.SignerCertificate
    $subject = ''
    $thumbprint = ''
    $flags = @()
    if ($signer) {
        $subject = [string]$signer.Subject
        $thumbprint = [string]$signer.Thumbprint
        if ([string]$signature.Status -ne 'Valid') {
            $chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
            $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
            [void]$chain.Build($signer)
            foreach ($element in $chain.ChainStatus) { $flags += [string]$element.Status }
            $chain.Reset()
        }
    }
    $results += ,([ordered]@{
        path = [string]$path
        status = [string]$signature.Status
        signature_type = [string]$signature.SignatureType
        signer_subject = $subject
        signer_thumbprint = $thumbprint
        has_timestamp = [bool]($null -ne $signature.TimeStamperCertificate)
        chain_flags = $flags
    })
}
Out-Result @($results)
"""  # noqa: E501


def run_powershell(
    body: str,
    *,
    spec: object = None,
    with_password: bool = False,
    runner: Runner | None = None,
) -> object:
    """Run a helper script in Windows PowerShell 5.1 and return the JSON it wrote.

    The script is written to a temporary file (a plain, readable .ps1 and not an encoded command
    line, which security software dislikes) and started with -File. The input goes in as JSON in
    an environment variable and the result comes back through a file, so no quoting, console
    encoding, secret or error stream noise can get in the way."""
    script = (
        POWERSHELL_PRELUDE
        + "try {\n"
        + body
        + "\n} catch {\n    Out-Result ([ordered]@{ error = [string]$_.Exception.Message })\n"
        + "    exit 3\n}\n"
    )
    execute = runner if runner is not None else run_process
    with tempfile.TemporaryDirectory(prefix="lcsc2fusion-sign-") as temp:
        script_path = Path(temp) / "helper.ps1"
        script_path.write_bytes(UTF8_BOM + script.replace("\n", "\r\n").encode("utf-8"))
        result_path = Path(temp) / "result.json"
        env = child_env(with_password=with_password)
        env[OUT_ENV] = str(result_path)
        env[SPEC_ENV] = json.dumps(spec) if spec is not None else ""
        argv = [
            str(powershell_path()),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        done = execute(argv, env, POWERSHELL_TIMEOUT_SECONDS, "utf-8")
        text = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
    if not text.strip():
        raise SigningError(
            f"PowerShell returned no result (exit code {done.returncode}). "
            + tail(done.stderr or done.stdout)
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SigningError("PowerShell returned an unreadable result.") from exc
    if isinstance(data, dict) and set(data) == {"error"}:
        raise SigningError(tail(str(data["error"])))
    return data


def _cert_from_json(item: Mapping[str, object]) -> CertInfo:
    ekus = item.get("ekus") or []
    assert isinstance(ekus, list)
    return CertInfo(
        thumbprint=str(item["thumbprint"]).upper(),
        subject=str(item["subject"]),
        issuer=str(item.get("issuer", "")),
        not_before=datetime.fromtimestamp(int(str(item["not_before"])), UTC),
        not_after=datetime.fromtimestamp(int(str(item["not_after"])), UTC),
        has_private_key=bool(item["has_private_key"]),
        ekus=tuple(str(e) for e in ekus),
    )


def list_store_certificates(runner: Runner | None = None) -> list[CertInfo]:
    """Every certificate of Cert:\\CurrentUser\\My (public data only)."""
    data = run_powershell(LIST_BODY, runner=runner)
    if data is None:
        return []
    assert isinstance(data, list)
    return [_cert_from_json(item) for item in data]


def load_pfx_info(path: Path, runner: Runner | None = None) -> CertInfo:
    """Subject, thumbprint and so on of a PFX file. The password comes from the environment."""
    data = run_powershell(PFX_BODY, spec={"pfx": str(path)}, with_password=True, runner=runner)
    assert isinstance(data, dict)
    return _cert_from_json(data)


def load_certificate(
    config: SigningConfig, *, runner: Runner | None = None, now: datetime | None = None
) -> CertInfo:
    moment = now if now is not None else datetime.now(UTC)
    if config.pfx is not None:
        cert = load_pfx_info(config.pfx, runner)
        problems = usability_problems(cert, moment)
        if problems:
            raise SigningError(
                f"The certificate in the PFX file cannot sign: {'; '.join(problems)}"
            )
        return cert
    return select_certificate(
        list_store_certificates(runner),
        thumbprint=config.thumbprint,
        subject=config.subject,
        now=moment,
    )


def verify_signatures(
    paths: Sequence[Path], *, runner: Runner | None = None
) -> list[SignatureInfo]:
    """Ask Windows (Get-AuthenticodeSignature) about the files and read their digest algorithm."""
    if not paths:
        return []
    data = run_powershell(VERIFY_BODY, spec=[str(p) for p in paths], runner=runner)
    assert isinstance(data, list)
    if len(data) != len(paths):
        raise SigningError("Internal error: PowerShell returned a different number of results.")
    infos: list[SignatureInfo] = []
    for path, item in zip(paths, data, strict=True):
        flags = item.get("chain_flags") or []
        status = str(item["status"])
        digest = None if status == "NotSigned" else signature_digest_oid(path)
        infos.append(
            SignatureInfo(
                label=path.name,
                path=path,
                status=status,
                signature_type=str(item["signature_type"]),
                signer_subject=str(item["signer_subject"]),
                signer_thumbprint=str(item["signer_thumbprint"]).upper(),
                has_timestamp=bool(item["has_timestamp"]),
                digest_oid=digest,
                chain_flags=tuple(str(f) for f in flags),
            )
        )
    return infos


# Signing -------------------------------------------------------------------------------------


class Signer:
    """Signs files in a working folder and checks the result. `sign` is what build_release calls."""

    def __init__(
        self,
        config: SigningConfig,
        certificate: CertInfo,
        *,
        signtool: Path | None = None,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] | None = None,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.certificate = certificate
        self.signtool = signtool
        self.runner: Runner = runner if runner is not None else run_process
        self.sleep = sleep
        self.say = say if say is not None else lambda text: print(text, flush=True)
        self.warn = warn if warn is not None else lambda text: print(text, file=sys.stderr)
        self.signed: list[SignatureInfo] = []

    @property
    def policy(self) -> VerifyPolicy:
        return VerifyPolicy(
            expect_thumbprint=self.certificate.thumbprint,
            expect_subject=self.certificate.subject,
            allow_untrusted_root=self.config.allow_untrusted_root,
            require_timestamp=self.config.timestamp,
        )

    def announce(self) -> None:
        cert = self.certificate
        source = (
            f"PFX file {self.config.pfx.name}"
            if self.config.pfx is not None
            else r"Windows certificate store (Cert:\CurrentUser\My)"
        )
        tool = (
            f"signtool.exe ({self.signtool})"
            if self.signtool is not None and self.config.pfx is None
            else "PowerShell Set-AuthenticodeSignature"
        )
        stamp = (
            ", ".join(self.config.timestamp_urls)
            if self.config.timestamp
            else "NONE (--no-timestamp)"
        )
        self.say(
            "Signing certificate\n"
            f"  subject:    {cert.subject}\n"
            f"  thumbprint: {cert.thumbprint}\n"
            f"  issuer:     {cert.issuer}\n"
            f"  expires:    {_day(cert.not_after)}\n"
            f"  source:     {source}\n"
            f"  tool:       {tool}\n"
            f"  digest:     sha256\n"
            f"  timestamp:  {stamp}"
        )
        if not self.config.timestamp:
            self.warn(
                "WARNING: --no-timestamp. The signatures carry NO timestamp, so they stop being "
                f"valid when the certificate expires on {_day(cert.not_after)}. Use this for "
                "tests only and never publish files made this way."
            )
        if self.config.allow_untrusted_root:
            self.warn(
                "WARNING: --sign-allow-untrusted-root. A signature from a self signed "
                "certificate is accepted. Windows will still call these files untrusted."
            )

    def _backends(self, path: Path) -> list[str]:
        if self.config.pfx is not None or self.config.backend == "powershell":
            return ["powershell"]
        if self.config.backend == "signtool":
            return ["signtool"]
        if self.signtool is None:
            return ["powershell"]
        # signtool signs scripts through the PowerShell SIP; if that fails, the cmdlet may work
        return ["signtool", "powershell"] if path.suffix.lower() == SCRIPT_SUFFIX else ["signtool"]

    def sign(self, path: Path, root: Path) -> SignatureInfo:
        """Sign `path` (a plain file inside `root`), check the signature, return what Windows
        says. Any failure raises SigningError; a half signed file is never left as a result."""
        ensure_plain_file_inside(path, root)
        original = path.read_bytes()
        failures: list[str] = []
        for backend in self._backends(path):
            try:
                self._sign_with(backend, path, original)
            except TimestampError as exc:
                self._restore(path, original)
                raise SigningError(f"Could not sign {path.name}: {exc}") from exc
            except SigningError as exc:
                self._restore(path, original)
                failures.append(f"{backend}: {exc}")
                continue
            info = self._check(path)
            self.signed.append(info)
            stamp = "timestamp yes" if info.has_timestamp else "NO timestamp"
            self.say(f"  signed {path.name} with {backend}: {info.status}, sha256, {stamp}")
            return info
        raise SigningError(f"Could not sign {path.name}. " + " | ".join(failures))

    @staticmethod
    def _restore(path: Path, original: bytes) -> None:
        if path.read_bytes() != original:
            path.write_bytes(original)

    def _sign_with(self, backend: str, path: Path, original: bytes) -> None:
        timestamping = self.config.timestamp
        urls: Sequence[str | None] = self.config.timestamp_urls if timestamping else (None,)
        rounds = TIMESTAMP_ROUNDS if timestamping else 1
        problems: list[str] = []
        for number in range(rounds):
            for url in urls:
                try:
                    self._attempt(backend, path, url)
                    return
                except TimestampError as exc:
                    self._restore(path, original)
                    problems.append(f"{url}: {exc}")
            if number + 1 < rounds:
                self.sleep(TIMESTAMP_PAUSE_SECONDS)
        raise TimestampError("no timestamp server answered (" + "; ".join(problems) + ")")

    def _attempt(self, backend: str, path: Path, url: str | None) -> None:
        if backend == "signtool":
            self._attempt_signtool(path, url)
        else:
            self._attempt_powershell(path, url)

    def _attempt_signtool(self, path: Path, url: str | None) -> None:
        assert self.signtool is not None
        argv = [
            str(self.signtool),
            "sign",
            "/fd",
            "sha256",
            "/s",
            "My",
            "/sha1",
            self.certificate.thumbprint,
        ]
        if url is not None:
            argv += ["/tr", url, "/td", "sha256"]
        argv.append(str(path))
        encoding = "oem" if sys.platform == "win32" else "utf-8"
        done = self.runner(argv, child_env(), SIGNTOOL_TIMEOUT_SECONDS, encoding)
        if done.returncode != 0:
            text = tail((done.stdout or "") + "\n" + (done.stderr or ""))
            message = f"signtool exit code {done.returncode}: {text}"
            if url is not None and "timestamp" in text.lower():
                raise TimestampError(message)
            raise SigningError(message)

    def _attempt_powershell(self, path: Path, url: str | None) -> None:
        spec = {
            "path": str(path),
            "thumbprint": self.certificate.thumbprint,
            "pfx": str(self.config.pfx) if self.config.pfx is not None else "",
            "timestamp_url": url or "",
        }
        data = run_powershell(
            SIGN_BODY, spec=spec, with_password=self.config.pfx is not None, runner=self.runner
        )
        assert isinstance(data, dict)
        if data.get("timestamp_missing"):
            raise TimestampError(f"no timestamp was added ({tail(str(data.get('message', '')))})")

    def _check(self, path: Path) -> SignatureInfo:
        info = verify_signatures([path], runner=self.runner)[0]
        problems = check_signature(info, self.policy)
        if problems:
            raise SigningError(
                f"{path.name} was signed but the check failed: " + "; ".join(problems)
            )
        return info


def create_signer(
    config: SigningConfig,
    *,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> Signer:
    """Validate the options, find the certificate and the tool, print what will be used."""
    if sys.platform != "win32":
        raise SigningError("Signing needs Windows (Authenticode, signtool and PowerShell).")
    checked = config.validated()
    signtool: Path | None = None
    if checked.pfx is None and checked.backend != "powershell":
        signtool = find_signtool()
        if checked.backend == "signtool" and signtool is None:
            raise SigningError(
                "signtool.exe was not found (PATH or Windows Kits\\10\\bin\\*\\x64). Install the "
                "Windows SDK signing tools or use --sign-backend powershell."
            )
    certificate = load_certificate(checked, runner=runner, now=now)
    signer = Signer(
        checked, certificate, signtool=signtool, runner=runner, sleep=sleep, say=say, warn=warn
    )
    signer.announce()
    return signer


# Verifying a release folder ------------------------------------------------------------------


def collect_targets(folder: Path, workdir: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """(label, path) of every signable file: the exe, and the .ps1 members of the installer zip
    (extracted into `workdir` under names that cannot collide). Plus the problems found."""
    targets: list[tuple[str, Path]] = []
    problems: list[str] = []
    for exe in sorted(folder.glob("LCSC2Fusion-Setup-*.exe")):
        targets.append((exe.name, exe))
    for archive in sorted(folder.glob("LCSC2Fusion-*-installer.zip")):
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = sorted(
                    info.filename
                    for info in bundle.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(SCRIPT_SUFFIX)
                )
                for required in REQUIRED_SCRIPTS:
                    if required not in members:
                        problems.append(f"{archive.name} contains no {required}")
                for index, member in enumerate(members):
                    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(member).name)
                    destination = workdir / f"{len(targets)}-{index}-{safe}"
                    destination.write_bytes(bundle.read(member))
                    targets.append((f"{archive.name}:{member}", destination))
        except (zipfile.BadZipFile, OSError) as exc:
            problems.append(f"{archive.name} cannot be read as a zip file: {exc}")
    return targets, problems


def verify_folder(
    folder: Path, policy: VerifyPolicy, *, runner: Runner | None = None
) -> VerifyReport:
    if sys.platform != "win32":
        raise SigningError("Verifying signatures needs Windows (Get-AuthenticodeSignature).")
    if not folder.is_dir():
        raise SigningError(f"{folder} is not a folder.")
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lcsc2fusion-verify-") as temp:
        targets, problems = collect_targets(folder, Path(temp))
        if not any(label.endswith(".exe") for label, _ in targets):
            notes.append("no setup exe in this folder")
        if not targets:
            problems.append(f"nothing to verify: no setup exe or installer zip in {folder}")
        results = verify_signatures([path for _, path in targets], runner=runner)
        infos = tuple(
            replace(result, label=label)
            for (label, _), result in zip(targets, results, strict=True)
        )
    for info in infos:
        problems.extend(check_signature(info, policy))
    signers = {info.signer_thumbprint for info in infos if info.signer_thumbprint}
    if len(signers) > 1:
        problems.append(
            "the files are signed by different certificates: " + ", ".join(sorted(signers))
        )
    return VerifyReport(infos=infos, problems=tuple(problems), notes=tuple(notes))


def format_report(report: VerifyReport) -> str:
    lines: list[str] = []
    if report.infos:
        width = max(len(info.label) for info in report.infos)
        lines.append(f"{'File':<{width}}  {'Status':<12} {'Digest':<7} Timestamp")
        for info in report.infos:
            digest = DIGEST_NAMES.get(info.digest_oid or "", "?")
            stamp = "yes" if info.has_timestamp else "NO"
            lines.append(f"{info.label:<{width}}  {info.status:<12} {digest:<7} {stamp}")
        seen: dict[str, str] = {}
        for info in report.infos:
            if info.signer_thumbprint:
                seen.setdefault(info.signer_thumbprint, info.signer_subject)
        for thumbprint, subject in seen.items():
            lines.append(f"Signer: {subject}")
            lines.append(f"Thumbprint: {thumbprint}")
    lines.extend(f"note: {note}" for note in report.notes)
    return "\n".join(lines)


# Publishing ----------------------------------------------------------------------------------


def _load_sibling(name: str) -> ModuleType:
    """scripts/ is not a package: load a sibling script by path (once)."""
    key = f"lcsc2fusion_script_{name}"
    loaded = sys.modules.get(key)
    if loaded is not None:
        return loaded
    spec = importlib_util.spec_from_file_location(key, Path(__file__).with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise SigningError(f"Cannot load scripts/{name}.py.")
    module = importlib_util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class GhRelease:
    tag: str
    is_draft: bool
    is_prerelease: bool
    url: str
    assets: dict[str, int]


def gh_release_view(gh: str, repo: str, tag: str, runner: Runner) -> GhRelease:
    argv = [
        gh,
        "release",
        "view",
        tag,
        "--repo",
        repo,
        "--json",
        "tagName,isDraft,isPrerelease,url,assets",
    ]
    done = runner(argv, _gh_env(), GH_TIMEOUT_SECONDS, "utf-8")
    if done.returncode != 0:
        raise SigningError(
            f"There is no readable release {tag} in {repo} (gh release view failed): "
            f"{tail(done.stderr or done.stdout)} The release must exist before its files can be "
            "replaced: push the tag and let the Release workflow publish it first."
        )
    try:
        data = json.loads(done.stdout)
        assets = {str(a["name"]): int(a["size"]) for a in data.get("assets") or []}
        return GhRelease(
            tag=str(data["tagName"]),
            is_draft=bool(data["isDraft"]),
            is_prerelease=bool(data.get("isPrerelease", False)),
            url=str(data.get("url", "")),
            assets=assets,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise SigningError(f"gh returned an unreadable description of {tag}: {exc!r}") from exc


def _gh_env() -> dict[str, str]:
    env = child_env()
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def release_file_names(version: str) -> list[str]:
    """The four assets of a release, SHA256SUMS.txt last (see verify_checksums.py)."""
    checksums = _load_sibling("verify_checksums")
    return [*checksums.expected_assets(version), checksums.SUMS_NAME]


def upload_release(
    tag: str,
    folder: Path,
    *,
    repo: str = DEFAULT_REPO,
    dry_run: bool = False,
    yes: bool = False,
    policy: VerifyPolicy | None = None,
    verifier: Callable[[Path, VerifyPolicy], VerifyReport] | None = None,
    runner: Runner | None = None,
    say: Callable[[str], None] | None = None,
) -> int:
    """Check the folder, then replace the assets of the existing release `tag` in `repo`."""
    out = say if say is not None else lambda text: print(text, flush=True)
    execute: Runner = runner if runner is not None else run_process
    if not TAG_PATTERN.fullmatch(tag):
        raise SigningError(f"{tag!r} is not a release tag like v1.2.3 or v1.2.3-rc1.")
    if not REPO_PATTERN.fullmatch(repo):
        raise SigningError(f"{repo!r} is not a repository like OWNER/REPO.")
    if not folder.is_dir():
        raise SigningError(f"{folder} is not a folder.")
    version = tag[1:]
    names = release_file_names(version)
    missing = [name for name in names if not (folder / name).is_file()]
    if missing:
        found = sorted(
            {
                m.group("version")
                for entry in folder.iterdir()
                for m in (EXE_PATTERN.match(entry.name), ZIP_PATTERN.match(entry.name))
                if m
            }
        )
        raise SigningError(
            f"The folder has no {', '.join(missing)}: the tag {tag} does not match the files in "
            f"{folder} (versions found in the file names: {', '.join(found) or 'none'})."
        )
    checksum_problems = _load_sibling("verify_checksums").verify(folder, version)
    if checksum_problems:
        raise SigningError(
            "SHA256SUMS.txt does not describe this folder:\n  " + "\n  ".join(checksum_problems)
        )
    check = verifier if verifier is not None else verify_folder
    report = check(folder, policy if policy is not None else VerifyPolicy())
    if not report.ok:
        raise SigningError(
            "The signatures are not acceptable, nothing was uploaded:\n  "
            + "\n  ".join(report.problems)
        )
    gh = shutil.which("gh")
    if gh is None:
        raise SigningError("The GitHub CLI (gh) was not found on PATH.")
    release = gh_release_view(gh, repo, tag, execute)
    if release.tag != tag:
        raise SigningError(f"gh returned the release {release.tag!r} for {tag!r}.")
    if release.is_draft:
        raise SigningError(
            f"The release {tag} is a draft. Files are replaced on a published release only: "
            "finish or delete the draft first."
        )
    paths = [str(folder / name) for name in names]
    command = [gh, "release", "upload", tag, *paths, "--clobber", "--repo", repo]
    out(f"Upload plan\n  repository: {repo}\n  release:    {tag} ({release.url})")
    if release.is_prerelease != ("-" in tag):
        out("  note: the pre-release flag of the release does not match the tag")
    for name in names:
        size = (folder / name).stat().st_size
        previous = release.assets.get(name)
        action = f"replaces the asset of {previous} bytes" if previous is not None else "new asset"
        digest = _load_sibling("verify_checksums").sha256_of(folder / name)
        out(f"  {name}\n    {size} bytes, sha256 {digest}, {action}")
    out("  command: " + " ".join(f'"{part}"' if " " in part else part for part in command))
    if dry_run:
        out("Dry run: nothing was uploaded.")
        return 0
    if not yes:
        raise SigningError("Nothing was uploaded. Repeat with --yes to do it, or --dry-run.")
    done = execute(command, _gh_env(), GH_TIMEOUT_SECONDS, "utf-8")
    if done.returncode != 0:
        raise SigningError(f"gh release upload failed: {tail(done.stderr or done.stdout)}")
    after = gh_release_view(gh, repo, tag, execute)
    wrong = [name for name in names if after.assets.get(name) != (folder / name).stat().st_size]
    if wrong:
        raise SigningError(
            "The upload finished but the release does not show the expected files: "
            + ", ".join(wrong)
        )
    out(f"Uploaded {len(names)} files to {tag}. Download one and run Get-AuthenticodeSignature.")
    return 0


# Command line --------------------------------------------------------------------------------


def add_signing_arguments(parser: argparse.ArgumentParser) -> None:
    """The certificate options shared by build_release.py --sign and this script."""
    group = parser.add_argument_group(
        "signing (Windows; used with --sign)",
        "Choose the certificate with exactly one of --sign-thumbprint, --sign-subject, "
        f"--sign-pfx. A PFX password is read from the environment variable {PFX_PASSWORD_ENV} "
        "only.",
    )
    group.add_argument(
        "--sign-thumbprint",
        metavar="HEX",
        help=r"SHA-1 thumbprint of a certificate in Cert:\CurrentUser\My",
    )
    group.add_argument(
        "--sign-subject",
        metavar="TEXT",
        help="exact subject, or exact CN or O value, of a certificate in the store",
    )
    group.add_argument("--sign-pfx", type=Path, metavar="PATH", help="PFX file outside the repo")
    group.add_argument(
        "--timestamp-url",
        action="append",
        metavar="URL",
        help="RFC 3161 server; repeat for fallbacks (default: "
        + ", ".join(DEFAULT_TIMESTAMP_URLS)
        + ")",
    )
    group.add_argument(
        "--no-timestamp",
        action="store_true",
        help="TESTS ONLY: sign without timestamp (the signature dies with the certificate)",
    )
    group.add_argument(
        "--sign-backend",
        choices=BACKENDS,
        default="auto",
        help="auto: signtool.exe when found, else PowerShell (a PFX always uses PowerShell)",
    )
    group.add_argument(
        "--sign-allow-untrusted-root",
        action="store_true",
        help="TESTS ONLY: accept a self signed certificate (its signature is never 'Valid')",
    )


def config_from_args(args: argparse.Namespace) -> SigningConfig | None:
    """The signing configuration, None when --sign was not given. Raises SigningError."""
    chosen = [args.sign_thumbprint, args.sign_subject, args.sign_pfx]
    extras = bool(
        args.timestamp_url
        or args.no_timestamp
        or args.sign_backend != "auto"
        or args.sign_allow_untrusted_root
    )
    if not args.sign:
        if any(item is not None for item in chosen) or extras:
            raise SigningError("The signing options need --sign.")
        return None
    if args.no_timestamp and args.timestamp_url:
        raise SigningError("--no-timestamp and --timestamp-url exclude each other.")
    urls = tuple(args.timestamp_url) if args.timestamp_url else DEFAULT_TIMESTAMP_URLS
    return SigningConfig(
        thumbprint=args.sign_thumbprint,
        subject=args.sign_subject,
        pfx=args.sign_pfx,
        timestamp_urls=urls,
        timestamp=not args.no_timestamp,
        backend=args.sign_backend,
        allow_untrusted_root=args.sign_allow_untrusted_root,
    ).validated()


def _add_expectations(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expect-subject", metavar="TEXT", help="exact subject, CN or O of the signer"
    )
    parser.add_argument("--expect-thumbprint", metavar="HEX", help="thumbprint of the signer")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and publish signed LCSC2Fusion releases.")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="check the signatures in a release folder")
    verify.add_argument("folder", type=Path)
    _add_expectations(verify)
    verify.add_argument(
        "--allow-untrusted-root",
        action="store_true",
        help="TESTS ONLY: accept a self signed certificate",
    )
    verify.add_argument(
        "--allow-no-timestamp",
        action="store_true",
        help="TESTS ONLY: accept signatures without a timestamp",
    )
    upload = commands.add_parser(
        "upload", help="replace the assets of an existing GitHub release with the signed files"
    )
    upload.add_argument("tag", help="release tag, for example v0.1.0")
    upload.add_argument("folder", type=Path)
    upload.add_argument("--repo", default=DEFAULT_REPO, metavar="OWNER/REPO")
    upload.add_argument("--dry-run", action="store_true", help="check and show the plan only")
    upload.add_argument("--yes", action="store_true", help="really upload")
    _add_expectations(upload)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            policy = VerifyPolicy(
                expect_thumbprint=args.expect_thumbprint,
                expect_subject=args.expect_subject,
                allow_untrusted_root=args.allow_untrusted_root,
                require_timestamp=not args.allow_no_timestamp,
            )
            report = verify_folder(args.folder, policy)
            print(format_report(report))
            for problem in report.problems:
                print(f"error: {problem}", file=sys.stderr)
            return 0 if report.ok else 1
        policy = VerifyPolicy(
            expect_thumbprint=args.expect_thumbprint, expect_subject=args.expect_subject
        )
        return upload_release(
            args.tag,
            args.folder,
            repo=args.repo,
            dry_run=args.dry_run,
            yes=args.yes,
            policy=policy,
        )
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
