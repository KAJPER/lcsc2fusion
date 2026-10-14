"""Tests of scripts/sign_release.py and of the signing path of scripts/build_release.py.

Most tests need no Windows: certificate selection, option checks, the signature parser, the order
of the build and the `upload` command (against a fake `gh` on PATH) run anywhere. The tests that
sign for real run on Windows only. They create throwaway self signed code signing certificates
in `Cert:\\CurrentUser\\My` (unique subjects, deleted in the teardown, even after a failure) and
never touch a trusted root, trusted publisher or machine store. Nothing here goes to the network:
the real signatures are made with `--no-timestamp`, the timestamp logic is tested with a fake
process runner. If a test run is killed hard, a certificate "CN=LCSC2Fusion test ..." may stay in
the store: remove it with
`Get-ChildItem Cert:\\CurrentUser\\My | ? Subject -like '*LCSC2Fusion test*' | Remove-Item`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from installer_support import (
    IS_WINDOWS,
    POWERSHELL,
    ROOT,
    WINDOWS_ONLY,
    Sandbox,
    load_build_release,
    make_junction,
    run_script,
    tree_bytes,
)

from lcsc2fusion import __version__

VERSION = __version__
BOM = b"\xef\xbb\xbf"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
DOUBLE_HYPHEN = "-" * 2
THUMB_A = "A" * 40
THUMB_B = "B" * 40
CANARY = "Canary-Pa55word-7f3a91"


# Fixtures and helpers ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def br() -> ModuleType:
    return load_build_release()


@pytest.fixture(scope="module")
def ss(br: ModuleType) -> ModuleType:
    """The same module instance that build_release.py uses."""
    return br._load_sign_release()


@pytest.fixture(scope="module")
def build_addin(br: ModuleType) -> ModuleType:
    return br._load_build_addin()


def make_cert(
    ss: ModuleType,
    thumbprint: str = THUMB_A,
    subject: str = "CN=KAJPA Kacper Popko, O=KAJPA, C=PL",
    *,
    ekus: tuple[str, ...] = ("1.3.6.1.5.5.7.3.3",),
    private_key: bool = True,
    valid_from_days: int = -30,
    valid_to_days: int = 300,
) -> Any:
    now = datetime.now(UTC)
    return ss.CertInfo(
        thumbprint=thumbprint,
        subject=subject,
        issuer="CN=Some CA",
        not_before=now + timedelta(days=valid_from_days),
        not_after=now + timedelta(days=valid_to_days),
        has_private_key=private_key,
        ekus=ekus,
    )


def parse_signing_args(ss: ModuleType, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sign", action="store_true")
    ss.add_signing_arguments(parser)
    return parser.parse_args(argv)


def run_ps(script: str, **env: str) -> str:
    """Windows PowerShell 5.1 with a script that gets its input from environment variables."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **env},
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.strip()


CREATE_CERT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$p = @{ Subject = $env:T_SUBJECT; CertStoreLocation = 'Cert:\CurrentUser\My' }
if ($env:T_KIND -eq 'codesign') { $p['Type'] = 'CodeSigningCert' }
if ($env:T_KIND -eq 'expired') {
    $p['Type'] = 'CodeSigningCert'
    $p['NotBefore'] = (Get-Date).AddDays(-30)
    $p['NotAfter'] = (Get-Date).AddDays(-1)
}
$cert = New-SelfSignedCertificate @p
$cert.Thumbprint
"""
DELETE_CERT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = 'Cert:\CurrentUser\My\' + $env:T_THUMB
if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
"""
EXPORT_PFX_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$secure = ConvertTo-Securestring -String $env:T_PASSWORD -AsPlainText -Force
$path = 'Cert:\CurrentUser\My\' + $env:T_THUMB
[void](Export-PfxCertificate -Cert $path -FilePath $env:T_PFX -Password $secure)
"""
LIST_THUMBS_SCRIPT = r"""
(Get-ChildItem -LiteralPath 'Cert:\CurrentUser\My' | ForEach-Object { $_.Thumbprint }) -join ','
"""


@dataclass(frozen=True)
class Made:
    thumbprint: str
    subject: str


@dataclass
class CertFactory:
    """Creates throwaway certificates and removes exactly those again."""

    prefix: str = field(default_factory=lambda: f"LCSC2Fusion test {uuid.uuid4().hex[:10]}")
    created: list[str] = field(default_factory=list)
    counter: int = 0

    def create(
        self, label: str = "", *, kind: str = "codesign", subject: str | None = None
    ) -> Made:
        self.counter += 1
        chosen = subject or f"CN={self.prefix} {label or self.counter}"
        thumbprint = run_ps(CREATE_CERT_SCRIPT, T_SUBJECT=chosen, T_KIND=kind)
        assert len(thumbprint) == 40, thumbprint
        self.created.append(thumbprint)
        return Made(thumbprint, chosen)

    def export_pfx(self, made: Made, path: Path, password: str) -> None:
        assert made.thumbprint in self.created
        run_ps(EXPORT_PFX_SCRIPT, T_THUMB=made.thumbprint, T_PFX=str(path), T_PASSWORD=password)

    def cleanup(self) -> None:
        for thumbprint in self.created:
            run_ps(DELETE_CERT_SCRIPT, T_THUMB=thumbprint)
        remaining = set(run_ps(LIST_THUMBS_SCRIPT).split(","))
        left = [t for t in self.created if t in remaining]
        self.created.clear()
        assert not left, f"throwaway certificates were not removed: {left}"


@pytest.fixture(scope="module")
def certs() -> Iterator[CertFactory]:
    if not IS_WINDOWS:
        pytest.skip("needs Windows: certificate store, Authenticode and PowerShell")
    factory = CertFactory()
    try:
        yield factory
    finally:
        factory.cleanup()


@pytest.fixture(scope="module")
def main_cert(certs: CertFactory) -> Made:
    """A code signing certificate with a subject full of non ASCII letters."""
    return certs.create(subject=f"CN={certs.prefix} Zażółć gęślą, O={certs.prefix} org")


@pytest.fixture(scope="module")
def unsigned_exe(br: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small exe made by IExpress, the way the real one is made."""
    iexpress = br.find_iexpress() if IS_WINDOWS else None
    if iexpress is None:
        pytest.skip("IExpress is not available")
    work = tmp_path_factory.mktemp("tiny_exe")
    archive = work / "installer.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("hello.txt", "hello")
    exe = work / "tiny.exe"
    br.build_setup_exe(ROOT, archive, exe, "0.0.0", iexpress)
    return exe


def real_signer(
    ss: ModuleType,
    cert: Made,
    *,
    backend: str = "auto",
    pfx: Path | None = None,
    timestamp: bool = False,
    messages: list[str] | None = None,
) -> Any:
    config = ss.SigningConfig(
        thumbprint=None if pfx else cert.thumbprint,
        pfx=pfx,
        timestamp=timestamp,
        backend=backend,
        allow_untrusted_root=True,
    )
    certificate = ss.load_certificate(config)
    signtool = ss.find_signtool() if backend != "powershell" and pfx is None else None
    if backend == "signtool" and signtool is None:
        pytest.skip("signtool.exe is not installed on this machine")
    out = messages if messages is not None else []
    return ss.Signer(config, certificate, signtool=signtool, say=out.append, warn=out.append)


def installer_script_copy(br: ModuleType, folder: Path, name: str = "install.ps1") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(br.read_installer_file(ROOT, name))
    return path


def unsigned_script_bytes(br: ModuleType, name: str) -> bytes:
    return bytes(br.read_installer_file(ROOT, name))


# DER, PE and script builders for the signature parser ----------------------------------------


