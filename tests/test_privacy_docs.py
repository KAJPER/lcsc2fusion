"""The Privacy and Code signing policy sections of both READMEs stay true to the code.

The Privacy section lists every host the program contacts and the files it keeps. These tests read
the source files as text, so a new request to a new host, or a new module that opens connections,
fails here until the READMEs (and the allow list below) are updated on purpose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import lcsc2fusion
from lcsc2fusion.client.http import DEFAULT_USER_AGENT

ROOT = Path(__file__).resolve().parents[1]
README_EN = ROOT / "README.md"
README_PL = ROOT / "README.pl.md"

# (policy heading, privacy heading, heading before them, heading after them)
HEADINGS = {
    README_EN: (
        "Code signing policy",
        "Privacy",
        "Limitations and legal",
        "Development",
    ),
    README_PL: (
        "Polityka podpisywania kodu",
        "Prywatność",
        "Ograniczenia i kwestie prawne",
        "Rozwój projektu",
    ),
}

# Files that send or may send data to other computers. Everything else must stay offline.
NETWORK_MODULES = {
    "lcsc2fusion/client/http.py",  # part data, 3D models, datasheet link, search
    "addin/LCSC2Fusion/update_check.py",  # the update check (api.github.com)
    "lcsc2fusion/mcp/fusion_client.py",  # 127.0.0.1 only: the MCP server talks to the add-in
}
NETWORK_PATTERN = re.compile(
    r"urllib\.request|HTTPSConnection|HTTPConnection|create_connection|urlopen|"
    r"\bimport requests\b|\bhttpx\b|\baiohttp\b|smtplib|ftplib|telnetlib"
)

# Hosts that appear in the sources as links or inside the User-Agent, not as request targets.
NOT_REQUEST_TARGETS = {"github.com"}

PRIVACY_FILES = ["state.json", "update.json", "api.json", "cache", "browser-profile", "setup.log"]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body of the `## heading` section, up to the next level 2 heading."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    assert match, f"no '## {heading}' heading"
    start = match.end()
    following = re.search(r"^## ", text[start:], re.M)  # no README code block has such a line
    end = start + following.start() if following else len(text)
    return text[start:end]


def listed_hosts(privacy: str) -> set[str]:
    """The hosts in the first column of the table of the Privacy section."""
    return set(re.findall(r"^\| `([a-z0-9.-]+\.[a-z]+)` \|", privacy, re.M))


def hosts_in_sources() -> set[str]:
    files = [*sorted((ROOT / "lcsc2fusion" / "client").glob("*.py"))]
    files.append(ROOT / "addin" / "LCSC2Fusion" / "update_check.py")
    found: set[str] = set()
    for path in files:
        found.update(re.findall(r"https://([A-Za-z0-9.-]+)", read(path)))
    return {host for host in found if host not in NOT_REQUEST_TARGETS}


@pytest.mark.parametrize("path", [README_EN, README_PL], ids=lambda p: p.name)
def test_policy_and_privacy_sections_sit_between_legal_and_development(path: Path) -> None:
    policy, privacy, before, after = HEADINGS[path]
    text = read(path)
    order = [text.index(f"\n## {name}\n") for name in (before, policy, privacy, after)]
    assert order == sorted(order)


def test_navigation_links_the_new_sections() -> None:
    en = read(README_EN)
    pl = read(README_PL)
    assert '<a href="#code-signing-policy">Code signing policy</a>' in en
    assert '<a href="#privacy">Privacy</a>' in en
    assert '<a href="#polityka-podpisywania-kodu">Polityka podpisywania kodu</a>' in pl
    assert '<a href="#prywatność">Prywatność</a>' in pl


@pytest.mark.parametrize("path", [README_EN, README_PL], ids=lambda p: p.name)
def test_the_privacy_section_lists_exactly_the_hosts_of_the_code(path: Path) -> None:
    privacy = section(read(path), HEADINGS[path][1])
    assert hosts_in_sources() == {
        "easyeda.com",
        "modules.easyeda.com",
        "wmsc.lcsc.com",
        "jlcpcb.com",
        "api.github.com",
    }
    assert listed_hosts(privacy) == hosts_in_sources()


@pytest.mark.parametrize("path", [README_EN, README_PL], ids=lambda p: p.name)
def test_the_privacy_section_names_the_user_agents_and_the_local_files(path: Path) -> None:
    privacy = section(read(path), HEADINGS[path][1])
    # the example uses 0.1.0, the format must be the one of the code
    assert DEFAULT_USER_AGENT.replace(lcsc2fusion.__version__, "0.1.0") in privacy
    assert "LCSC2Fusion/0.1.0" in privacy
    for name in PRIVACY_FILES:
        assert name in privacy, name
    assert "127.0.0.1" in privacy
    assert "default-src 'self'" in privacy
    assert "-RemoveData" in privacy


@pytest.mark.parametrize("path", [README_EN, README_PL], ids=lambda p: p.name)
def test_the_policy_section_does_not_claim_that_signing_is_active(path: Path) -> None:
    policy = section(read(path), HEADINGS[path][0])
    assert "https://signpath.org/" in policy
    assert "@KAJPER" in policy
    assert "Get-AuthenticodeSignature" in policy
    assert "SHA256SUMS.txt" in policy
    # The ready made "Free code signing provided by ..." text is for the day the certificate
    # exists. When signing goes live, change the README and this test together.
    assert "Free code signing provided by" not in policy
    assert "Bezpłatny podpis kodu" not in policy


def test_only_known_modules_open_network_connections() -> None:
    found: set[str] = set()
    for folder in (ROOT / "lcsc2fusion", ROOT / "addin" / "LCSC2Fusion"):
        for path in folder.rglob("*.py"):
            if NETWORK_PATTERN.search(read(path)):
                found.add(path.relative_to(ROOT).as_posix())
    assert found == NETWORK_MODULES, (
        "A module that talks to the network was added or removed. Update NETWORK_MODULES here "
        "and the Privacy section of README.md and README.pl.md."
    )


def test_the_installer_makes_no_network_connection() -> None:
    pattern = re.compile(
        r"Invoke-WebRequest|Invoke-RestMethod|System\.Net|DownloadFile|Start-BitsTransfer|"
        r"certutil|bitsadmin|\bcurl\b|\bwget\b",
        re.I,
    )
    for path in sorted((ROOT / "installer").iterdir()):
        if path.is_file():
            assert not pattern.search(path.read_text(encoding="utf-8-sig")), path.name