def der(tag: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    if len(body) < 0x80:
        length = bytes([len(body)])
    else:
        raw = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + body


def der_oid(dotted: str) -> bytes:
    numbers = [int(part) for part in dotted.split(".")]
    content = bytearray([numbers[0] * 40 + numbers[1]])
    for number in numbers[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append(0x80 | (number & 0x7F))
            number >>= 7
        content.extend(reversed(chunk))
    return der(0x06, bytes(content))


def signed_data_blob(digest_oid: str, *, with_certs: bool = True) -> bytes:
    algorithm = der(0x30, der_oid(digest_oid), der(0x05))
    signer = der(
        0x30,
        der(0x02, b"\x01"),
        der(0x30, der(0x30), der(0x02, b"\x07")),
        algorithm,
        der(0x30, der_oid("1.2.840.113549.1.1.1"), der(0x05)),
        der(0x04, b"\x00\x01"),
    )
    parts = [der(0x02, b"\x01"), der(0x31, algorithm), der(0x30, der_oid("1.2.840.113549.1.7.1"))]
    if with_certs:
        parts.append(der(0xA0, der(0x30, b"\x00" * 3)))
    parts.append(der(0x31, signer))
    return der(0x30, der_oid("1.2.840.113549.1.7.2"), der(0xA0, der(0x30, *parts)))


def pe_with_signature(blob: bytes | None, *, pe32_plus: bool = True) -> bytes:
    optional_size = (112 if pe32_plus else 96) + 16 * 8
    header = bytearray(64)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (64).to_bytes(4, "little")
    coff = bytearray(20)
    coff[16:18] = optional_size.to_bytes(2, "little")
    optional = bytearray(optional_size)
    optional[0:2] = (0x20B if pe32_plus else 0x10B).to_bytes(2, "little")
    count_at = 108 if pe32_plus else 92
    optional[count_at : count_at + 4] = (16).to_bytes(4, "little")
    image = bytes(header) + b"PE\0\0" + bytes(coff)
    table = 112 if pe32_plus else 96
    body = image + bytes(optional) + b"\x90" * 64
    if blob is not None:
        padded = blob + b"\x00" * (-len(blob) % 8)
        # dwLength, wRevision 0x0200, wCertificateType 2 (PKCS #7 signed data), little endian
        certificate = (8 + len(padded)).to_bytes(4, "little") + b"\x00\x02\x02\x00" + padded
        offset = len(body)
        entry = 24 + 64 + table + 32  # file offset of the security directory entry
        patched = bytearray(body)
        patched[entry : entry + 4] = offset.to_bytes(4, "little")
        patched[entry + 4 : entry + 8] = len(certificate).to_bytes(4, "little")
        body = bytes(patched) + certificate
    return body


def script_with_signature(blob: bytes) -> bytes:
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[i : i + 64] for i in range(0, len(text), 64)]
    body = ["Write-Host 'hi'", "# SIG # Begin signature block"]
    body += [f"# {line}" for line in lines]
    body.append("# SIG # End signature block")
    return BOM + ("\r\n".join(body) + "\r\n").encode("utf-8")


# Fake process runner (no Windows needed) -----------------------------------------------------


class FakeRunner:
    """Stands in for run_process: answers signtool and the PowerShell helpers and records calls."""

    def __init__(self, ss: ModuleType) -> None:
        self.ss = ss
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []
        self.signtool_outcomes: list[tuple[int, str]] = []
        self.sign_outcomes: list[dict[str, object] | str] = []
        self.touch_on_failure = False
        self.has_timestamp = True
        self.verify_thumbprint = THUMB_A
        self.verify_subject = "CN=KAJPA Kacper Popko, O=KAJPA, C=PL"
        self.verify_status = "UnknownError"
        self.certs: list[dict[str, object]] = []
        self.pfx_cert: dict[str, object] | None = None

    def urls(self) -> list[str]:
        return [argv[argv.index("/tr") + 1] for kind, argv, _ in self.calls if kind == "signtool"]

    def kinds(self) -> list[str]:
        return [kind for kind, _argv, _env in self.calls]

    def __call__(
        self, argv: Sequence[str], env: dict[str, str], timeout: float, encoding: str
    ) -> subprocess.CompletedProcess[str]:
        arguments = list(argv)
        if Path(arguments[0]).stem.lower() == "signtool":
            return self._signtool(arguments, env)
        script = Path(arguments[arguments.index("-File") + 1]).read_text(encoding="utf-8-sig")
        assert "-EncodedCommand" not in arguments
        step = script.split("# step: ", 1)[1].split("\n", 1)[0].strip()
        spec = json.loads(env[self.ss.SPEC_ENV]) if env.get(self.ss.SPEC_ENV) else None
        result: Any
        code = 0
        if step == "sign":
            outcome = self.sign_outcomes.pop(0) if self.sign_outcomes else {}
            if isinstance(outcome, str):
                result, code = {"error": outcome}, 3
            else:
                result = {
                    "status": "UnknownError",
                    "timestamp_missing": False,
                    "message": "",
                    **outcome,
                }
                if not result["timestamp_missing"]:
                    with Path(spec["path"]).open("ab") as handle:
                        handle.write(b"[signed-ps]")
        elif step == "verify":
            result = [
                {
                    "path": path,
                    "status": self.verify_status,
                    "signature_type": "Authenticode",
                    "signer_subject": self.verify_subject,
                    "signer_thumbprint": self.verify_thumbprint,
                    "has_timestamp": self.has_timestamp,
                    "chain_flags": ["UntrustedRoot"] if self.verify_status != "Valid" else [],
                }
                for path in spec
            ]
        elif step == "list":
            result = self.certs
        elif step == "pfx":
            result = self.pfx_cert
        else:
            raise AssertionError(step)
        self.calls.append((step, arguments, dict(env)))
        Path(env[self.ss.OUT_ENV]).write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(arguments, code, "", "")

    def _signtool(self, argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(("signtool", argv, dict(env)))
        code, output = self.signtool_outcomes.pop(0) if self.signtool_outcomes else (0, "")
        target = Path(argv[-1])
        if code == 0:
            with target.open("ab") as handle:
                handle.write(b"[signed]")
        elif self.touch_on_failure:
            with target.open("ab") as handle:
                handle.write(b"[partial]")
        return subprocess.CompletedProcess(argv, code, output, "")


@pytest.fixture
def fake(ss: ModuleType, monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    monkeypatch.setattr(ss, "signature_digest_oid", lambda _path: ss.SHA256_OID)
    monkeypatch.setattr(ss, "powershell_path", lambda: Path("powershell.exe"))
    return FakeRunner(ss)


def fake_signer(
    ss: ModuleType,
    fake: FakeRunner,
    *,
    urls: tuple[str, ...] = ("http://ts1.invalid", "http://ts2.invalid", "http://ts3.invalid"),
    timestamp: bool = True,
    backend: str = "auto",
    signtool: bool = True,
    pfx: Path | None = None,
    sleeps: list[float] | None = None,
) -> Any:
    config = ss.SigningConfig(
        thumbprint=None if pfx else THUMB_A,
        pfx=pfx,
        timestamp_urls=urls,
        timestamp=timestamp,
        backend=backend,
        allow_untrusted_root=True,
    )
    pauses = sleeps if sleeps is not None else []
    return ss.Signer(
        config,
        make_cert(ss),
        signtool=Path("signtool.exe") if signtool else None,
        runner=fake,
        sleep=pauses.append,
        say=lambda _text: None,
        warn=lambda _text: None,
    )


# Pure helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [THUMB_A.lower(), " ".join(["AB" * 2] * 10), ":".join(["ab"] * 20), "‎" + THUMB_A],
)
def test_thumbprints_are_normalised(ss: ModuleType, text: str) -> None:
    result = ss.normalize_thumbprint(text)
    assert len(result) == 40
    assert result == result.upper()
    assert ss.normalize_thumbprint(THUMB_A.lower()) == THUMB_A


@pytest.mark.parametrize("text", ["", "ABC", "G" * 40, "A" * 39, "A" * 41, "A" * 64])
def test_bad_thumbprints_are_refused(ss: ModuleType, text: str) -> None:
    with pytest.raises(ss.SigningError, match="40 hex digits"):
        ss.normalize_thumbprint(text)


def test_distinguished_names_are_parsed_with_quotes_and_escapes(ss: ModuleType) -> None:
    assert ss.parse_distinguished_name("CN=A, O=B, C=PL") == [("CN", "A"), ("O", "B"), ("C", "PL")]
    assert ss.parse_distinguished_name('CN="Doe, John", O=KAJPA') == [
        ("CN", "Doe, John"),
        ("O", "KAJPA"),
    ]
    assert ss.parse_distinguished_name('CN="say ""hi""", O=x') == [("CN", 'say "hi"'), ("O", "x")]
    assert ss.parse_distinguished_name(r"CN=a\, b, O=c") == [("CN", "a, b"), ("O", "c")]
    assert ss.parse_distinguished_name("cn = lower ,o=Org") == [("CN", "lower"), ("O", "Org")]
    assert ss.parse_distinguished_name("") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CN=KAJPA Kacper Popko, O=KAJPA, C=PL", True),  # the whole subject
        ("kajpa kacper popko", True),  # CN value, case does not matter
        ("KAJPA", True),  # O value
        ("CN=KAJPA Kacper Popko", True),
        ("O=KAJPA", True),
        ("  KAJPA  ", True),
        ("KAJPA Kacper", False),  # a part of a name never matches
        ("Popko", False),
        ("PL", False),  # C is not a selector
        ("C=PL", False),
        ("CN=KAJPA", False),
        ("", False),
    ],
)
def test_subjects_match_exactly(ss: ModuleType, text: str, expected: bool) -> None:
    subject = "CN=KAJPA Kacper Popko, O=KAJPA, C=PL"
    assert ss.subject_matches(subject, text) is expected


def test_a_quoted_comma_in_the_subject_still_matches(ss: ModuleType) -> None:
    subject = 'CN="Doe, John", O=KAJPA, C=PL'
    assert ss.subject_matches(subject, "Doe, John")
    assert ss.subject_matches(subject, "CN=Doe, John")
    assert not ss.subject_matches(subject, "Doe")


def test_the_single_usable_certificate_is_selected(ss: ModuleType) -> None:
    other = make_cert(ss, THUMB_B, "CN=Somebody else")
    wanted = make_cert(ss)
    assert ss.select_certificate([other, wanted], thumbprint=THUMB_A.lower()) is wanted
    assert ss.select_certificate([other, wanted], subject="KAJPA") is wanted
    assert ss.select_certificate([wanted], subject="CN=KAJPA Kacper Popko, O=KAJPA, C=PL") is wanted


def test_exactly_one_selector_is_required(ss: ModuleType) -> None:
    with pytest.raises(ss.SigningError, match="exactly one"):
        ss.select_certificate([make_cert(ss)])
    with pytest.raises(ss.SigningError, match="exactly one"):
        ss.select_certificate([make_cert(ss)], thumbprint=THUMB_A, subject="x")


def test_no_matching_certificate_lists_what_could_be_used(ss: ModuleType) -> None:
    store = [
        make_cert(ss, THUMB_B, "CN=Some Other Signer"),
        make_cert(ss, "C" * 40, "CN=web", ekus=()),
    ]
    with pytest.raises(ss.SigningError) as caught:
        ss.select_certificate(store, thumbprint="D" * 40)
    message = str(caught.value)
    assert "No certificate with thumbprint" in message
    assert THUMB_B in message  # the code signing certificate is listed
    assert "C" * 40 not in message  # the web certificate is not
    with pytest.raises(ss.SigningError, match="SimplySign"):
        ss.select_certificate([make_cert(ss, "C" * 40, "CN=web", ekus=())], subject="nothing")
    with pytest.raises(ss.SigningError, match="No certificate with subject"):
        ss.select_certificate([], subject="nothing")


def test_several_matching_certificates_are_refused_with_their_thumbprints(ss: ModuleType) -> None:
    first = make_cert(ss, THUMB_A, "CN=First, O=KAJPA")
    second = make_cert(ss, THUMB_B, "CN=Second, O=KAJPA")
    with pytest.raises(ss.SigningError) as caught:
        ss.select_certificate([first, second], subject="KAJPA")
    message = str(caught.value)
    assert "2 certificates match" in message
    assert THUMB_A in message
    assert THUMB_B in message
    assert "--sign-thumbprint" in message
    assert ss.select_certificate([first, second], subject="First") is first


def test_a_renewed_certificate_wins_over_the_expired_one_with_the_same_subject(
    ss: ModuleType,
) -> None:
    old = make_cert(ss, THUMB_A, valid_from_days=-500, valid_to_days=-10)
    new = make_cert(ss, THUMB_B)
    assert ss.select_certificate([old, new], subject="KAJPA Kacper Popko") is new
    with pytest.raises(ss.SigningError, match="expired"):
        ss.select_certificate([old, new], thumbprint=THUMB_A)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"ekus": ("1.3.6.1.5.5.7.3.1", "1.3.6.1.5.5.7.3.2")}, "no Code Signing purpose"),
        ({"ekus": ()}, "no Code Signing purpose"),
        ({"private_key": False}, "no private key"),
        ({"valid_to_days": -1, "valid_from_days": -90}, "expired on"),
        ({"valid_from_days": 5}, "not valid before"),
    ],
)
def test_unusable_certificates_are_refused_with_the_reason(
    ss: ModuleType, changes: dict[str, object], expected: str
) -> None:
    cert = make_cert(ss, **changes)
    with pytest.raises(ss.SigningError, match=expected):
        ss.select_certificate([cert], thumbprint=THUMB_A)


def test_option_checks(ss: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ss.PFX_PASSWORD_ENV, raising=False)
    config = ss.config_from_args

    def bad(*argv: str) -> str:
        with pytest.raises(ss.SigningError) as caught:
            config(parse_signing_args(ss, *argv))
        return str(caught.value)

    assert config(parse_signing_args(ss)) is None
    assert "exactly one" in bad("--sign")
    assert "exactly one" in bad("--sign", "--sign-thumbprint", THUMB_A, "--sign-subject", "x")
    assert "40 hex digits" in bad("--sign", "--sign-thumbprint", "123")
    assert "need --sign" in bad("--sign-thumbprint", THUMB_A)
    assert "need --sign" in bad("--no-timestamp")
    assert "exclude each other" in bad(
        "--sign", "--sign-thumbprint", THUMB_A, "--no-timestamp", "--timestamp-url", "http://x.test"
    )
    assert "not an http" in bad(
        "--sign", "--sign-thumbprint", THUMB_A, "--timestamp-url", "ftp://x"
    )
    assert "not an http" in bad("--sign", "--sign-thumbprint", THUMB_A, "--timestamp-url", "x y")
    assert "user name" in bad(
        "--sign", "--sign-thumbprint", THUMB_A, "--timestamp-url", "http://u:p@x.test/ts"
    )
    assert "does not exist" in bad("--sign", "--sign-pfx", str(tmp_path / "missing.pfx"))
    pfx = tmp_path / "key.pfx"
    pfx.write_bytes(b"not a real pfx")
    assert ss.PFX_PASSWORD_ENV in bad("--sign", "--sign-pfx", str(pfx))
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    assert "signtool would need" in bad(
        "--sign", "--sign-pfx", str(pfx), "--sign-backend", "signtool"
    )
    checked = config(parse_signing_args(ss, "--sign", "--sign-pfx", str(pfx), "--no-timestamp"))
    assert checked.pfx == pfx and not checked.timestamp
    assert CANARY not in repr(checked)


def test_a_pfx_inside_the_repository_is_refused(
    ss: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    inside = ROOT / "pyproject.toml"  # any existing file of the checkout
    with pytest.raises(ss.SigningError, match="inside the repository"):
        ss.check_pfx_file(inside)


def test_defaults_and_options_are_resolved(ss: ModuleType) -> None:
    default = ss.config_from_args(parse_signing_args(ss, "--sign", "--sign-subject", "KAJPA"))
    assert default.timestamp and default.timestamp_urls == ss.DEFAULT_TIMESTAMP_URLS
    assert default.backend == "auto" and not default.allow_untrusted_root
    assert len(ss.DEFAULT_TIMESTAMP_URLS) >= 3
    assert all(url.startswith("http") for url in ss.DEFAULT_TIMESTAMP_URLS)
    custom = ss.config_from_args(
        parse_signing_args(
            ss,
            "--sign",
            "--sign-thumbprint",
            THUMB_A.lower(),
            "--timestamp-url",
            "http://one.test",
            "--timestamp-url",
            "https://two.test/ts",
            "--sign-backend",
            "powershell",
        )
    )
    assert custom.timestamp_urls == ("http://one.test", "https://two.test/ts")
    assert custom.thumbprint == THUMB_A and custom.backend == "powershell"


# Reading signatures out of files -------------------------------------------------------------


@pytest.mark.parametrize(
    ("oid", "name"),
    [
        ("2.16.840.1.101.3.4.2.1", "sha256"),
        ("1.3.14.3.2.26", "sha1"),
        ("2.16.840.1.101.3.4.2.3", "sha512"),
    ],
)
def test_the_digest_algorithm_is_read_from_a_pkcs7_blob(
    ss: ModuleType, oid: str, name: str
) -> None:
    for with_certs in (True, False):
        assert ss.pkcs7_digest_oid(signed_data_blob(oid, with_certs=with_certs)) == oid
    assert ss.DIGEST_NAMES[oid] == name


def test_the_digest_of_an_exe_and_a_script_is_read_from_the_file(
    ss: ModuleType, tmp_path: Path
) -> None:
    blob = signed_data_blob(ss.SHA256_OID)
    for plus in (True, False):
        exe = tmp_path / f"signed_{plus}.exe"
        exe.write_bytes(pe_with_signature(blob, pe32_plus=plus))
        assert ss.signature_digest_oid(exe) == ss.SHA256_OID
    script = tmp_path / "signed.ps1"
    script.write_bytes(script_with_signature(signed_data_blob("1.3.14.3.2.26")))
    assert ss.signature_digest_oid(script) == "1.3.14.3.2.26"


def test_unsigned_or_damaged_files_have_no_digest(ss: ModuleType, tmp_path: Path) -> None:
    cases = {
        "unsigned.exe": pe_with_signature(None),
        "text.exe": b"hello" * 100,
        "empty.exe": b"",
        "plain.ps1": BOM + b"Write-Host 1\r\n",
        "garbage.ps1": BOM
        + b"x\r\n# SIG # Begin signature block\r\n# !!!\r\n# SIG # End signature block\r\n",
        "cut.exe": pe_with_signature(signed_data_blob(ss.SHA256_OID))[:-30],
        "nonsense.exe": pe_with_signature(b"\x30\x03\x01\x02\x03"),
    }
    for name, data in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert ss.signature_digest_oid(path) is None, name


# Judging a signature -------------------------------------------------------------------------


def info(ss: ModuleType, **changes: object) -> Any:
    values: dict[str, object] = {
        "label": "file.exe",
        "path": Path("file.exe"),
        "status": "Valid",
        "signature_type": "Authenticode",
        "signer_subject": "CN=KAJPA Kacper Popko, O=KAJPA",
        "signer_thumbprint": THUMB_A,
        "has_timestamp": True,
        "digest_oid": ss.SHA256_OID,
        "chain_flags": (),
    }
    values.update(changes)
    return ss.SignatureInfo(**values)


def test_a_valid_sha256_timestamped_signature_passes(ss: ModuleType) -> None:
    policy = ss.VerifyPolicy(expect_thumbprint=THUMB_A, expect_subject="KAJPA")
    assert ss.check_signature(info(ss), policy) == []


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"status": "NotSigned", "signature_type": "None"}, "not signed"),
        ({"status": "HashMismatch"}, "changed after it was signed"),
        ({"status": "NotTrusted"}, "does not trust"),
        ({"status": "UnknownError", "chain_flags": ("UntrustedRoot",)}, "does not trust"),
        ({"signature_type": "Catalog"}, "embedded Authenticode"),
        ({"signer_thumbprint": THUMB_B}, "expected " + THUMB_A),
        ({"signer_subject": "CN=Somebody else"}, "expected"),
        ({"digest_oid": "1.3.14.3.2.26"}, "sha1"),
        ({"digest_oid": None}, "unreadable"),
        ({"has_timestamp": False}, "no timestamp"),
    ],
)
def test_signature_problems_are_reported(
    ss: ModuleType, changes: dict[str, object], expected: str
) -> None:
    policy = ss.VerifyPolicy(expect_thumbprint=THUMB_A, expect_subject="KAJPA")
    problems = ss.check_signature(info(ss, **changes), policy)
    assert problems
    assert expected in " ".join(problems)


def test_untrusted_root_is_accepted_only_when_it_is_the_only_flaw(ss: ModuleType) -> None:
    allow = ss.VerifyPolicy(allow_untrusted_root=True)
    self_signed = info(ss, status="UnknownError", chain_flags=("UntrustedRoot",))
    assert ss.check_signature(self_signed, allow) == []
    assert (
        ss.check_signature(info(ss, status="NotTrusted", chain_flags=("UntrustedRoot",)), allow)
        == []
    )
    assert ss.check_signature(self_signed, ss.VerifyPolicy()) != []
    for flags in ((), ("NotTimeValid",), ("UntrustedRoot", "NotTimeValid"), ("PartialChain",)):
        assert ss.check_signature(info(ss, status="UnknownError", chain_flags=flags), allow)
    # a hash mismatch is never excused
    tampered = info(ss, status="HashMismatch", chain_flags=("UntrustedRoot",))
    assert any("changed after" in p for p in ss.check_signature(tampered, allow))


def test_the_timestamp_requirement_can_be_switched_off_for_tests(ss: ModuleType) -> None:
    no_stamp = info(ss, has_timestamp=False)
    assert ss.check_signature(no_stamp, ss.VerifyPolicy(require_timestamp=False)) == []
    assert ss.check_signature(no_stamp, ss.VerifyPolicy()) != []


# Finding signtool ----------------------------------------------------------------------------


def test_signtool_is_found_on_path_or_in_the_newest_sdk(ss: ModuleType, tmp_path: Path) -> None:
    program_files = tmp_path / "pf"
    for version in ("10.0.19041.0", "10.0.22621.0", "10.0.9.0"):
        folder = program_files / "Windows Kits" / "10" / "bin" / version / "x64"
        folder.mkdir(parents=True)
        (folder / "signtool.exe").write_bytes(b"")
        (program_files / "Windows Kits" / "10" / "bin" / version / "arm64").mkdir()
    environ = {"ProgramFiles(x86)": str(program_files), "PATH": ""}
    found = ss.find_signtool(environ)
    assert found is not None
    assert found.parent.parent.name == "10.0.22621.0"  # numeric, not alphabetical, order
    assert ss.find_signtool({"ProgramFiles(x86)": str(tmp_path / "none"), "PATH": ""}) is None
    assert ss.find_signtool({"PATH": ""}) is None
    on_path = tmp_path / "bin"
    on_path.mkdir()
    name = "signtool.exe" if IS_WINDOWS else "signtool"
    tool = on_path / name
    tool.write_bytes(b"")
    tool.chmod(0o755)
    assert ss.find_signtool({**environ, "PATH": str(on_path)}) == tool


# Which files may be signed -------------------------------------------------------------------


def test_only_plain_files_inside_the_working_folder_may_be_signed(
    ss: ModuleType, tmp_path: Path
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    inside = root / "a.ps1"
    inside.write_bytes(b"x")
    ss.ensure_plain_file_inside(inside, root)
    outside = tmp_path / "outside.ps1"
    outside.write_bytes(b"x")
    with pytest.raises(ss.SigningError, match="not inside"):
        ss.ensure_plain_file_inside(outside, root)
    with pytest.raises(ss.SigningError, match="not inside"):
        ss.ensure_plain_file_inside(root / ".." / "outside.ps1", root)
    with pytest.raises(ss.SigningError, match="not inside"):
        ss.ensure_plain_file_inside(tmp_path / "work2" / "a.ps1", root)
    with pytest.raises(ss.SigningError, match="not a regular file"):
        (root / "folder").mkdir()
        ss.ensure_plain_file_inside(root / "folder", root)
    with pytest.raises(ss.SigningError):
        ss.ensure_plain_file_inside(root / "missing.ps1", root)


def test_hard_links_are_refused(ss: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    original = tmp_path / "original.ps1"
    original.write_bytes(b"x")
    linked = root / "linked.ps1"
    try:
        os.link(original, linked)
    except OSError:
        pytest.skip("hard links are not available here")
    with pytest.raises(ss.SigningError, match="hard links"):
        ss.ensure_plain_file_inside(linked, root)


def test_symbolic_links_are_refused(ss: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    target = tmp_path / "target.ps1"
    target.write_bytes(b"x")
    link = root / "link.ps1"
    folder_target = tmp_path / "elsewhere"
    folder_target.mkdir()
    (folder_target / "inner.ps1").write_bytes(b"x")
    folder_link = root / "dirlink"
    try:
        link.symlink_to(target)
        folder_link.symlink_to(folder_target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available here (Windows needs a privilege)")
    with pytest.raises(ss.SigningError, match="is a link"):
        ss.ensure_plain_file_inside(link, root)
    with pytest.raises(ss.SigningError, match="is a link"):
        ss.ensure_plain_file_inside(folder_link / "inner.ps1", root)


@WINDOWS_ONLY
def test_junctions_are_refused(ss: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "inner.ps1").write_bytes(b"x")
    make_junction(root / "junction", target)
    with pytest.raises(ss.SigningError, match="is a link"):
        ss.ensure_plain_file_inside(root / "junction" / "inner.ps1", root)
    junction_root = tmp_path / "rootlink"
    make_junction(junction_root, root)
    (root / "a.ps1").write_bytes(b"x")
    with pytest.raises(ss.SigningError, match="is a link"):
        ss.ensure_plain_file_inside(junction_root / "a.ps1", junction_root)


def test_the_signer_refuses_a_file_outside_its_folder_before_doing_anything(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    root = tmp_path / "work"
    root.mkdir()
    victim = tmp_path / "precious.ps1"
    victim.write_bytes(b"precious")
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError, match="not inside"):
        signer.sign(victim, root)
    assert fake.calls == []
    assert victim.read_bytes() == b"precious"


# Signing logic with a fake process runner ----------------------------------------------------


def work_file(tmp_path: Path, name: str = "install.ps1") -> tuple[Path, Path]:
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(BOM + b"Write-Host 1\r\n")
    return path, root


def test_timestamp_servers_are_tried_in_order(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    timestamp_error = "SignTool Error: The specified timestamp server either could not be reached"
    fake.signtool_outcomes = [(1, timestamp_error), (1, timestamp_error)]
    sleeps: list[float] = []
    signer = fake_signer(ss, fake, sleeps=sleeps)
    result = signer.sign(path, root)
    assert fake.urls() == ["http://ts1.invalid", "http://ts2.invalid", "http://ts3.invalid"]
    assert result.status == "UnknownError"
    assert path.read_bytes().endswith(b"[signed]")
    assert path.read_bytes().count(b"[signed]") == 1
    assert sleeps == []  # all in the first round
    first = fake.calls[0][1]
    assert first[1:7] == ["sign", "/fd", "sha256", "/s", "My", "/sha1"]
    assert first[7] == THUMB_A
    assert first[8:12] == ["/tr", "http://ts1.invalid", "/td", "sha256"]


def test_all_servers_failing_ends_after_two_rounds_and_restores_the_file(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    original = path.read_bytes()
    fake.touch_on_failure = True
    fake.signtool_outcomes = [
        (1, "SignTool Error: timestamp server returned an invalid response")
    ] * 20
    sleeps: list[float] = []
    signer = fake_signer(ss, fake, sleeps=sleeps)
    with pytest.raises(ss.SigningError, match="no timestamp server answered") as caught:
        signer.sign(path, root)
    assert len(fake.urls()) == 2 * 3
    assert sleeps == [ss.TIMESTAMP_PAUSE_SECONDS]
    assert path.read_bytes() == original  # a half signed file is not left behind
    assert "setup.exe" in str(caught.value)
    assert "sign" not in fake.kinds()  # no PowerShell fallback for a timestamp problem


def test_a_failure_that_is_not_about_the_timestamp_does_not_try_other_servers(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    fake.signtool_outcomes = [
        (1, "SignTool Error: No certificates were found that met all criteria")
    ]
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError, match="No certificates were found"):
        signer.sign(path, root)
    assert len(fake.urls()) == 1
    assert "sign" not in fake.kinds()  # an exe has no PowerShell fallback


def test_a_script_falls_back_to_powershell_when_signtool_cannot_sign_it(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path)
    fake.signtool_outcomes = [(1, "SignTool Error: No SIP exists for this file type")]
    signer = fake_signer(ss, fake)
    signer.sign(path, root)
    assert fake.kinds()[:2] == ["signtool", "sign"]
    assert path.read_bytes().endswith(b"[signed-ps]")
    spec = json.loads(fake.calls[1][2][ss.SPEC_ENV])
    assert spec["thumbprint"] == THUMB_A and spec["timestamp_url"] == "http://ts1.invalid"


def test_both_tools_failing_reports_both_reasons(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path)
    original = path.read_bytes()
    fake.signtool_outcomes = [(1, "SignTool Error: first problem")]
    fake.sign_outcomes = ["second problem"]
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError) as caught:
        signer.sign(path, root)
    assert "first problem" in str(caught.value) and "second problem" in str(caught.value)
    assert path.read_bytes() == original


def test_powershell_that_adds_no_timestamp_counts_as_a_timestamp_failure(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path)
    fake.sign_outcomes = [{"timestamp_missing": True, "message": "Unknown error (0x80072efd)"}, {}]
    signer = fake_signer(ss, fake, backend="powershell")
    signer.sign(path, root)
    stamps = [
        json.loads(env[ss.SPEC_ENV])["timestamp_url"]
        for kind, _a, env in fake.calls
        if kind == "sign"
    ]
    assert stamps == ["http://ts1.invalid", "http://ts2.invalid"]
    assert "signtool" not in fake.kinds()
    assert path.read_bytes().count(b"[signed-ps]") == 1  # the failed attempt was rolled back


def test_no_timestamp_signs_once_and_without_a_timestamp_option(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    fake.has_timestamp = False
    signer = fake_signer(ss, fake, timestamp=False)
    signer.sign(path, root)
    argv = fake.calls[0][1]
    assert "/tr" not in argv and "/td" not in argv
    assert fake.kinds().count("signtool") == 1


def test_a_missing_timestamp_fails_the_check_after_signing(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    fake.has_timestamp = False
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError, match="no timestamp"):
        signer.sign(path, root)


def test_a_signature_by_another_certificate_fails_the_check(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    fake.verify_thumbprint = THUMB_B
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError, match="expected " + THUMB_A):
        signer.sign(path, root)


def test_a_signature_with_another_subject_fails_the_check(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    fake.verify_subject = "CN=Somebody else"
    signer = fake_signer(ss, fake)
    with pytest.raises(ss.SigningError, match="signer is 'CN=Somebody else'"):
        signer.sign(path, root)


def test_a_status_other_than_valid_fails_unless_untrusted_roots_are_allowed(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path
) -> None:
    path, root = work_file(tmp_path, "setup.exe")
    signer = fake_signer(ss, fake)
    signer.config = ss.SigningConfig(thumbprint=THUMB_A, timestamp_urls=("http://ts1.invalid",))
    with pytest.raises(ss.SigningError, match="does not trust"):
        signer.sign(path, root)
    path2, _ = work_file(tmp_path, "setup2.exe")
    fake.verify_status = "Valid"
    signer.sign(path2, root)


def test_the_backend_choice(ss: ModuleType, fake: FakeRunner, tmp_path: Path) -> None:
    exe = tmp_path / "a.exe"
    script = tmp_path / "a.ps1"
    assert fake_signer(ss, fake)._backends(exe) == ["signtool"]
    assert fake_signer(ss, fake)._backends(script) == ["signtool", "powershell"]
    assert fake_signer(ss, fake, signtool=False)._backends(exe) == ["powershell"]
    assert fake_signer(ss, fake, backend="powershell")._backends(exe) == ["powershell"]
    assert fake_signer(ss, fake, backend="signtool")._backends(script) == ["signtool"]


def test_a_pfx_is_signed_through_powershell_and_the_password_stays_in_that_process(
    ss: ModuleType, fake: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    pfx = tmp_path / "key.pfx"
    pfx.write_bytes(b"x")
    path, root = work_file(tmp_path, "setup.exe")
    signer = fake_signer(ss, fake, pfx=pfx)
    signer.sign(path, root)
    assert "signtool" not in fake.kinds()
    for kind, argv, env in fake.calls:
        assert CANARY not in " ".join(argv)  # never on a command line
        assert (ss.PFX_PASSWORD_ENV in env) == (kind == "sign"), kind
    assert json.loads(fake.calls[0][2][ss.SPEC_ENV])["pfx"] == str(pfx)


def test_the_pfx_password_is_removed_from_error_text(
    ss: ModuleType, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    monkeypatch.setattr(ss, "powershell_path", lambda: Path("powershell.exe"))

    def runner(argv: Sequence[str], env: dict[str, str], timeout: float, encoding: str) -> object:
        return subprocess.CompletedProcess(
            list(argv), 5, f"stdout has {CANARY}", f"stderr has {CANARY}"
        )

    with pytest.raises(ss.SigningError) as caught:
        ss.run_powershell("# step: list\n", runner=runner)
    assert CANARY not in str(caught.value)
    assert "***" in str(caught.value)

    def failing(argv: Sequence[str], env: dict[str, str], timeout: float, encoding: str) -> object:
        Path(env[ss.OUT_ENV]).write_text(
            json.dumps({"error": f"bad {CANARY} here"}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(list(argv), 3, "", "")

    with pytest.raises(ss.SigningError) as caught2:
        ss.run_powershell("# step: list\n", runner=failing)
    assert CANARY not in str(caught2.value)
    assert ss.tail(f"x {CANARY} y") == "x *** y"


def test_the_password_is_not_in_the_environment_of_other_children(
    ss: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    assert ss.PFX_PASSWORD_ENV not in ss.child_env()
    assert ss.child_env(with_password=True)[ss.PFX_PASSWORD_ENV] == CANARY


@WINDOWS_ONLY
def test_the_announcement_and_the_no_timestamp_warning(
    ss: ModuleType, fake: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.certs = [
        {
            "thumbprint": THUMB_A,
            "subject": "CN=KAJPA Kacper Popko, O=KAJPA, C=PL",
            "issuer": "CN=Some CA",
            "not_before": int((datetime.now(UTC) - timedelta(days=5)).timestamp()),
            "not_after": int((datetime.now(UTC) + timedelta(days=100)).timestamp()),
            "has_private_key": True,
            "ekus": ["1.3.6.1.5.5.7.3.3"],
        }
    ]
    lines: list[str] = []
    warnings: list[str] = []
    config = ss.SigningConfig(subject="KAJPA", timestamp=False)
    signer = ss.create_signer(config, runner=fake, say=lines.append, warn=warnings.append)
    text = "\n".join(lines)
    assert "CN=KAJPA Kacper Popko" in text and THUMB_A in text and "expires:" in text
    assert "NONE (--no-timestamp)" in text
    assert any("NO timestamp" in w and "tests only" in w for w in warnings)
    assert signer.certificate.thumbprint == THUMB_A
    lines.clear()
    warnings.clear()
    ss.create_signer(
        ss.SigningConfig(thumbprint=THUMB_A, backend="powershell"),
        runner=fake,
        say=lines.append,
        warn=warnings.append,
    )
    assert warnings == []  # a normal signing run warns about nothing
    assert "digest:     sha256" in "\n".join(lines)


def test_signing_is_refused_outside_windows(ss: ModuleType) -> None:
    if IS_WINDOWS:
        pytest.skip("this is the non Windows check")
    with pytest.raises(ss.SigningError, match="needs Windows"):
        ss.create_signer(ss.SigningConfig(thumbprint=THUMB_A))
    with pytest.raises(ss.SigningError, match="needs Windows"):
        ss.verify_folder(Path("."), ss.VerifyPolicy())


# The build order -----------------------------------------------------------------------------


class RecordingSigner:
    """A signer for the build tests: marks every file and remembers what existed at that time."""

    def __init__(self, br: ModuleType, fail_on: str | None = None) -> None:
        self.br = br
        self.fail_on = fail_on
        self.events: list[str] = []
        self.snapshots: dict[str, dict[str, bytes]] = {}

    def sign(self, path: Path, root: Path) -> object:
        self.events.append(path.name)
        if path.name == self.fail_on:
            raise RuntimeError(f"cannot sign {path.name}")
        self.snapshots[path.name] = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        if path.suffix == ".ps1":
            assert root.name == "signing"  # copies in a temporary folder, not the repository
            path.write_bytes(path.read_bytes() + b"# SIG # Begin signature block\r\n# FAKE\r\n")
        else:
            path.write_bytes(path.read_bytes() + b"FAKE EXE SIGNATURE")
        return object()


def repo_installer_bytes() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (ROOT / "installer").iterdir() if p.is_file()}


def zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_scripts_are_signed_before_they_are_zipped_and_nothing_else_changes(
    br: ModuleType, tmp_path: Path
) -> None:
    before = repo_installer_bytes()
    plain = tmp_path / "plain"
    br.build_release(ROOT, plain, want_exe=False)
    signed_out = tmp_path / "signed"
    signer = RecordingSigner(br)
    br.build_release(ROOT, signed_out, want_exe=False, signer=signer)
    assert signer.events == ["install.ps1", "uninstall.ps1"]
    assert repo_installer_bytes() == before  # the files of the repository are never touched
    name = f"LCSC2Fusion-{VERSION}-installer.zip"
    unsigned_members = zip_members(plain / name)
    signed_members = zip_members(signed_out / name)
    assert set(signed_members) == set(unsigned_members)
    for member, data in signed_members.items():
        if member in ("install.ps1", "uninstall.ps1"):
            assert data == unsigned_members[member] + b"# SIG # Begin signature block\r\n# FAKE\r\n"
        else:
            assert data == unsigned_members[member], member  # including payload.sha256
    assert (signed_out / f"LCSC2Fusion-{VERSION}-addin.zip").read_bytes() == (
        plain / f"LCSC2Fusion-{VERSION}-addin.zip"
    ).read_bytes()  # the plain add-in zip is not signed
    sums = (signed_out / "SHA256SUMS.txt").read_text(encoding="ascii")
    for line in sums.splitlines():
        digest, file_name = line.split("  ", 1)
        assert digest == hashlib.sha256((signed_out / file_name).read_bytes()).hexdigest()
    assert sums.count("\n") == 2  # no exe in this build


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_exe_is_built_from_the_signed_zip_and_signed_last(
    br: ModuleType, tmp_path: Path
) -> None:
    if br.find_iexpress() is None:
        pytest.skip("IExpress is not available")
    out = tmp_path / "out"
    signer = RecordingSigner(br)
    inner: dict[str, bytes] = {}
    original_sign = signer.sign

    def sign_and_inspect(path: Path, root: Path) -> object:
        if path.suffix == ".exe":
            unpacked = tmp_path / "unpacked"
            unpacked.mkdir()
            done = subprocess.run(
                [str(path), "/C", f"/T:{unpacked}", "/Q"], timeout=120, check=False
            )
            assert done.returncode == 0
            inner.update(zip_members(unpacked / "installer.zip"))
            assert not (out / "SHA256SUMS.txt").exists()  # the checksums come after the exe
        return original_sign(path, root)

    signer.sign = sign_and_inspect
    assets = br.build_release(ROOT, out, signer=signer)
    assert signer.events == ["install.ps1", "uninstall.ps1", f"LCSC2Fusion-Setup-{VERSION}.exe"]
    assert inner["install.ps1"].endswith(b"# FAKE\r\n")  # the exe holds the signed scripts
    assert inner["uninstall.ps1"].endswith(b"# FAKE\r\n")
    installer_zip = zip_members(out / f"LCSC2Fusion-{VERSION}-installer.zip")
    assert {k: v for k, v in inner.items()} == installer_zip
    exe = out / f"LCSC2Fusion-Setup-{VERSION}.exe"
    assert exe.read_bytes().endswith(b"FAKE EXE SIGNATURE")  # the shipped exe is the signed one
    listed = dict(
        line.split("  ", 1)[::-1]
        for line in (out / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    )
    assert listed[exe.name] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert [a.name for a in assets][-1] == "SHA256SUMS.txt"


def test_a_signing_failure_fails_the_build_and_leaves_the_output_folder_alone(
    br: ModuleType, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    stale = {
        f"LCSC2Fusion-{VERSION}-addin.zip": b"previous good build",
        "SHA256SUMS.txt": b"previous sums",
        "notes.txt": b"mine",
    }
    for name, data in stale.items():
        (out / name).write_bytes(data)
    signer = RecordingSigner(br, fail_on="uninstall.ps1")
    with pytest.raises(RuntimeError, match=r"cannot sign uninstall\.ps1"):
        br.build_release(ROOT, out, want_exe=False, signer=signer)
    assert signer.events == ["install.ps1", "uninstall.ps1"]
    assert {p.name: p.read_bytes() for p in out.iterdir()} == stale  # nothing half finished


@WINDOWS_ONLY
@pytest.mark.slow
def test_a_failed_exe_signature_publishes_no_unsigned_exe(br: ModuleType, tmp_path: Path) -> None:
    if br.find_iexpress() is None:
        pytest.skip("IExpress is not available")
    out = tmp_path / "out"
    signer = RecordingSigner(br, fail_on=f"LCSC2Fusion-Setup-{VERSION}.exe")
    with pytest.raises(RuntimeError):
        br.build_release(ROOT, out, signer=signer)
    assert list(out.iterdir()) == []


def test_signing_without_the_setup_exe_is_refused_when_the_exe_is_wanted(
    br: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(br, "find_iexpress", lambda: None)
    with pytest.raises(br.ReleaseError, match="needs the setup exe"):
        br.build_release(ROOT, tmp_path / "out", signer=RecordingSigner(br))
    br.build_release(ROOT, tmp_path / "out2", want_exe=False, signer=RecordingSigner(br))


def test_overrides_must_name_installer_files(br: ModuleType, tmp_path: Path) -> None:
    build_addin = br._load_build_addin()
    result = build_addin.build(root=ROOT, dist=tmp_path / "dist")
    with pytest.raises(br.ReleaseError, match="not a file of the installer zip root"):
        br.build_installer_zip(
            ROOT, result.folder, result.files, tmp_path / "x.zip", {"evil.ps1": b"x"}
        )


def test_the_build_checks_that_signing_kept_the_text_format(br: ModuleType, tmp_path: Path) -> None:
    class Damaging:
        def sign(self, path: Path, root: Path) -> object:
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
            return object()

    with pytest.raises(br.ReleaseError, match="text format"):
        br.sign_installer_scripts(ROOT, tmp_path / "signing", Damaging())


def test_main_passes_the_options_to_the_signer_and_marks_the_result(
    br: ModuleType,
    ss: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[object] = []

    def create_signer(config: object) -> object:
        seen.append(config)
        return RecordingSigner(br)

    monkeypatch.setattr(ss, "create_signer", create_signer)
    out = tmp_path / "out"
    code = br.main(
        [
            "--sign",
            "--sign-subject",
            "KAJPA",
            "--timestamp-url",
            "http://ts.test",
            "--no-exe",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    config = seen[0]
    assert config.subject == "KAJPA" and config.timestamp_urls == ("http://ts.test",)
    assert f"Release {VERSION} in {out} (signed):" in capsys.readouterr().out
    # --sign together with --check-tag builds, a bare --check-tag only checks
    seen.clear()
    assert (
        br.main(
            [
                "--check-tag",
                f"v{VERSION}",
                "--sign",
                "--sign-subject",
                "KAJPA",
                "--no-exe",
                "--out",
                str(tmp_path / "o2"),
            ]
        )
        == 0
    )
    assert len(seen) == 1
    assert br.main(["--check-tag", f"v{VERSION}"]) == 0
    assert len(seen) == 1


def test_main_reports_a_signing_error_as_a_build_failure(
    br: ModuleType,
    ss: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(config: object) -> object:
        raise ss.SigningError("No certificate with subject 'KAJPA' in Cert:\\CurrentUser\\My.")

    monkeypatch.setattr(ss, "create_signer", refuse)
    out = tmp_path / "out"
    assert br.main(["--sign", "--sign-subject", "KAJPA", "--no-exe", "--out", str(out)]) == 1
    assert "No certificate with subject" in capsys.readouterr().err
    assert not out.exists()  # nothing was built


@pytest.mark.parametrize(
    "argv",
    [
        ["--sign-thumbprint", THUMB_A],
        ["--no-timestamp"],
        ["--sign"],
        ["--sign", "--sign-thumbprint", THUMB_A, "--sign-subject", "x"],
        ["--sign", "--sign-thumbprint", "123"],
        [
            "--sign",
            "--sign-thumbprint",
            THUMB_A,
            "--no-timestamp",
            "--timestamp-url",
            "http://x.test",
        ],
    ],
)
def test_main_rejects_inconsistent_signing_options_before_building(
    br: ModuleType, tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as caught:
        br.main([*argv, "--no-exe", "--out", str(out)])
    assert caught.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not out.exists()


def test_the_unsigned_default_does_not_use_the_signing_module(
    br: ModuleType, ss: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("signing was used without --sign")

    monkeypatch.setattr(ss, "create_signer", forbidden)
    monkeypatch.setattr(ss, "run_powershell", forbidden)
    assert br.main(["--out", str(tmp_path / "out"), "--no-exe"]) == 0


def run_git(folder: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=folder,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_the_build_warns_about_a_dirty_tree_or_an_untagged_head(
    br: ModuleType, tmp_path: Path
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    assert br.uncommitted_changes_warning(tmp_path, "1.0.0") is None  # no .git: nothing to say
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    (repo / "a.txt").write_text("a", encoding="utf-8")
    run_git(repo, "add", "a.txt")
    run_git(repo, "commit", "-q", "-m", "first")
    untagged = br.uncommitted_changes_warning(repo, "1.0.0")
    assert untagged is not None and "HEAD is not tagged v1.0.0" in untagged
    run_git(repo, "tag", "v1.0.0")
    assert br.uncommitted_changes_warning(repo, "1.0.0") is None
    (repo / "a.txt").write_text("changed", encoding="utf-8")
    dirty = br.uncommitted_changes_warning(repo, "1.0.0")
    assert dirty is not None and "uncommitted changes" in dirty
    assert "git switch --detach v1.0.0" in dirty


# Real signing (Windows) ----------------------------------------------------------------------


@WINDOWS_ONLY
def test_the_store_listing_contains_the_throwaway_certificate(
    ss: ModuleType, main_cert: Made
) -> None:
    listed = {c.thumbprint: c for c in ss.list_store_certificates()}
    cert = listed[main_cert.thumbprint]
    assert cert.subject == main_cert.subject  # non ASCII letters survive
    assert cert.has_private_key
    assert ss.CODE_SIGNING_EKU in cert.ekus
    assert cert.not_before < datetime.now(UTC) < cert.not_after
    assert cert.issuer == cert.subject  # self signed


@WINDOWS_ONLY
def test_the_real_store_selection_by_thumbprint_and_subject(
    ss: ModuleType, main_cert: Made, certs: CertFactory
) -> None:
    by_thumbprint = ss.load_certificate(ss.SigningConfig(thumbprint=main_cert.thumbprint))
    assert by_thumbprint.subject == main_cert.subject
    by_subject = ss.load_certificate(ss.SigningConfig(subject=main_cert.subject))
    assert by_subject.thumbprint == main_cert.thumbprint
    by_cn = ss.load_certificate(ss.SigningConfig(subject=f"{certs.prefix} Zażółć gęślą"))
    assert by_cn.thumbprint == main_cert.thumbprint
    by_org = ss.load_certificate(ss.SigningConfig(subject=f"{certs.prefix} org"))
    assert by_org.thumbprint == main_cert.thumbprint  # O is a selector too


@WINDOWS_ONLY
def test_the_real_store_errors(ss: ModuleType, certs: CertFactory) -> None:
    web = certs.create("tls", kind="tls")
    expired = certs.create("expired", kind="expired")
    with pytest.raises(ss.SigningError, match="no Code Signing purpose"):
        ss.load_certificate(ss.SigningConfig(thumbprint=web.thumbprint))
    with pytest.raises(ss.SigningError, match="expired on"):
        ss.load_certificate(ss.SigningConfig(thumbprint=expired.thumbprint))
    with pytest.raises(ss.SigningError, match="No certificate with subject"):
        ss.load_certificate(ss.SigningConfig(subject=f"{certs.prefix} does not exist"))
    org = f"Twin Org {uuid.uuid4().hex[:8]}"
    first = certs.create(subject=f"CN={certs.prefix} twin one, O={org}")
    second = certs.create(subject=f"CN={certs.prefix} twin two, O={org}")
    with pytest.raises(ss.SigningError, match="2 certificates match") as caught:
        ss.load_certificate(ss.SigningConfig(subject=org))
    assert first.thumbprint in str(caught.value) and second.thumbprint in str(caught.value)
    chosen = ss.load_certificate(ss.SigningConfig(subject=f"{certs.prefix} twin two"))
    assert chosen.thumbprint == second.thumbprint


BACKENDS = ["signtool", "powershell"]


@WINDOWS_ONLY
@pytest.mark.parametrize("backend", BACKENDS)
def test_a_script_is_signed_and_checked(
    ss: ModuleType, br: ModuleType, main_cert: Made, backend: str, tmp_path: Path
) -> None:
    script = installer_script_copy(br, tmp_path / "work")
    original = script.read_bytes()
    messages: list[str] = []
    signer = real_signer(ss, main_cert, backend=backend, messages=messages)
    result = signer.sign(script, tmp_path / "work")
    assert result.signer_thumbprint == main_cert.thumbprint
    assert result.signer_subject == main_cert.subject
    assert result.digest_oid == ss.SHA256_OID
    assert result.signature_type == "Authenticode"
    assert result.status in ("UnknownError", "NotTrusted")
    assert result.chain_flags == ("UntrustedRoot",)
    assert not result.has_timestamp
    data = script.read_bytes()
    assert data.startswith(BOM) and data.replace(b"\r\n", b"").count(b"\n") == 0
    assert data.startswith(original)  # the script text itself is unchanged
    assert b"# SIG # Begin signature block" in data
    assert any(f"signed install.ps1 with {backend}" in m for m in messages)
    # Windows is not told to trust the self signed root, so a strict check must fail
    again = ss.verify_signatures([script])[0]
    assert ss.check_signature(again, ss.VerifyPolicy(require_timestamp=False)) != []
    assert (
        ss.check_signature(
            again, ss.VerifyPolicy(allow_untrusted_root=True, require_timestamp=False)
        )
        == []
    )


@WINDOWS_ONLY
@pytest.mark.parametrize("backend", BACKENDS)
def test_an_exe_is_signed_and_checked(
    ss: ModuleType, main_cert: Made, backend: str, unsigned_exe: Path, tmp_path: Path
) -> None:
    exe = tmp_path / "work" / "tiny.exe"
    exe.parent.mkdir()
    shutil.copyfile(unsigned_exe, exe)
    assert ss.verify_signatures([exe])[0].status == "NotSigned"
    assert ss.signature_digest_oid(exe) is None
    signer = real_signer(ss, main_cert, backend=backend)
    result = signer.sign(exe, exe.parent)
    assert result.signer_thumbprint == main_cert.thumbprint
    assert result.digest_oid == ss.SHA256_OID
    assert result.signature_type == "Authenticode"
    assert exe.stat().st_size > unsigned_exe.stat().st_size
    assert exe.read_bytes()[:2] == b"MZ"


@WINDOWS_ONLY
@pytest.mark.parametrize("name", ["tiny.exe", "script.ps1"])
def test_a_file_changed_after_signing_is_caught(
    ss: ModuleType, br: ModuleType, main_cert: Made, unsigned_exe: Path, tmp_path: Path, name: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    target = work / name
    if name.endswith(".exe"):
        shutil.copyfile(unsigned_exe, target)
    else:
        target.write_bytes(br.read_installer_file(ROOT, "install.ps1"))
    signer = real_signer(ss, main_cert)
    signer.sign(target, work)
    data = bytearray(target.read_bytes())
    position = 1000 if name.endswith(".exe") else data.index(b"Installs the LCSC2Fusion") + 10
    data[position] ^= 0x01
    target.write_bytes(bytes(data))
    changed = ss.verify_signatures([target])[0]
    assert changed.status == "HashMismatch"
    allow_everything = ss.VerifyPolicy(allow_untrusted_root=True, require_timestamp=False)
    assert any(
        "changed after it was signed" in p for p in ss.check_signature(changed, allow_everything)
    )


def zip_release_folder(
    br: ModuleType, ss: ModuleType, signer: object, exe_source: Path, folder: Path, *, sign: bool
) -> None:
    """A release folder with the file names of the real one, built by hand."""
    folder.mkdir(parents=True, exist_ok=True)
    work = folder.parent / (folder.name + "_work")
    work.mkdir()
    exe = folder / f"LCSC2Fusion-Setup-{VERSION}.exe"
    shutil.copyfile(exe_source, exe)
    members = {"README.txt": b"x", "LCSC2Fusion/LCSC2Fusion.manifest": b"{}"}
    for name in br.SIGNED_SCRIPTS:
        script = installer_script_copy(br, work, name)
        if sign:
            signer.sign(script, work)
        members[name] = script.read_bytes()
    with zipfile.ZipFile(folder / f"LCSC2Fusion-{VERSION}-installer.zip", "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    if sign:
        signer.sign(exe, folder)


@WINDOWS_ONLY
def test_verify_folder_accepts_a_signed_release_and_names_every_problem(
    ss: ModuleType, br: ModuleType, main_cert: Made, unsigned_exe: Path, tmp_path: Path
) -> None:
    signer = real_signer(ss, main_cert)
    folder = tmp_path / "release"
    zip_release_folder(br, ss, signer, unsigned_exe, folder, sign=True)
    lenient = ss.VerifyPolicy(
        expect_thumbprint=main_cert.thumbprint, allow_untrusted_root=True, require_timestamp=False
    )
    report = ss.verify_folder(folder, lenient)
    assert report.ok, report.problems
    assert [i.label for i in report.infos] == [
        f"LCSC2Fusion-Setup-{VERSION}.exe",
        f"LCSC2Fusion-{VERSION}-installer.zip:install.ps1",
        f"LCSC2Fusion-{VERSION}-installer.zip:uninstall.ps1",
    ]
    text = ss.format_report(report)
    assert main_cert.thumbprint in text and "sha256" in text and "File" in text
    # the default policy wants a trusted root and a timestamp
    strict = ss.verify_folder(folder, ss.VerifyPolicy())
    assert not strict.ok
    assert any("does not trust" in p for p in strict.problems)
    assert any("no timestamp" in p for p in strict.problems)
    # a signer that is not the expected one
    other = ss.VerifyPolicy(
        expect_thumbprint=THUMB_B, allow_untrusted_root=True, require_timestamp=False
    )
    assert any("expected " + THUMB_B in p for p in ss.verify_folder(folder, other).problems)
    named = ss.VerifyPolicy(
        expect_subject="nobody", allow_untrusted_root=True, require_timestamp=False
    )
    assert any("signer is" in p for p in ss.verify_folder(folder, named).problems)


@WINDOWS_ONLY
def test_verify_folder_catches_unsigned_changed_and_foreign_files(
    ss: ModuleType,
    br: ModuleType,
    main_cert: Made,
    certs: CertFactory,
    unsigned_exe: Path,
    tmp_path: Path,
) -> None:
    lenient = ss.VerifyPolicy(allow_untrusted_root=True, require_timestamp=False)
    signer = real_signer(ss, main_cert)
    # an unsigned script inside the installer zip
    folder = tmp_path / "one"
    zip_release_folder(br, ss, signer, unsigned_exe, folder, sign=True)
    archive_path = folder / f"LCSC2Fusion-{VERSION}-installer.zip"
    members = zip_members(archive_path)
    members["uninstall.ps1"] = unsigned_script_bytes(br, "uninstall.ps1")
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    report = ss.verify_folder(folder, lenient)
    assert any("uninstall.ps1: not signed" in p for p in report.problems)
    # a script that is missing from the zip
    del members["uninstall.ps1"]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    assert any("contains no uninstall.ps1" in p for p in ss.verify_folder(folder, lenient).problems)
    # a signed exe that was changed afterwards
    exe = folder / f"LCSC2Fusion-Setup-{VERSION}.exe"
    data = bytearray(exe.read_bytes())
    data[1000] ^= 0x01
    exe.write_bytes(bytes(data))
    assert any(
        "changed after it was signed" in p for p in ss.verify_folder(folder, lenient).problems
    )
    # two different signers in one folder
    second = certs.create("second signer")
    folder2 = tmp_path / "two"
    zip_release_folder(br, ss, signer, unsigned_exe, folder2, sign=True)
    other_signer = real_signer(ss, second)
    exe2 = folder2 / f"LCSC2Fusion-Setup-{VERSION}.exe"
    shutil.copyfile(unsigned_exe, exe2)
    other_signer.sign(exe2, folder2)
    mixed = ss.verify_folder(folder2, lenient)
    assert any("different certificates" in p for p in mixed.problems)


@WINDOWS_ONLY
def test_verify_folder_with_nothing_to_check_or_a_bad_zip(ss: ModuleType, tmp_path: Path) -> None:
    report = ss.verify_folder(tmp_path, ss.VerifyPolicy())
    assert any("nothing to verify" in p for p in report.problems)
    (tmp_path / f"LCSC2Fusion-{VERSION}-installer.zip").write_bytes(b"not a zip")
    broken = ss.verify_folder(tmp_path, ss.VerifyPolicy())
    assert any("cannot be read as a zip" in p for p in broken.problems)
    with pytest.raises(ss.SigningError, match="not a folder"):
        ss.verify_folder(tmp_path / "missing", ss.VerifyPolicy())


@WINDOWS_ONLY
def test_the_verify_command_exit_codes(
    ss: ModuleType,
    br: ModuleType,
    main_cert: Made,
    unsigned_exe: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    signer = real_signer(ss, main_cert)
    folder = tmp_path / "release"
    zip_release_folder(br, ss, signer, unsigned_exe, folder, sign=True)
    good = [
        "verify",
        str(folder),
        "--allow-untrusted-root",
        "--allow-no-timestamp",
        "--expect-thumbprint",
        main_cert.thumbprint.lower(),
    ]
    assert ss.main(good) == 0
    shown = capsys.readouterr()
    assert "UnknownError" in shown.out and main_cert.thumbprint in shown.out
    assert ss.main(["verify", str(folder), "--allow-no-timestamp"]) == 1  # untrusted root
    assert "does not trust" in capsys.readouterr().err
    assert ss.main(["verify", str(folder), "--allow-untrusted-root"]) == 1  # no timestamp
    assert "no timestamp" in capsys.readouterr().err
    assert ss.main([*good, "--expect-subject", "somebody else"]) == 1
    assert ss.main(["verify", str(tmp_path / "missing")]) == 1
    assert "not a folder" in capsys.readouterr().err


@WINDOWS_ONLY
def test_a_pfx_file_signs_without_touching_the_store(
    ss: ModuleType,
    br: ModuleType,
    certs: CertFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    made = certs.create("pfx source")
    pfx = tmp_path / "sign.pfx"
    certs.export_pfx(made, pfx, CANARY)
    # remove the certificate from the store: the PFX must be enough, and nothing is imported back
    run_ps(DELETE_CERT_SCRIPT, T_THUMB=made.thumbprint)
    before = run_ps(LIST_THUMBS_SCRIPT)
    assert made.thumbprint not in before
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, CANARY)
    config = ss.SigningConfig(pfx=pfx, timestamp=False, allow_untrusted_root=True).validated()
    messages: list[str] = []
    signer = ss.create_signer(config, say=messages.append, warn=messages.append)
    assert signer.certificate.thumbprint == made.thumbprint
    assert "PFX file sign.pfx" in "\n".join(messages)
    script = installer_script_copy(br, tmp_path / "work")
    result = signer.sign(script, tmp_path / "work")
    assert result.signer_thumbprint == made.thumbprint
    assert result.digest_oid == ss.SHA256_OID
    assert run_ps(LIST_THUMBS_SCRIPT) == before  # nothing was imported into the store
    shown = capsys.readouterr()
    assert CANARY not in shown.out + shown.err + "\n".join(messages)


@WINDOWS_ONLY
def test_a_wrong_pfx_password_fails_without_revealing_it(
    ss: ModuleType, certs: CertFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = certs.create("pfx wrong password")
    pfx = tmp_path / "sign.pfx"
    certs.export_pfx(made, pfx, CANARY)
    wrong = "Wrong-Pa55word-12345"
    monkeypatch.setenv(ss.PFX_PASSWORD_ENV, wrong)
    with pytest.raises(ss.SigningError) as caught:
        ss.create_signer(ss.SigningConfig(pfx=pfx, timestamp=False).validated())
    assert wrong not in str(caught.value) and CANARY not in str(caught.value)
    monkeypatch.delenv(ss.PFX_PASSWORD_ENV)
    with pytest.raises(ss.SigningError, match=ss.PFX_PASSWORD_ENV):
        ss.SigningConfig(pfx=pfx, timestamp=False).validated()


@WINDOWS_ONLY
def test_build_with_a_real_certificate_signs_the_scripts_in_the_zip(
    ss: ModuleType,
    br: ModuleType,
    main_cert: Made,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = repo_installer_bytes()
    plain = tmp_path / "plain"
    br.build_release(ROOT, plain, want_exe=False)
    out = tmp_path / "signed"
    code = br.main(
        [
            "--sign",
            "--sign-subject",
            main_cert.subject,
            "--no-timestamp",
            "--sign-allow-untrusted-root",
            "--no-exe",
            "--out",
            str(out),
        ]
    )
    shown = capsys.readouterr()
    assert code == 0, shown.err
    assert "Signing certificate" in shown.out and main_cert.thumbprint in shown.out
    assert "NO timestamp" in shown.err and "tests only" in shown.err  # the loud warning
    assert repo_installer_bytes() == before
    name = f"LCSC2Fusion-{VERSION}-installer.zip"
    signed_members = zip_members(out / name)
    plain_members = zip_members(plain / name)
    assert signed_members["payload.sha256"] == plain_members["payload.sha256"]
    for script in br.SIGNED_SCRIPTS:
        assert signed_members[script].startswith(plain_members[script])
        assert b"# SIG # Begin signature block" in signed_members[script]
    assert signed_members["Install-LCSC2Fusion.bat"] == plain_members["Install-LCSC2Fusion.bat"]
    verify = ss._load_sibling("verify_checksums")
    assert verify.verify(out, VERSION, with_exe=False) == []
    policy = ss.VerifyPolicy(
        expect_thumbprint=main_cert.thumbprint, allow_untrusted_root=True, require_timestamp=False
    )
    assert ss.verify_folder(out, policy).ok


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_installer_zip_of_a_signed_build_installs(
    ss: ModuleType, br: ModuleType, build_addin: ModuleType, main_cert: Made, tmp_path: Path
) -> None:
    out = tmp_path / "signed"
    assert (
        br.main(
            [
                "--sign",
                "--sign-thumbprint",
                main_cert.thumbprint,
                "--no-timestamp",
                "--sign-allow-untrusted-root",
                "--no-exe",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    result = build_addin.build(root=ROOT, dist=tmp_path / "dist")
    expected = {name: (result.folder / name).read_bytes() for name in result.files}
    unpacked = tmp_path / "unpacked here"
    with zipfile.ZipFile(out / f"LCSC2Fusion-{VERSION}-installer.zip") as archive:
        archive.extractall(unpacked)
    assert b"# SIG # Begin signature block" in (unpacked / "install.ps1").read_bytes()
    sandbox = Sandbox(tmp_path / "profile")
    target = sandbox.root / "Fusion Addins"
    done = run_script(
        unpacked / "install.ps1", "-TargetRoot", str(target), "-Language", "en", sandbox=sandbox
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert tree_bytes(target / "LCSC2Fusion") == expected
    gone = run_script(unpacked / "uninstall.ps1", "-TargetRoot", str(target), sandbox=sandbox)
    assert gone.returncode == 0, gone.stdout + gone.stderr


@WINDOWS_ONLY
@pytest.mark.slow
def test_the_full_signed_build_with_the_setup_exe(
    ss: ModuleType,
    br: ModuleType,
    build_addin: ModuleType,
    main_cert: Made,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if br.find_iexpress() is None:
        pytest.skip("IExpress is not available")
    out = tmp_path / "signed"
    code = br.main(
        [
            "--sign",
            "--sign-thumbprint",
            main_cert.thumbprint,
            "--no-timestamp",
            "--sign-allow-untrusted-root",
            "--out",
            str(out),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert sorted(p.name for p in out.iterdir()) == [
        f"LCSC2Fusion-{VERSION}-addin.zip",
        f"LCSC2Fusion-{VERSION}-installer.zip",
        f"LCSC2Fusion-Setup-{VERSION}.exe",
        "SHA256SUMS.txt",
    ]
    verify_checksums = ss._load_sibling("verify_checksums")
    assert verify_checksums.verify(out, VERSION) == []
    command = [
        "verify",
        str(out),
        "--allow-untrusted-root",
        "--allow-no-timestamp",
        "--expect-thumbprint",
        main_cert.thumbprint,
    ]
    assert ss.main(command) == 0
    exe = out / f"LCSC2Fusion-Setup-{VERSION}.exe"
    signature = ss.verify_signatures([exe])[0]
    assert signature.signer_thumbprint == main_cert.thumbprint
    assert signature.signature_type == "Authenticode"
    # the signed exe still installs, quietly, into a temp target and never touches the real one
    result = build_addin.build(root=ROOT, dist=tmp_path / "dist")
    expected = {name: (result.folder / name).read_bytes() for name in result.files}
    sandbox = Sandbox(tmp_path / "profile")
    target = sandbox.root / "FusionAddins"
    log = tmp_path / "setup.log"
    env = sandbox.env(
        {"LCSC2FUSION_FUSIONADDINS_DIR": str(target), "LCSC2FUSION_SETUP_LOG": str(log)}
    )
    done = subprocess.run([str(exe), "/Q"], env=env, timeout=180, check=False)
    assert done.returncode == 0
    assert tree_bytes(target / "LCSC2Fusion") == expected
    text = log.read_text(encoding="utf-8", errors="replace")
    assert f"Done. LCSC2Fusion {VERSION} is installed in" in text or "Gotowe." in text
    # a signed exe that is changed afterwards no longer verifies
    data = bytearray(exe.read_bytes())
    data[2000] ^= 0x01
    exe.write_bytes(bytes(data))
    assert ss.main(command) == 1


# upload, against a fake gh -------------------------------------------------------------------

FAKE_GH = r"""
import json
import os
import sys

state_path = os.environ["FAKE_GH_STATE"]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)
code = 0
if args[:2] == ["release", "view"]:
    release = state.get("release")
    if release is None:
        sys.stderr.write("release not found\n")
        code = 1
    else:
        sys.stdout.write(json.dumps(release))
elif args[:2] == ["release", "upload"]:
    if state.get("fail_upload"):
        sys.stderr.write("HTTP 403: Resource not accessible\n")
        code = 1
    else:
        assets = {a["name"]: a for a in state["release"]["assets"]}
        for argument in args[3:]:
            if os.path.isfile(argument) and not state.get("drop_assets"):
                name = os.path.basename(argument)
                assets[name] = {"name": name, "size": os.path.getsize(argument)}
        state["release"]["assets"] = list(assets.values())
else:
    sys.stderr.write("unexpected gh command\n")
    code = 9
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump(state, handle)
sys.exit(code)
"""


@dataclass
class FakeGh:
    state_path: Path

    def state(self) -> dict[str, object]:
        data: dict[str, object] = json.loads(self.state_path.read_text(encoding="utf-8"))
        return data

    def calls(self) -> list[list[str]]:
        calls: list[list[str]] = self.state()["calls"]
        return calls

    def configure(self, **changes: object) -> None:
        data = self.state()
        data.update(changes)
        self.state_path.write_text(json.dumps(data), encoding="utf-8")


def release_json(tag: str = "v1.2.3", **changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "tagName": tag,
        "isDraft": False,
        "isPrerelease": "-" in tag,
        "url": f"https://github.com/KAJPER/lcsc2fusion/releases/tag/{tag}",
        "assets": [
            {"name": "LCSC2Fusion-Setup-1.2.3.exe", "size": 5},
            {"name": "SHA256SUMS.txt", "size": 1},
        ],
    }
    data.update(changes)
    return data


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """A `gh` on PATH that records its arguments and answers `release view` and `release upload`."""
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir()
    script = bin_dir / "fake_gh.py"
    script.write_text(FAKE_GH, encoding="utf-8")
    if IS_WINDOWS:
        (bin_dir / "gh.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="ascii"
        )
    else:
        shim = bin_dir / "gh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    state_path = tmp_path / "gh_state.json"
    state_path.write_text(json.dumps({"calls": [], "release": release_json()}), encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return FakeGh(state_path)


def release_folder(ss: ModuleType, folder: Path, version: str = "1.2.3") -> Path:
    folder.mkdir(parents=True)
    contents = {
        f"LCSC2Fusion-Setup-{version}.exe": b"exe bytes!!",
        f"LCSC2Fusion-{version}-installer.zip": b"installer bytes",
        f"LCSC2Fusion-{version}-addin.zip": b"addin bytes",
    }
    lines = []
    for name, data in sorted(contents.items()):
        (folder / name).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    (folder / "SHA256SUMS.txt").write_text("".join(lines), encoding="ascii")
    return folder


def accepting(ss: ModuleType) -> Callable[[Path, object], object]:
    return lambda _folder, _policy: ss.VerifyReport(infos=(), problems=(), notes=())


def rejecting(ss: ModuleType) -> Callable[[Path, object], object]:
    return lambda _folder, _policy: ss.VerifyReport(
        infos=(), problems=("file.exe: not signed",), notes=()
    )


def asset_order(ss: ModuleType, folder: Path) -> list[str]:
    return [str(folder / name) for name in ss.release_file_names("1.2.3")]


def test_the_four_release_files_are_named_with_the_checksums_last(ss: ModuleType) -> None:
    assert ss.release_file_names("1.2.3") == [
        "LCSC2Fusion-1.2.3-addin.zip",
        "LCSC2Fusion-1.2.3-installer.zip",
        "LCSC2Fusion-Setup-1.2.3.exe",
        "SHA256SUMS.txt",
    ]


def test_upload_dry_run_checks_everything_and_changes_nothing(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    lines: list[str] = []
    code = ss.upload_release(
        "v1.2.3", folder, dry_run=True, verifier=accepting(ss), say=lines.append
    )
    assert code == 0
    assert [call[:2] for call in gh.calls()] == [["release", "view"]]  # read only
    text = "\n".join(lines)
    assert "Upload plan" in text and "KAJPER/lcsc2fusion" in text and "Dry run" in text
    assert "replaces the asset of 5 bytes" in text  # the exe already exists on the release
    assert "new asset" in text
    assert "release upload v1.2.3" in text
    assert "--clobber --repo KAJPER/lcsc2fusion" in text


def test_upload_without_yes_prints_the_plan_and_refuses(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    lines: list[str] = []
    with pytest.raises(ss.SigningError, match="--yes"):
        ss.upload_release("v1.2.3", folder, verifier=accepting(ss), say=lines.append)
    assert "Upload plan" in "\n".join(lines)
    assert [call[:2] for call in gh.calls()] == [["release", "view"]]


def test_upload_with_yes_replaces_the_four_assets_with_one_clobber_upload(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    code = ss.upload_release(
        "v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None
    )
    assert code == 0
    calls = gh.calls()
    assert [call[:2] for call in calls] == [
        ["release", "view"],
        ["release", "upload"],
        ["release", "view"],
    ]
    upload = calls[1]
    assert upload == [
        "release",
        "upload",
        "v1.2.3",
        *asset_order(ss, folder),
        "--clobber",
        "--repo",
        "KAJPER/lcsc2fusion",
    ]
    assert upload[-3] == "--clobber"
    assert upload[-4].endswith("SHA256SUMS.txt")  # the checksum list goes last
    names = {a["name"] for a in gh.state()["release"]["assets"]}
    assert names == set(ss.release_file_names("1.2.3"))


def test_upload_goes_to_the_repository_that_was_asked_for(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    ss.upload_release(
        "v1.2.3", folder, repo="someone/else", yes=True, verifier=accepting(ss), say=lambda _t: None
    )
    for call in gh.calls():
        assert call[call.index("--repo") + 1] == "someone/else"


def test_upload_only_reads_and_uploads_it_never_creates_or_deletes(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    for call in gh.calls():
        assert call[0] == "release" and call[1] in ("view", "upload")
        assert not {"create", "delete", "edit", "--draft", "delete-asset"} & set(call)
    source = (ROOT / "scripts" / "sign_release.py").read_text(encoding="utf-8")
    verbs = set(re.findall(r'"release",\s*"(\w+)"', source))
    assert verbs == {"view", "upload"}  # the only gh verbs the script knows
    for verb in ("create", "delete", "edit"):
        assert f'"{verb}"' not in source, verb


def test_upload_refuses_a_release_that_does_not_exist(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    gh.configure(release=None)
    folder = release_folder(ss, tmp_path / "rel")
    with pytest.raises(ss.SigningError, match=r"no readable release v1\.2\.3") as caught:
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert "push the tag" in str(caught.value)
    assert [call[:2] for call in gh.calls()] == [["release", "view"]]  # no upload attempt


def test_upload_refuses_a_draft_and_a_different_tag(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    gh.configure(release=release_json(isDraft=True))
    with pytest.raises(ss.SigningError, match="is a draft"):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    gh.configure(release=release_json("v9.9.9"))
    with pytest.raises(ss.SigningError, match=r"v9\.9\.9"):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert all(call[:2] != ["release", "upload"] for call in gh.calls())


def test_upload_refuses_a_tag_that_does_not_match_the_file_names(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel", version="1.2.3")
    with pytest.raises(ss.SigningError, match="does not match the files") as caught:
        ss.upload_release("v1.2.4", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert "1.2.3" in str(caught.value)  # the version that the folder really holds
    assert gh.calls() == []  # gh was not even asked


def test_upload_refuses_when_the_signatures_are_not_acceptable(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    with pytest.raises(ss.SigningError, match="not acceptable") as caught:
        ss.upload_release("v1.2.3", folder, yes=True, verifier=rejecting(ss), say=lambda _t: None)
    assert "file.exe: not signed" in str(caught.value)
    assert gh.calls() == []  # verify runs first, gh is never called


def test_upload_refuses_when_the_checksums_do_not_match_the_files(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    (folder / "LCSC2Fusion-Setup-1.2.3.exe").write_bytes(b"changed after the sums were written")
    with pytest.raises(ss.SigningError, match=r"SHA256SUMS\.txt does not describe"):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    (folder / "SHA256SUMS.txt").unlink()
    with pytest.raises(ss.SigningError):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert gh.calls() == []


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v1.2", "v1.2.3.4", "V1.2.3", "v1.2.3 ", "v1.2.3\n", "refs/tags/v1.2.3", "v1.2.3;x"],
)
def test_upload_rejects_odd_tags(ss: ModuleType, gh: FakeGh, tmp_path: Path, tag: str) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    with pytest.raises(ss.SigningError, match="not a release tag"):
        ss.upload_release(tag, folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert gh.calls() == []


@pytest.mark.parametrize("repo", ["", "owner", "a/b/c", "owner/repo name", "--repo/x", "o/r;x"])
def test_upload_rejects_odd_repositories(
    ss: ModuleType, gh: FakeGh, tmp_path: Path, repo: str
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    with pytest.raises(ss.SigningError, match="not a repository"):
        ss.upload_release("v1.2.3", folder, repo=repo, yes=True, verifier=accepting(ss))
    assert gh.calls() == []


def test_upload_reports_a_failed_upload_and_an_upload_that_did_not_arrive(
    ss: ModuleType, gh: FakeGh, tmp_path: Path
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    gh.configure(fail_upload=True)
    with pytest.raises(ss.SigningError, match="gh release upload failed") as caught:
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)
    assert "403" in str(caught.value)
    gh.configure(fail_upload=False, drop_assets=True)
    with pytest.raises(ss.SigningError, match="does not show the expected files"):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss), say=lambda _t: None)


def test_upload_needs_the_github_cli(
    ss: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    monkeypatch.setattr(ss.shutil, "which", lambda _name, **_kw: None)
    with pytest.raises(ss.SigningError, match="GitHub CLI"):
        ss.upload_release("v1.2.3", folder, yes=True, verifier=accepting(ss))


def test_the_upload_command_line(
    ss: ModuleType,
    gh: FakeGh,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = release_folder(ss, tmp_path / "rel")
    seen: list[object] = []

    def stub(folder_arg: Path, policy: object) -> object:
        seen.append(policy)
        return ss.VerifyReport(infos=(), problems=(), notes=())

    monkeypatch.setattr(ss, "verify_folder", stub)
    base = [
        "upload",
        "v1.2.3",
        str(folder),
        "--expect-thumbprint",
        THUMB_A,
        "--expect-subject",
        "KAJPA",
    ]
    assert ss.main([*base, "--dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out
    policy = seen[0]
    assert policy.expect_thumbprint == THUMB_A and policy.expect_subject == "KAJPA"
    assert not policy.allow_untrusted_root and policy.require_timestamp
    assert ss.main(base) == 1  # neither --yes nor --dry-run
    assert "--yes" in capsys.readouterr().err
    assert ss.main([*base, "--yes"]) == 0
    assert "Uploaded 4 files" in capsys.readouterr().out
    assert ss.main(["upload", "nope", str(folder), "--yes"]) == 1
    assert "not a release tag" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        ss.main(["upload", "v1.2.3"])  # the folder is required


@WINDOWS_ONLY
def test_upload_with_really_signed_files(
    ss: ModuleType,
    br: ModuleType,
    gh: FakeGh,
    main_cert: Made,
    unsigned_exe: Path,
    tmp_path: Path,
) -> None:
    """Real signatures (self signed, so the lenient policy), fake gh."""
    signer = real_signer(ss, main_cert)
    folder = tmp_path / "rel"
    zip_release_folder(br, ss, signer, unsigned_exe, folder, sign=True)
    (folder / f"LCSC2Fusion-{VERSION}-addin.zip").write_bytes(b"addin")
    lines = []
    for path in sorted(p for p in folder.iterdir() if p.name != "SHA256SUMS.txt"):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (folder / "SHA256SUMS.txt").write_text("".join(lines), encoding="ascii")
    gh.configure(release=release_json(f"v{VERSION}", assets=[]))
    policy = ss.VerifyPolicy(
        expect_thumbprint=main_cert.thumbprint, allow_untrusted_root=True, require_timestamp=False
    )
    code = ss.upload_release(f"v{VERSION}", folder, yes=True, policy=policy, say=lambda _t: None)
    assert code == 0
    assert [c[:2] for c in gh.calls()] == [
        ["release", "view"],
        ["release", "upload"],
        ["release", "view"],
    ]
    # the same folder with a strict policy is refused, nothing more reaches gh
    calls_before = len(gh.calls())
    with pytest.raises(ss.SigningError, match="not acceptable"):
        ss.upload_release(f"v{VERSION}", folder, yes=True, say=lambda _t: None)
    assert len(gh.calls()) == calls_before


# Housekeeping --------------------------------------------------------------------------------


def test_the_signing_script_is_stdlib_only() -> None:
    import ast

    tree = ast.parse((ROOT / "scripts" / "sign_release.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize("name", ["build_release.py", "sign_release.py"])
def test_no_certificate_is_ever_installed_into_a_store(name: str) -> None:
    source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    for forbidden in (
        "Import-Certificate",
        "Import-PfxCertificate",
        "certutil",
        "addstore",
        "LocalMachine",
        "TrustedPublisher",
        "CurrentUser\\Root",
        "CurrentUser/Root",
        "ImportPfx",
        "X509Store",
        "Export-PfxCertificate",
        "/p ",
    ):
        assert forbidden not in source, forbidden
    if name == "sign_release.py":
        # the password is read from the environment and used by PowerShell only
        assert source.count("PFX_PASSWORD_ENV") >= 4
        assert '"/p"' not in source and "'/p'" not in source and '"/csp"' not in source


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "scripts" / "sign_release.py",
        ROOT / "scripts" / "build_release.py",
        ROOT / "tests" / "test_signing.py",
        ROOT / "installer" / "README.txt",
        ROOT / "RELEASING.md",
    ],
    ids=lambda p: p.name,
)
def test_no_long_dashes_and_no_double_hyphen_prose(path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig")
    assert EM_DASH not in text and EN_DASH not in text
    assert f" {DOUBLE_HYPHEN} " not in text.replace("```", "") or path.suffix == ".md"
    if path.suffix == ".md":
        outside_fences = "\n".join(part for i, part in enumerate(text.split("```")) if i % 2 == 0)
        assert f" {DOUBLE_HYPHEN} " not in outside_fences
    if path.name == "README.txt":
        assert DOUBLE_HYPHEN not in text


def test_the_installer_readme_explains_signed_and_unsigned_releases() -> None:
    text = (ROOT / "installer" / "README.txt").read_text(encoding="utf-8-sig")
    assert "Signed releases" in text and "Podpisane wydania" in text
    assert "Get-AuthenticodeSignature" in text and "Digital Signatures" in text
    assert "Unknown publisher" in text
    assert "unsigned" in text.lower()  # the text for unsigned builds is still there
    assert "In an unsigned release these files are not signed" in text
    assert "SHA256SUMS.txt" in text and "SmartScreen" in text


def test_releasing_md_has_the_code_signing_section() -> None:
    text = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "\n## Code signing\n" in text
    section = text.split("\n## Code signing\n", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "python scripts/build_release.py --sign --sign-thumbprint",
        "python scripts/sign_release.py verify dist/release-signed --expect-thumbprint",
        "python scripts/sign_release.py upload vX.Y.Z dist/release-signed --yes",
        "Get-AuthenticodeSignature",
        "LCSC2FUSION_SIGN_PFX_PASSWORD",
        "2026-10-14",
        "https://shop.certum.eu/code-signing.html",
        "https://shop.certum.eu/open-source-code-signing-on-simplysign.html",
        "https://learn.microsoft.com/azure/trusted-signing/quickstart",
        "https://signpath.org/",
        "SimplySign Desktop",
        "Open Source Developer",
        "KAJPA Kacper Popko",
        "EUR 49",
        "EUR 209",
        "EUR 379",
        "EUR 69",
        "EUR 169",
        "EUR 359",
        "459 days",
        "5000 signatures per month",
        "SmartScreen",
        "SignPath Foundation",
        "Azure Trusted Signing",
        "Never put a PFX file",
        "protected",
        "renewed",
        "macOS",
    ):
        assert needle in section, needle
    assert section.count("\n| --- |") == 1  # one table
    assert "## Signing\n" not in text  # the old stub section is gone
