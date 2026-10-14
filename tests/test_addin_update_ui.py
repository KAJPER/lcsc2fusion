"""Tests of the update notice in the add-in window (the page side of the update check).

The Python side (update_check.py, update_service.py, the controller handlers) has its own tests.
Here the page is checked in four layers, all offline and without Fusion:

* the two URL checks of app.js (`window.__lcscPalette.updateReleaseUrl` and `updateInstallerUrl`)
  run under Node against good, hostile and malformed values;
* texts: every key of the notice exists in both dictionaries with the same placeholders, the fixed
  texts of the contract are exact, no dashes, and the one Python sentence has a Polish pattern;
* consistency of ids, field names and actions between controller.py, app.js, index.html,
  style.css and mock-bridge.js (the mock runs under Node and is played through every scenario);
* a real browser (Playwright, found in any Python on PATH) that drives the real page over the real
  HTTP bridge with scripted `update` messages. Skipped when Playwright or Chromium is missing.
  Set LCSC_UPDATE_SHOTS to a folder to keep the screenshots.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import controller
import pytest
from test_addin_http_bridge import _FixtureServer, _python_with_playwright

ROOT = Path(__file__).resolve().parents[1]
PALETTE_DIR = ROOT / "addin" / "LCSC2Fusion" / "palette"
DRIVER = ROOT / "tests" / "js" / "update_ui_browser.py"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
CYRILLIC_I = chr(0x0456)  # looks like a Latin i
ARABIC_INDIC_VERSION = ".".join(chr(0x0660 + digit) for digit in (1, 2, 3))  # looks like 1.2.3
TAG_SENTENCE = "Invalid request: tag must be a release tag such as v1.2.3."
TAG_SENTENCE_PL = "Nieprawidłowe żądanie: tag musi być tagiem wydania, na przykład v1.2.3."
FLAG_SENTENCE = "Invalid request: enabled must be true or false."

RELEASES = "https://github.com/KAJPER/lcsc2fusion/releases/"


def release_url(version: str) -> str:
    return f"{RELEASES}tag/v{version}"


def installer_url(version: str) -> str:
    return f"{RELEASES}download/v{version}/LCSC2Fusion-Setup-{version}.exe"


def js_code(name: str) -> str:
    """A palette script without comments, so that the checks do not trip over prose."""
    text = (PALETTE_DIR / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|(?<=\s)//.*$", "", text)


def run_node(script: str, *args: str) -> Any:
    assert NODE is not None
    completed = subprocess.run(
        [NODE, "-e", script, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# The URL checks of app.js, under Node -------------------------------------------------------------

# app.js loads without a DOM as long as the document is still "loading": init() is deferred and the
# checks are reachable through the read only hook window.__lcscPalette.
CHECKS_SCRIPT = """
global.window = {};
global.document = { readyState: 'loading', addEventListener() {} };
require(process.argv[1]);
const checks = window.__lcscPalette;
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  names: Object.keys(checks).sort(),
  frozen: Object.isFrozen(checks),
  results: cases.map(([name, args]) => checks[name](...args)),
}));
"""


def run_checks(cases: list[tuple[str, list[object]]]) -> list[object]:
    found = run_node(CHECKS_SCRIPT, str(PALETTE_DIR / "app.js"), json.dumps(cases))
    assert isinstance(found["results"], list)
    return list(found["results"])


def check_release(latest: object, url: object) -> object:
    return run_checks([("updateReleaseUrl", [latest, url])])[0]


GOOD_VERSIONS = ["0.0.0", "0.1.0", "0.2.0", "1.2.3", "10.20.30", "999999.999999.999999"]

# What a URL must never be, given latest = 1.2.3. Each is built from the one good URL.
GOOD = "1.2.3"


def hostile_variants(good: str) -> list[str]:
    """Spellings of `good` that must all be refused (the same host, scheme and path rules)."""
    assert good.startswith("https://github.com/KAJPER/lcsc2fusion/")
    rest = good.removeprefix("https://github.com/KAJPER/lcsc2fusion/")
    return [
        "",
        " ",
        "javascript:alert(1)",
        "javascript:" + good,
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///C:/Windows/win.ini",
        "//github.com/KAJPER/lcsc2fusion/" + rest,
        "github.com/KAJPER/lcsc2fusion/" + rest,
        good.replace("https://", "http://"),
        good.replace("https://", "HTTPS://"),
        good.replace("github.com", "GITHUB.COM"),
        good.replace("github.com", "Github.com"),
        good.replace("github.com", "evil.example"),
        good.replace("github.com", "github.com.evil.example"),
        good.replace("github.com", "evil-github.com"),
        good.replace("github.com", "api.github.com"),
        good.replace("github.com", "www.github.com"),
        good.replace("github.com", "raw.githubusercontent.com"),
        good.replace("https://github.com", "https://github.com@evil.example"),
        good.replace("https://", "https://user@"),
        good.replace("https://", "https://user:secret@"),
        good.replace("github.com", "github.com:8443"),
        good.replace("github.com", "github.com:443"),
        good.replace("github.com", f"g{CYRILLIC_I}thub.com"),
        good.replace("KAJPER", "kajper"),
        good.replace("KAJPER", "OTHER"),
        good.replace("lcsc2fusion", "lcsc2fusion2"),
        good.replace("lcsc2fusion", "LCSC2Fusion"),
        good.replace("/releases/", "/release/"),
        good.replace("/releases/", "/releases//"),
        good + "/",
        good + "?x=1",
        good + "?next=https://evil.example",
        good + "#frag",
        good + "#.exe",
        good + " ",
        " " + good,
        good + "\n",
        "\n" + good,
        good + "\r\n",
        good + "\x00",
        good + "%20",
        good + "/..",
        good + "/../",
        good.replace("/releases/", "/releases/../releases/"),
        good.replace("/releases/", "/releases/./"),
        good.replace("/releases/", "\\releases\\"),
        good.replace("lcsc2fusion/", "lcsc2fusion%2f"),
        good.replace("/KAJPER/", "/KAJPER/../KAJPER/"),
        good.replace("/KAJPER/", "//KAJPER/"),
        good.replace("1.2.3", "1.2.3%2f..%2f1.2.4"),
        good.replace("1.2.3", "1.2.3.html"),
        good + good,
    ]


@needs_node
def test_the_two_checks_are_on_the_read_only_hook_next_to_the_link_checks() -> None:
    found = run_node(CHECKS_SCRIPT, str(PALETTE_DIR / "app.js"), "[]")
    assert found["names"] == sorted(
        ["datasheetUrl", "lcscProductUrl", "updateInstallerUrl", "updateReleaseUrl"]
    )
    assert found["frozen"] is True


@needs_node
def test_release_url_check_accepts_exactly_the_url_built_from_the_version() -> None:
    cases = [("updateReleaseUrl", [version, release_url(version)]) for version in GOOD_VERSIONS]
    assert run_checks(cases) == [release_url(version) for version in GOOD_VERSIONS]


@needs_node
def test_release_url_check_refuses_hostile_and_malformed_urls() -> None:
    hostile = hostile_variants(release_url(GOOD))
    assert len(hostile) >= 50
    assert len(set(hostile)) == len(hostile)
    results = run_checks([("updateReleaseUrl", [GOOD, url]) for url in hostile])
    assert results == [""] * len(hostile)


@needs_node
def test_release_url_check_refuses_a_tag_of_another_shape_or_another_version() -> None:
    wrong_tags = [
        "v1.2",
        "1.2.3",
        "V1.2.3",
        "vv1.2.3",
        "v1.2.3-evil",
        "v1.2.3-rc1",
        "v1.2.3+build",
        "v1.2.3.4",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1234567.2.3",
        "v1.2.x",
        "v1.2.3/",
        "v1.2.3%0a",
        "latest",
        "",
        "v1.2.4",  # a different version than `latest`
        "v1.2.30",
        "v11.2.3",
        "v0.2.3",
    ]
    urls = [f"{RELEASES}tag/{tag}" for tag in wrong_tags]
    urls += [
        installer_url(GOOD),  # an installer URL is not the release page
        f"{RELEASES}latest",
        f"{RELEASES}tag/",
        f"{RELEASES}download/v{GOOD}",
        f"{RELEASES}",
        "https://github.com/KAJPER/lcsc2fusion",
    ]
    results = run_checks([("updateReleaseUrl", [GOOD, url]) for url in urls])
    assert results == [""] * len(urls)


@needs_node
def test_a_latest_that_is_not_x_y_z_gives_no_link_even_when_the_url_matches_it() -> None:
    wrong = [
        "1.2",
        "v1.2.3",
        "1.2.3-evil",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1234567.2.3",
        "1.2.3\n",
        " 1.2.3",
        "1.2.3 ",
        "1.2.x",
        "",
        "1.2.3/../x",
        ARABIC_INDIC_VERSION,
        "1.2.3?x",
        "<b>1.2.3</b>",
    ]
    cases: list[tuple[str, list[object]]] = []
    for latest in wrong:
        cases.append(("updateReleaseUrl", [latest, f"{RELEASES}tag/v{latest}"]))
        cases.append(("updateInstallerUrl", [latest, installer_url(latest)]))
    assert run_checks(cases) == [""] * len(cases)


@needs_node
def test_installer_url_check_accepts_exactly_the_asset_of_the_version() -> None:
    cases = [("updateInstallerUrl", [version, installer_url(version)]) for version in GOOD_VERSIONS]
    assert run_checks(cases) == [installer_url(version) for version in GOOD_VERSIONS]


@needs_node
def test_installer_url_check_refuses_hostile_and_malformed_urls() -> None:
    hostile = hostile_variants(installer_url(GOOD))
    results = run_checks([("updateInstallerUrl", [GOOD, url]) for url in hostile])
    assert results == [""] * len(hostile)


@needs_node
def test_installer_url_check_refuses_another_asset_or_another_version() -> None:
    base = f"{RELEASES}download/v{GOOD}/"
    urls = [
        base + "LCSC2Fusion-Setup-1.2.3.exe.zip",
        base + "LCSC2Fusion-Setup-1.2.3.msi",
        base + "LCSC2Fusion-Setup-1.2.3.EXE",
        base + "lcsc2fusion-setup-1.2.3.exe",
        base + "LCSC2Fusion-Setup-1.2.3",
        base + "LCSC2Fusion-Setup-.exe",
        base + "LCSC2Fusion-Setup-1.2.4.exe",  # another version in the file name
        base + "LCSC2Fusion-Setup-1.2.30.exe",
        base + "LCSC2Fusion-Setup-1.2.3-evil.exe",
        base + "LCSC2Fusion-Setup-v1.2.3.exe",
        base + "LCSC2Fusion-1.2.3-addin.zip",
        base + "LCSC2Fusion-Setup-1.2.3.exe.sha256",
        base + "evil.exe",
        base + "sub/LCSC2Fusion-Setup-1.2.3.exe",
        base + "../LCSC2Fusion-Setup-1.2.3.exe",
        base + "..%2fLCSC2Fusion-Setup-1.2.3.exe",
        base + "LCSC2Fusion-Setup-1.2.3.exe/",
        base + "LCSC2Fusion-Setup-1.2.3.exe/evil.exe",
        base + "LCSC2Fusion-Setup-1.2.3.exe\\evil.exe",
        base,
        base.rstrip("/"),
        # another tag in the path than in the file name, or than `latest`
        f"{RELEASES}download/v1.2.4/LCSC2Fusion-Setup-1.2.3.exe",
        f"{RELEASES}download/v1.2.3/../v1.2.4/LCSC2Fusion-Setup-1.2.3.exe",
        installer_url("1.2.4"),
        f"{RELEASES}download/1.2.3/LCSC2Fusion-Setup-1.2.3.exe",  # no v in the tag
        f"{RELEASES}download/V1.2.3/LCSC2Fusion-Setup-1.2.3.exe",
        f"{RELEASES}download/v1.2/LCSC2Fusion-Setup-1.2.3.exe",
        f"{RELEASES}download/v01.2.3/LCSC2Fusion-Setup-01.2.3.exe",
        f"{RELEASES}download/v1.2.3-evil/LCSC2Fusion-Setup-1.2.3-evil.exe",
        f"{RELEASES}tag/v1.2.3/LCSC2Fusion-Setup-1.2.3.exe",  # the tag page, not the download
        f"{RELEASES}latest/download/LCSC2Fusion-Setup-1.2.3.exe",
        release_url(GOOD),  # the release page is not the installer
    ]
    assert len(set(urls)) == len(urls)
    results = run_checks([("updateInstallerUrl", [GOOD, url]) for url in urls])
    assert results == [""] * len(urls)


@needs_node
def test_the_url_checks_refuse_values_that_are_not_strings() -> None:
    values: list[object] = [
        None,
        True,
        False,
        0,
        12,
        1.5,
        [],
        ["x"],
        [release_url(GOOD)],
        {},
        {"href": release_url(GOOD)},
    ]
    cases: list[tuple[str, list[object]]] = []
    for value in values:
        cases.append(("updateReleaseUrl", [GOOD, value]))
        cases.append(("updateInstallerUrl", [GOOD, value]))
        cases.append(("updateReleaseUrl", [value, release_url(GOOD)]))
        cases.append(("updateInstallerUrl", [value, installer_url(GOOD)]))
    cases += [("updateReleaseUrl", []), ("updateInstallerUrl", [GOOD])]  # arguments missing
    assert run_checks(cases) == [""] * len(cases)


# Texts: both dictionaries, the contract, the one Python sentence ---------------------------------

DICTIONARY_SCRIPT = """
global.window = {};
require(process.argv[1]);
const i18n = window.LcscI18n;
const texts = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  dictionaries: i18n.DICTIONARIES,
  translated: texts.map((text) => ({
    found: i18n.findMessage(text) !== null,
    pl: i18n.translateMessage(text, 'pl'),
    en: i18n.translateMessage(text, 'en'),
  })),
}));
"""

# The fixed texts of the contract (English, Polish). The page may add keys but not reword these.
CONTRACT_TEXTS: dict[str, tuple[str, str]] = {
    "update.banner": (
        "Version {latest} is available (you have {current}).",
        "Dostępna jest wersja {latest} (masz {current}).",
    ),
    "update.download": ("Download installer", "Pobierz instalator"),
    "update.notes": ("What is new", "Co nowego"),
    "update.dismiss": ("Dismiss", "Ukryj"),
    "update.dismissTitle": (
        "Hide this notice until a newer version appears",
        "Ukryj to powiadomienie do czasu wydania nowszej wersji",
    ),
    "update.hint": (
        "To update: run the downloaded installer, then in Fusion press Shift+S, open Add-Ins, "
        "select LCSC2Fusion, press Stop, then Run.",
        "Aby zaktualizować: uruchom pobrany instalator, a potem we Fusion naciśnij Shift+S, "
        "otwórz kartę Add-Ins, wybierz LCSC2Fusion, kliknij Stop, a następnie Run.",
    ),
    "update.hintNoInstaller": (
        "To update: download the installer from the release page and run it, then in Fusion press "
        "Shift+S, open Add-Ins, select LCSC2Fusion, press Stop, then Run.",
        "Aby zaktualizować: pobierz instalator ze strony wydania i uruchom go, a potem we Fusion "
        "naciśnij Shift+S, otwórz kartę Add-Ins, wybierz LCSC2Fusion, kliknij Stop, a następnie "
        "Run.",
    ),
    "update.group": ("Updates", "Aktualizacje"),
    "update.setting": ("Check for updates once a day", "Sprawdzaj aktualizacje raz dziennie"),
    "update.privacy": (
        "Asks api.github.com for the latest release. Nothing about you is sent.",
        "Pyta api.github.com o najnowsze wydanie. Nic o Tobie nie jest wysyłane.",
    ),
    "update.checkNow": ("Check now", "Sprawdź teraz"),
    "update.checking": ("Checking...", "Sprawdzanie..."),
    "update.result.current": (
        "You have the latest version ({current}).",
        "Masz najnowszą wersję ({current}).",
    ),
    "update.result.available": (
        "Version {latest} is available.",
        "Dostępna jest wersja {latest}.",
    ),
    "update.result.error": (
        "Could not check for updates. Try again later.",
        "Nie udało się sprawdzić aktualizacji. Spróbuj ponownie później.",
    ),
}
# Keys the page added to the contract (accessible names and tooltips, the time of the last check).
EXTRA_KEYS = {
    "update.downloadLabel",
    "update.downloadTitle",
    "update.notesLabel",
    "update.notesTitle",
    "update.lastChecked",
}


@pytest.fixture(scope="module")
def dumped() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("node is not installed")
    found = run_node(
        DICTIONARY_SCRIPT, str(PALETTE_DIR / "i18n.js"), json.dumps([TAG_SENTENCE, FLAG_SENTENCE])
    )
    assert isinstance(found, dict)
    return found


def placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


def test_the_contract_texts_are_exact_in_both_languages(dumped: dict[str, Any]) -> None:
    english = dumped["dictionaries"]["en"]
    polish = dumped["dictionaries"]["pl"]
    for key, (en_text, pl_text) in CONTRACT_TEXTS.items():
        assert english[key] == en_text, key
        assert polish[key] == pl_text, key


def test_the_notice_has_exactly_the_contract_keys_and_the_added_ones(
    dumped: dict[str, Any],
) -> None:
    for language in ("en", "pl"):
        keys = {key for key in dumped["dictionaries"][language] if key.startswith("update.")}
        assert keys == set(CONTRACT_TEXTS) | EXTRA_KEYS, language


def test_every_notice_key_has_the_same_placeholders_and_is_translated(
    dumped: dict[str, Any],
) -> None:
    english = dumped["dictionaries"]["en"]
    polish = dumped["dictionaries"]["pl"]
    for key in sorted(set(CONTRACT_TEXTS) | EXTRA_KEYS):
        assert isinstance(english[key], str), key  # no plural form here
        assert isinstance(polish[key], str), key
        assert placeholders(english[key]) == placeholders(polish[key]), key
        assert polish[key] != english[key], f"{key} is not translated"
        assert polish[key] == polish[key].strip() and polish[key], key


def test_the_added_texts_name_the_version_and_say_that_a_browser_tab_opens(
    dumped: dict[str, Any],
) -> None:
    english = dumped["dictionaries"]["en"]
    polish = dumped["dictionaries"]["pl"]
    for key in ("update.downloadLabel", "update.downloadTitle"):
        assert placeholders(english[key]) == {"latest"}, key
    for key in ("update.notesLabel", "update.notesTitle"):
        assert placeholders(english[key]) == {"latest"}, key
    assert placeholders(english["update.lastChecked"]) == {"when"}
    # the visible text comes first in the accessible name (WCAG label in name)
    assert english["update.downloadLabel"].startswith(english["update.download"])
    assert english["update.notesLabel"].startswith(english["update.notes"])
    assert polish["update.downloadLabel"].startswith(polish["update.download"])
    assert polish["update.notesLabel"].startswith(polish["update.notes"])
    for key in ("update.downloadTitle", "update.notesTitle"):
        assert "browser" in english[key]
        assert "przeglądarce" in polish[key]


def test_no_text_of_the_notice_has_a_dash_or_markup(dumped: dict[str, Any]) -> None:
    for language in ("en", "pl"):
        for key, text in dumped["dictionaries"][language].items():
            if not key.startswith("update."):
                continue
            assert EM_DASH not in text and EN_DASH not in text, (language, key)
            assert "--" not in text, (language, key)
            assert "<" not in text and ">" not in text, (language, key)


def test_the_polish_texts_of_the_notice_have_real_diacritics(dumped: dict[str, Any]) -> None:
    polish = dumped["dictionaries"]["pl"]
    joined = " ".join(text for key, text in polish.items() if key.startswith("update."))
    for char in "ąćęłóśź":
        assert char in joined, char
    assert "�" not in joined
    assert not re.search(r"[ÃÅÄ][\x80-\xbf]", joined)


def test_the_texts_of_the_release_stay_off_the_page() -> None:
    # Only version numbers and our own texts are ever shown: no field of GitHub's release text.
    app = js_code("app.js")
    for word in ("body", "release_notes", "notes_text", "name:", "html_url", "published_at"):
        assert not re.search(rf"\bdata\.{re.escape(word.rstrip(':'))}\b", app), word


@needs_node
def test_the_python_sentence_about_a_bad_tag_has_a_polish_pattern(dumped: dict[str, Any]) -> None:
    tag, flag = dumped["translated"]
    assert tag["found"] is True
    assert tag["pl"] == TAG_SENTENCE_PL
    assert tag["en"] == TAG_SENTENCE  # English is never touched
    assert flag["found"] is True  # the existing pattern with \w+ covers `enabled`
    assert flag["pl"] == "Nieprawidłowe żądanie: enabled musi mieć wartość true albo false."


def test_the_controller_raises_the_sentence_that_the_pattern_translates() -> None:
    source = (ROOT / "addin" / "LCSC2Fusion" / "controller.py").read_text(encoding="utf-8")
    assert f'raise _BadRequest("{TAG_SENTENCE}")' in source
    assert 'f"Invalid request: {key} must be true or false."' in source


# Consistency: ids, fields and actions between controller.py, app.js, index.html, the mock ------


def action_pairs(source: str, name: str) -> dict[str, str]:
    match = re.search(rf"const {name} = Object\.freeze\(\{{(.*?)\}}\)", source, re.DOTALL)
    assert match is not None, f"{name} block not found in app.js"
    return dict(re.findall(r"(\w+):\s*'([a-z_]+)'", match.group(1)))


def test_app_js_has_the_update_actions_and_they_match_the_controller() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    to_python = action_pairs(source, "TO_PYTHON")
    from_python = action_pairs(source, "FROM_PYTHON")
    assert to_python["CHECK_UPDATE"] == controller.ACTION_CHECK_UPDATE == "check_update"
    assert to_python["DISMISS_UPDATE"] == controller.ACTION_DISMISS_UPDATE == "dismiss_update"
    assert to_python["SET_UPDATE_CHECK"] == controller.ACTION_SET_UPDATE_CHECK == "set_update_check"
    assert from_python["UPDATE"] == controller.ACTION_UPDATE == "update"
    assert set(to_python.values()) == set(controller.INCOMING_ACTIONS)
    assert set(from_python.values()) == set(controller.OUTGOING_ACTIONS)
    for group, pairs in (("TO_PYTHON", to_python), ("FROM_PYTHON", from_python)):
        for key in pairs:
            assert f"{group}.{key}" in source, f"{group}.{key} is defined but never used"


def send_keys(source: str, action: str) -> tuple[set[str], str]:
    """The keys of the object literal of `send(TO_PYTHON.<action>, {...})`, and its text."""
    match = re.search(rf"send\(TO_PYTHON\.{action}, \{{([^}}]*)\}}\)", source)
    assert match is not None, f"send(TO_PYTHON.{action}, {{...}}) not found in app.js"
    keys = {part.split(":")[0].strip() for part in match.group(1).split(",") if part.strip()}
    return keys, match.group(1)


def test_the_three_requests_carry_exactly_the_controller_fields() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    keys, body = send_keys(source, "CHECK_UPDATE")
    assert keys == set(controller.CHECK_UPDATE_REQUEST_FIELDS)
    assert "force: true" in body  # "Check now" is a forced check
    keys, body = send_keys(source, "DISMISS_UPDATE")
    assert keys == set(controller.DISMISS_UPDATE_REQUEST_FIELDS)
    assert "tag: 'v' + info.latest" in body  # the tag of the version that the banner names
    keys, body = send_keys(source, "SET_UPDATE_CHECK")
    assert keys == set(controller.SET_UPDATE_CHECK_REQUEST_FIELDS)
    assert "enabled: enabled" in body
    # the value is the state of the box: a real boolean, taken from the checkbox
    assert "setUpdateCheck(event.target.checked)" in source


def test_the_dismiss_tag_has_the_shape_the_controller_demands() -> None:
    # the page only ever sends 'v' + a version that passed VERSION_PATTERN
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const VERSION_PATTERN = /(.*?)/;", source)
    assert match is not None
    page = match.group(1)
    assert page == r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
    from update_check import parse_tag

    assert parse_tag("v0.2.0") is not None
    assert parse_tag("v01.2.3") is None  # the same rule as the page: no leading zeros
    assert parse_tag("0.2.0") is None


def test_the_states_of_the_page_are_the_states_of_the_controller() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const UPDATE_STATES = Object\.freeze\(\[(.*?)\]\)", source)
    assert match is not None
    assert tuple(re.findall(r"'([a-z]+)'", match.group(1))) == controller.UPDATE_STATES


def test_the_page_reads_every_field_of_the_update_message() -> None:
    app = js_code("app.js")
    start = app.index("function applyUpdate(")
    body = app[start : app.index("\n  }\n", start)]
    for field in controller.UPDATE_FIELDS:
        assert re.search(rf"\bdata\.{field}\b", body), f"applyUpdate never reads data.{field}"


def test_links_of_the_notice_are_built_by_the_one_link_helper_from_checked_urls() -> None:
    app = js_code("app.js")
    assert "const releaseOk" not in app
    # a URL of the message reaches an href only through its check and externalLink
    assert "release: updateReleaseUrl(latest, update.release_url)" in app
    assert "installer: updateInstallerUrl(latest, update.installer_url)" in app
    assert len(re.findall(r"\.release_url\b", app)) == 2  # the copy of the message and the check
    assert len(re.findall(r"\.installer_url\b", app)) == 2
    assert "externalLink(\n        'update-download',\n        info.installer," in app
    assert "externalLink(\n        'update-notes',\n        info.release," in app
    assert len(re.findall(r"\.href = ", app)) == 1  # still the one place in externalLink
    assert "innerHTML" not in app
    assert "insertAdjacentHTML" not in app
    # GitHub is only ever named as the prefix of these two checked shapes
    assert app.count("https://github.com/") == 1
    assert "const RELEASES_URL = 'https://github.com/KAJPER/lcsc2fusion/releases/';" in app


def test_the_update_notice_adds_no_network_access_or_storage_to_the_page() -> None:
    app = js_code("app.js")
    for call in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "window.open("):
        assert call not in app, call
    for word in ("localStorage", "sessionStorage", "indexedDB"):
        assert word not in app, word


class _Page(HTMLParser):
    """Start tags of index.html in document order, with their attributes and the open ancestors."""

    VOID = frozenset({"meta", "link", "img", "input", "br", "hr"})

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None], tuple[str, ...]]] = []
        self.text: dict[str, str] = {}
        self._open: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        ancestors = tuple(name for _, name in self._open if name)
        self.tags.append((tag, attributes, ancestors))
        if tag not in self.VOID:
            self._open.append((tag, attributes.get("id")))

    def handle_endtag(self, tag: str) -> None:
        while self._open:
            name, _ = self._open.pop()
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        for _, element_id in self._open:
            if element_id:
                self.text[element_id] = self.text.get(element_id, "") + data

    def find(self, element_id: str) -> tuple[str, dict[str, str | None], tuple[str, ...]]:
        found = next((t for t in self.tags if t[1].get("id") == element_id), None)
        assert found is not None, f"#{element_id} is missing from index.html"
        return found


def page() -> _Page:
    collector = _Page()
    collector.feed((PALETTE_DIR / "index.html").read_text(encoding="utf-8"))
    return collector


UPDATE_IDS = (
    "update-banner",
    "update-text",
    "update-actions",
    "update-dismiss",
    "update-hint",
    "update-enabled",
    "update-privacy",
    "update-check",
    "update-result",
    "update-checked",
)


def test_every_element_id_of_the_notice_exists_and_app_js_uses_the_ones_it_needs() -> None:
    collector = page()
    for element_id in UPDATE_IDS:
        collector.find(element_id)
    used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js_code("app.js")))
    needed = {i for i in UPDATE_IDS if i != "update-privacy"}  # that one is aria-describedby only
    assert needed <= used, sorted(needed - used)
    assert [name for name in used if collector.find(name) is None] == []


def test_the_banner_is_a_hidden_status_region_above_the_search_form_under_the_settings() -> None:
    collector = page()
    tag, attrs, _ancestors = collector.find("update-banner")
    assert tag == "section"
    assert attrs["role"] == "status"
    assert "hidden" in attrs  # app.js shows it only for a newer, valid version
    assert attrs["class"] == "update-banner"
    order = [attrs.get("id") for _tag, attrs, _ in collector.tags if attrs.get("id")]
    assert order.index("settings-panel") < order.index("update-banner") < order.index("search-form")
    assert order.index("update-banner") < order.index("update-text")
    # inside it: the text, the actions with the Dismiss button last, the hint
    assert order.index("update-text") < order.index("update-actions") < order.index("update-hint")
    dismiss_tag, dismiss, ancestors = collector.find("update-dismiss")
    assert dismiss_tag == "button"
    assert dismiss["type"] == "button"
    assert "small" in str(dismiss["class"]).split()
    assert dismiss["data-i18n"] == "update.dismiss"
    assert dismiss["data-i18n-attr"] == "title:update.dismissTitle"
    assert dismiss["title"] == CONTRACT_TEXTS["update.dismissTitle"][0]
    assert "update-actions" in ancestors
    assert collector.text["update-dismiss"].strip() == "Dismiss"
    # the text, the hint and the links are written by app.js: no static key, no static text
    for element_id in ("update-text", "update-hint", "update-result", "update-checked"):
        _tag, attributes, _ = collector.find(element_id)
        assert "data-i18n" not in attributes, element_id
        assert collector.text.get(element_id, "").strip() == "", element_id
    assert not [t for t in collector.tags if t[0] == "a" and "update-banner" in t[2]]


def test_the_updates_group_is_the_third_group_of_the_settings_panel() -> None:
    collector = page()
    _tag, _attrs, ancestors = collector.find("update-enabled")
    assert "settings-panel" in ancestors
    groups = [
        attrs
        for tag, attrs, parents in collector.tags
        if tag == "fieldset" and "settings-panel" in parents
    ]
    assert len(groups) == 3
    assert "setting-updates" in str(groups[2]["class"]).split()
    legends = [
        attrs.get("data-i18n")
        for tag, attrs, parents in collector.tags
        if tag == "legend" and "settings-panel" in parents
    ]
    assert legends == ["settings.theme", "settings.language", "update.group"]


def test_the_checkbox_and_the_button_are_labelled_and_the_note_is_attached() -> None:
    collector = page()
    tag, box, _ = collector.find("update-enabled")
    assert (tag, box["type"], box["name"]) == ("input", "checkbox", "update_check")
    assert "checked" in box  # the default is "on" (a missing update.json counts as on)
    assert box["aria-describedby"] == "update-privacy"
    label = next(
        a for t, a, _ in collector.tags if t == "label" and a.get("for") == "update-enabled"
    )
    assert label["class"] == "check"
    privacy = collector.find("update-privacy")[1]
    assert privacy["data-i18n"] == "update.privacy"
    assert collector.text["update-privacy"].strip() == CONTRACT_TEXTS["update.privacy"][0]
    tag, button, _ = collector.find("update-check")
    assert (tag, button["type"], button["data-i18n"]) == ("button", "button", "update.checkNow")
    assert "primary" not in str(button.get("class"))  # a quiet button in the settings
    assert button["aria-describedby"] == "update-result"
    result = collector.find("update-result")[1]
    assert result["role"] == "status"  # the answer is announced
    assert "hidden" not in result  # a live region that exists before its text does
    assert "hidden" in collector.find("update-checked")[1]


def test_the_default_english_texts_of_the_markup_are_the_contract_texts() -> None:
    collector = page()
    pairs = {
        "update-privacy": "update.privacy",
        "update-check": "update.checkNow",
        "update-dismiss": "update.dismiss",
    }
    for element_id, key in pairs.items():
        assert " ".join(collector.text[element_id].split()) == CONTRACT_TEXTS[key][0], element_id
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    assert f'<legend data-i18n="update.group">{CONTRACT_TEXTS["update.group"][0]}</legend>' in html
    assert f'data-i18n="update.setting">{CONTRACT_TEXTS["update.setting"][0]}</span>' in html


def test_every_update_key_that_app_js_uses_exists_and_none_is_dead(dumped: dict[str, Any]) -> None:
    known = {key for key in dumped["dictionaries"]["en"] if key.startswith("update.")}
    used_by_code = set(re.findall(r"'(update\.[A-Za-z.]+)'", js_code("app.js")))
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    used_by_html = set(re.findall(r'data-i18n="(update\.[A-Za-z.]+)"', html))
    used_by_html |= set(re.findall(r"(?:title|aria-label):(update\.[A-Za-z.]+)", html))
    assert used_by_code <= known, sorted(used_by_code - known)
    assert used_by_html <= known, sorted(used_by_html - known)
    assert known - used_by_code - used_by_html == set()
    # no key is made up from pieces at run time
    assert not re.search(r"'update\.'\s*\+", js_code("app.js"))


def test_a_language_switch_redraws_the_notice_from_state() -> None:
    app = js_code("app.js")
    body = app[app.index("function retranslate()") :]
    body = body[: body.index("\n  }\n")]
    assert "renderUpdate()" in body
    # the state holds codes, URLs and numbers, never a rendered text
    start = app.index("function applyUpdate(")
    applied = app[start : app.index("\n  }\n", start)]
    assert "t(" not in applied.replace("textKey(", "").replace("setText(", "")
    for stored in ("updateChecking", "manualUpdateCheck", "updateLine", "updateCheckedAt"):
        assert f"{stored}:" in app  # in the state object


def test_the_mock_handles_the_three_new_requests_and_the_page_keeps_the_old_ones() -> None:
    mock = (PALETTE_DIR / "mock-bridge.js").read_text(encoding="utf-8")
    handled = set(re.findall(r"case '([a-z_]+)':", mock))
    assert {"check_update", "dismiss_update", "set_update_check"} <= handled
    assert set(controller.INCOMING_ACTIONS) <= handled
    assert "deliver('update'," in mock


# The mock bridge under Node ---------------------------------------------------------------------

MOCK_SCRIPT = r"""
global.window = {};
global.document = { readyState: 'loading', addEventListener() {} };
const realSetTimeout = setTimeout;
global.setTimeout = (fn, ms) => realSetTimeout(fn, Math.floor(ms / 25));  // run the mock fast
const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));
require(process.argv[1]);   // mock-bridge.js
require(process.argv[2]);   // app.js, for its URL checks
const checks = window.__lcscPalette;
const queries = JSON.parse(process.argv[3]);

(async () => {
  const report = {};
  for (const query of queries) {
    if (query === null) delete global.location;     // Node has no location at all
    else global.location = { search: query };
    const messages = [];
    const bridge = window.LcscMock.install((action, data) => {
      if (action === 'update' || action === 'error') messages.push([action, data]);
    });
    const steps = [];
    let seen = 0;  // every message lands in exactly one step, the later ones in a "settled" step
    const record = (action, payload, reply) => {
      steps.push({ action, payload, reply, messages: messages.slice(seen) });
      seen = messages.length;
    };
    const send = async (action, payload) => {
      const reply = await bridge.fusionSendData(action, JSON.stringify(payload));
      await sleep(60);
      record(action, payload, reply);
    };
    await send('ready', {});
    await sleep(80);
    record('settled', null, null);
    await send('check_update', { force: true });
    await sleep(60);
    record('settled', null, null);
    await send('dismiss_update', { tag: 'v0.2.0' });
    await send('dismiss_update', { tag: 'v01.2.3' });
    await send('dismiss_update', { tag: 5 });
    await send('dismiss_update', {});
    await send('set_update_check', { enabled: false });
    await send('set_update_check', { enabled: 'yes' });
    await send('set_update_check', {});
    await send('set_update_check', { enabled: true });
    const urls = messages
      .filter(([action]) => action === 'update')
      .map(([, m]) => ({
        state: m.state,
        release: m.release_url === null ? null
          : checks.updateReleaseUrl(m.latest, m.release_url),
        installer: m.installer_url === null ? null
          : checks.updateInstallerUrl(m.latest, m.installer_url),
        release_url: m.release_url,
        installer_url: m.installer_url,
      }));
    report[String(query)] = { steps, urls };
  }
  console.log(JSON.stringify(report));
})();
"""

MOCK_QUERIES = [
    None,
    "",
    "?update=current",
    "?update=available",
    "?update=available-noinstaller",
    "?update=dismissed",
    "?update=disabled",
    "?update=checking",
    "?update=error",
    "?update=nonsense",
    "?foo=1&update=available&update_after=error",
    "?update=current&update_after=available",
    "?update=available&update_after=checking",
]


@pytest.fixture(scope="module")
def mock_runs() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("node is not installed")
    found = run_node(
        MOCK_SCRIPT,
        str(PALETTE_DIR / "mock-bridge.js"),
        str(PALETTE_DIR / "app.js"),
        json.dumps(MOCK_QUERIES),
    )
    assert isinstance(found, dict)
    return found


def run_key(query: str | None) -> str:
    """The key of a query in the report of MOCK_SCRIPT (String(null) is "null" in JS)."""
    return "null" if query is None else query


def updates_of(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [data for action, data in step["messages"] if action == "update"]


def all_updates(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [message for step in run["steps"] for message in updates_of(step)]


def test_every_mock_update_message_has_exactly_the_controller_fields(
    mock_runs: dict[str, Any],
) -> None:
    for query, run in mock_runs.items():
        messages = all_updates(run)
        assert len(messages) >= 5, query
        for message in messages:
            assert set(message) == set(controller.UPDATE_FIELDS), query
            assert message["state"] in controller.UPDATE_STATES, query
            assert message["current"] == "0.1.0", query
            assert message["latest"] is None or isinstance(message["latest"], str), query
            for field in ("release_url", "installer_url"):
                assert message[field] is None or isinstance(message[field], str), (query, field)
            assert isinstance(message["checked_at"], float | int), query
            assert isinstance(message["enabled"], bool), query
            assert isinstance(message["dismissed"], bool), query
            if message["dismissed"]:
                assert message["state"] == "available", query  # the contract


def test_the_mock_urls_pass_the_checks_of_the_page_like_the_ones_python_builds(
    mock_runs: dict[str, Any],
) -> None:
    for query, run in mock_runs.items():
        for entry in run["urls"]:
            assert entry["release"] == entry["release_url"], query
            assert entry["installer"] == entry["installer_url"], query
    available = mock_runs["?update=available"]["steps"][0]["messages"][0][1]
    assert available["release_url"] == release_url("0.2.0")
    assert available["installer_url"] == installer_url("0.2.0")


@pytest.mark.parametrize(
    ("query", "state", "dismissed", "installer", "enabled"),
    [
        (None, "current", False, None, True),  # no location under Node: the default
        ("", "current", False, None, True),
        ("?update=current", "current", False, None, True),
        ("?update=nonsense", "current", False, None, True),  # an unknown kind: the default
        ("?update=available", "available", False, True, True),
        ("?update=available-noinstaller", "available", False, False, True),
        ("?update=dismissed", "available", True, True, True),
        ("?update=disabled", "disabled", False, None, False),
        ("?update=checking", "available", False, True, True),
        ("?update=error", "available", False, True, True),
    ],
)
def test_the_mock_ready_message_follows_the_query(
    mock_runs: dict[str, Any],
    query: str | None,
    state: str,
    dismissed: bool,
    installer: bool | None,
    enabled: bool,
) -> None:
    ready = mock_runs[run_key(query)]["steps"][0]
    first = updates_of(ready)[0]
    assert first["state"] == state
    assert first["dismissed"] is dismissed
    assert first["enabled"] is enabled
    if state == "available":
        assert first["latest"] == "0.2.0"
        assert first["release_url"] == release_url("0.2.0")
        assert (first["installer_url"] == installer_url("0.2.0")) is installer
        assert (first["installer_url"] is None) is (installer is False)
    assert ready["reply"] == "OK"


def test_the_mock_plays_a_running_and_a_failing_automatic_check_after_ready(
    mock_runs: dict[str, Any],
) -> None:
    def before_the_press(query: str) -> list[str]:
        steps = mock_runs[query]["steps"]
        end = next(i for i, step in enumerate(steps) if step["action"] == "check_update")
        return [m["state"] for step in steps[:end] for m in updates_of(step)]

    # the cached knowledge, then the automatic check: it never ends, or it fails
    assert before_the_press("?update=checking")[:2] == ["available", "checking"]
    assert before_the_press("?update=error")[:3] == ["available", "checking", "error"]
    assert before_the_press("?update=available") == ["available"]
    assert before_the_press("?update=current") == ["current"]


def test_the_mock_check_now_plays_checking_and_then_the_chosen_result(
    mock_runs: dict[str, Any],
) -> None:
    def states_after_press(query: str) -> list[str]:
        step = next(s for s in mock_runs[query]["steps"] if s["action"] == "check_update")
        assert step["payload"] == {"force": True}
        assert step["reply"] == "OK"
        return [m["state"] for m in updates_of(step)]

    assert states_after_press("?update=current")[:2] == ["checking", "current"]
    assert states_after_press("?update=available")[:2] == ["checking", "available"]
    assert states_after_press("?update=current&update_after=available")[:2] == [
        "checking",
        "available",
    ]
    assert states_after_press("?foo=1&update=available&update_after=error")[:2] == [
        "checking",
        "error",
    ]
    assert states_after_press("?update=available&update_after=checking") == ["checking"]
    assert states_after_press("?update=disabled")[:2] == ["checking", "current"]


def test_the_mock_dismiss_and_switch_are_answered_like_the_controller(
    mock_runs: dict[str, Any],
) -> None:
    run = mock_runs["?update=available"]
    steps = [s for s in run["steps"] if s["action"] == "dismiss_update"]
    good, short, number, missing = steps
    assert good["reply"] == "OK"
    answer = updates_of(good)
    assert [m["dismissed"] for m in answer] == [True]
    assert answer[0]["state"] == "available"
    for bad in (short, number, missing):
        assert bad["reply"] == "ERROR"
        assert bad["messages"] == [["error", {"message": TAG_SENTENCE}]]
    switches = [s for s in run["steps"] if s["action"] == "set_update_check"]
    off, yes, absent, on = switches
    assert off["reply"] == "OK"
    assert [(m["state"], m["enabled"]) for m in updates_of(off)] == [("disabled", False)]
    for bad in (yes, absent):
        assert bad["reply"] == "ERROR"
        assert bad["messages"] == [["error", {"message": FLAG_SENTENCE}]]
    assert on["reply"] == "OK"
    back = updates_of(on)
    assert [(m["state"], m["enabled"], m["dismissed"]) for m in back] == [("available", True, True)]


def test_the_mock_answers_every_new_sentence_that_has_a_pattern(
    mock_runs: dict[str, Any], dumped: dict[str, Any]
) -> None:
    sentences = {
        data["message"]
        for run in mock_runs.values()
        for step in run["steps"]
        for action, data in step["messages"]
        if action == "error"
    }
    assert sentences == {TAG_SENTENCE, FLAG_SENTENCE}


# The notice in the CSS ---------------------------------------------------------------------------


def rule_blocks(css: str, selector: str) -> list[str]:
    """The bodies of every rule whose selector list contains `selector`."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [part.strip() for part in match.group(1).split(",")]:
            found.append(match.group(2))
    return found


def test_the_banner_is_a_calm_tinted_box_with_a_hairline_accent_border() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    (banner,) = rule_blocks(css, ".update-banner")
    assert "background: var(--accent-soft);" in banner
    assert "border: 1px solid var(--accent-border);" in banner
    assert "border-radius: var(--r-lg);" in banner
    assert "#" not in banner  # tokens only, so both schemes work


def test_the_download_link_looks_like_the_primary_button_with_the_same_colours() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    blocks = rule_blocks(css, "a.update-download")
    assert len(blocks) == 2  # the shared layout rule and the colours
    colours = next(block for block in blocks if "background" in block)
    for declaration in (
        "color: var(--accent-fg);",
        "background: var(--accent);",
        "border: 1px solid var(--accent-border);",
        "border-radius: var(--r-md);",
        "font-weight: 600;",
    ):
        assert declaration in colours, declaration
    for block in blocks:
        assert "#" not in block
    layout = next(block for block in blocks if "min-height" in block)
    assert "min-height: var(--ctl);" in layout  # a 32 px high target
    (hover,) = rule_blocks(css, "a.update-download:hover")
    assert "background: var(--accent-hover);" in hover
    assert "text-decoration: none;" in hover  # the global a:hover underline does not apply
    (active,) = rule_blocks(css, "a.update-download:active")
    assert "background: var(--accent-active);" in active
    assert "min-height: var(--ctl);" in " ".join(rule_blocks(css, "a.update-notes"))


def test_the_notice_uses_a_text_glyph_and_adds_no_motion_or_drawing() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    start = css.index("/* The Updates group")
    end = css.index("/* Search ---")
    notice = css[start:end]
    assert 'content: "\\2197";' in notice  # the arrow of the other links
    for forbidden in ("animation", "transition", "@keyframes", "url(", "svg", "gradient("):
        assert forbidden not in notice, forbidden
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", notice)  # no hard coded colour
    radii = set(re.findall(r"border-radius:\s*([^;]+);", notice))
    assert radii <= {"var(--r-sm)", "var(--r-md)", "var(--r-lg)"}
    for selector in (".setting-updates", ".update-check-row", ".update-result", ".update-actions"):
        assert rule_blocks(css, selector), selector


def test_the_narrow_window_lets_the_actions_wrap() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    (actions,) = rule_blocks(css, ".update-actions")
    assert "flex-wrap: wrap;" in actions
    (row,) = rule_blocks(css, ".update-check-row")
    assert "flex-wrap: wrap;" in row
    (text,) = rule_blocks(css, ".update-text")
    assert "overflow-wrap: anywhere;" in text


# The real page in a real browser ----------------------------------------------------------------

NO_INSTALLER_HINT = CONTRACT_TEXTS["update.hintNoInstaller"][0]
HINT = CONTRACT_TEXTS["update.hint"][0]
BANNER_TEXT = "Version 0.2.0 is available (you have 0.1.0)."
BANNER_TEXT_PL = "Dostępna jest wersja 0.2.0 (masz 0.1.0)."


@dataclass
class UpdateRun:
    report: dict[str, Any]
    found: dict[str, Any]
    shots: Path


@pytest.fixture(scope="module")
def update_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[UpdateRun]:
    python = _python_with_playwright()
    if python is None:
        pytest.skip("Playwright is not installed in any Python on PATH")
    configured = os.environ.get("LCSC_UPDATE_SHOTS")
    shots = Path(configured) if configured else tmp_path_factory.mktemp("update-shots")
    server = _FixtureServer()
    server.start()
    try:
        completed = subprocess.run(
            [python, str(DRIVER), server.base, str(shots)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stderr[-3000:]
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        if "skipped" in report:
            pytest.skip(report["skipped"])
        yield UpdateRun(report, report["found"], shots)
    finally:
        server.stop()


def test_browser_nothing_shows_before_the_first_update_message(update_run: UpdateRun) -> None:
    initial = update_run.found["initial"]
    assert initial["banner"]["hidden"] is True
    assert initial["banner"]["shown"] is False
    assert initial["banner"]["role"] == "status"
    settings = initial["settings"]
    assert settings["group"] == "Updates"
    assert settings["checkbox"] is True  # on by default, like a missing update.json
    assert settings["button"] == "Check now"
    assert settings["buttonDisabled"] is False
    assert settings["result"] == ""  # no line before the user pressed the button
    assert settings["resultRole"] == "status"
    assert settings["checkedHidden"] is True
    assert update_run.found["initial_requests"] == ["ready"]  # the notice sends nothing by itself


def test_browser_an_available_message_from_the_event_stream_shows_the_banner(
    update_run: UpdateRun,
) -> None:
    banner = update_run.found["available"]
    assert banner["hidden"] is False
    assert banner["shown"] is True
    assert banner["role"] == "status"
    assert banner["text"] == BANNER_TEXT
    assert banner["hint"] == HINT
    assert (banner["dismissText"], banner["dismissTitle"]) == (
        "Dismiss",
        "Hide this notice until a newer version appears",
    )
    assert banner["sideways"] == 0


def test_browser_the_links_are_plain_anchors_that_open_a_new_tab(update_run: UpdateRun) -> None:
    links = update_run.found["available"]["links"]
    assert [link["kind"] for link in links] == ["update-download", "update-notes"]
    download, notes = links
    assert download["href"] == installer_url("0.2.0")
    assert notes["href"] == release_url("0.2.0")
    for link in links:
        assert link["target"] == "_blank"
        assert link["rel"] == "noopener noreferrer"
    assert (download["text"], notes["text"]) == ("Download installer", "What is new")
    assert download["label"] == "Download installer for version 0.2.0"
    assert notes["label"] == "What is new in version 0.2.0"
    assert "opens in your browser" in download["title"]
    assert "opens in your browser" in notes["title"]


def test_browser_the_keyboard_reaches_the_links_in_reading_order_and_dismiss_last(
    update_run: UpdateRun,
) -> None:
    assert update_run.found["tab_order"] == [
        "update-download",
        "update-notes",
        "update-dismiss",
        "query",
    ]


def test_browser_a_release_without_an_installer_keeps_the_text_the_release_link_and_dismiss(
    update_run: UpdateRun,
) -> None:
    banner = update_run.found["no_installer"]
    assert banner["hidden"] is False
    assert banner["text"] == BANNER_TEXT
    assert [link["kind"] for link in banner["links"]] == ["update-notes"]
    assert banner["hint"] == NO_INSTALLER_HINT  # it sends the user to the release page
    assert banner["dismissText"] == "Dismiss"
    only_installer = update_run.found["no_release_link"]
    assert [link["kind"] for link in only_installer["links"]] == ["update-download"]
    assert only_installer["hint"] == HINT
    neither = update_run.found["no_links"]
    assert neither["hidden"] is False
    assert neither["links"] == []
    assert neither["text"] == BANNER_TEXT
    assert neither["hint"] == NO_INSTALLER_HINT


def test_browser_hostile_urls_never_become_an_anchor(update_run: UpdateRun) -> None:
    hostile = update_run.found["hostile"]
    assert set(hostile) == {
        "javascript",
        "other_host",
        "http",
        "wrong_asset",
        "other_version_in_url",
        "query_and_fragment",
        "userinfo_and_port",
        "not_strings",
    }
    for name, banner in hostile.items():
        hrefs = [link["href"] for link in banner["links"]]
        if name == "wrong_asset":
            assert hrefs == [release_url("0.2.0")], name  # the release URL was fine
        else:
            assert hrefs == [], name
        assert banner["hidden"] is False, name  # the news itself is still shown
        assert banner["text"] == BANNER_TEXT, name
        assert banner["hint"] == NO_INSTALLER_HINT, name
    assert update_run.found["hostile_dialogs"] == []  # nothing ran


def test_browser_a_version_that_is_not_x_y_z_never_shows_a_banner(update_run: UpdateRun) -> None:
    assert update_run.found["bad_versions"] == dict.fromkeys(
        ["0.2", "v0.2.0", "01.2.3", "0.2.0-evil", "0.2.0.1", "1234567.0.0", "<b>x</b>"], True
    )
    assert update_run.found["available_without_version"] is True
    assert update_run.found["available_with_number"] is True


def test_browser_an_unknown_state_changes_nothing(update_run: UpdateRun) -> None:
    banner = update_run.found["unknown_state"]
    assert banner["hidden"] is False
    assert banner["text"] == BANNER_TEXT  # 0.9.9 of the unknown message was not taken


def test_browser_a_language_switch_rewrites_the_notice_and_the_settings_and_keeps_the_state(
    update_run: UpdateRun,
) -> None:
    english = update_run.found["english"]
    polish = update_run.found["polish"]
    again = update_run.found["english_again"]
    assert polish["banner"]["text"] == BANNER_TEXT_PL
    assert polish["banner"]["hint"] == CONTRACT_TEXTS["update.hint"][1]
    assert polish["banner"]["dismissText"] == "Ukryj"
    assert polish["banner"]["dismissTitle"] == CONTRACT_TEXTS["update.dismissTitle"][1]
    assert [link["text"] for link in polish["banner"]["links"]] == [
        "Pobierz instalator",
        "Co nowego",
    ]
    assert polish["banner"]["links"][0]["label"] == "Pobierz instalator wersji 0.2.0"
    assert polish["banner"]["links"][1]["label"] == "Co nowego w wersji 0.2.0"
    assert [link["href"] for link in polish["banner"]["links"]] == [
        link["href"] for link in english["banner"]["links"]
    ]
    settings = polish["settings"]
    assert settings["group"] == "Aktualizacje"
    assert settings["label"] == "Sprawdzaj aktualizacje raz dziennie"
    assert settings["privacy"] == CONTRACT_TEXTS["update.privacy"][1]
    assert settings["button"] == "Sprawdź teraz"
    assert settings["checked"].startswith("Ostatnie sprawdzenie: ")
    assert "2025" in settings["checked"]
    assert english["settings"]["checked"].startswith("Last check: ")
    assert "Oct 9, 2025" in english["settings"]["checked"]
    # and back: every text returns, nothing was lost on the way
    for probe in (english, again):
        probe["banner"].pop("focus")  # the radio button that was clicked has it, not the notice
    assert again == english
    assert english["settings"]["checkbox"] is polish["settings"]["checkbox"] is True


def test_browser_check_now_sends_a_forced_check_and_shows_checking_while_it_runs(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    for name in ("press_1", "press_2", "press_3", "press_refused"):
        assert found[name] == [{"action": "check_update", "data": {"force": True}}], name
    pressed = found["pressed_1"]
    assert pressed["settings"]["button"] == "Checking..."
    assert pressed["settings"]["buttonDisabled"] is True
    assert pressed["settings"]["result"] == "Checking..."  # for a screen reader
    assert pressed["settings"]["resultQuiet"] is True  # and not twice for the eye
    assert pressed["banner"]["hidden"] is False  # checking never hides the banner
    checking = found["checking_1"]
    assert checking["settings"]["button"] == "Checking..."
    assert checking["settings"]["buttonDisabled"] is True
    assert checking["banner"]["hidden"] is False
    assert checking["banner"]["text"] == BANNER_TEXT


def test_browser_the_button_cannot_be_pressed_again_while_the_answer_is_pending(
    update_run: UpdateRun,
) -> None:
    assert update_run.found["double_press_requests"] == 1


def test_browser_a_check_that_never_ends_does_not_disable_the_button_for_good(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    before = found["guard_before"]  # 30 s after the press: still waiting
    assert (before["button"], before["buttonDisabled"]) == ("Checking...", True)
    after = found["guard_after"]  # 50 s: the page gave up, and says so to the one who asked
    assert (after["settings"]["button"], after["settings"]["buttonDisabled"]) == (
        "Check now",
        False,
    )
    assert after["settings"]["result"] == "Could not check for updates. Try again later."
    assert after["banner"]["hidden"] is False
    # a check that nobody asked for is released silently
    assert found["guard_auto_before"]["buttonDisabled"] is True
    auto = found["guard_auto_after"]["settings"]
    assert (auto["buttonDisabled"], auto["button"]) == (False, "Check now")


def test_browser_an_automatic_check_only_overlays_the_button_and_stays_silent_on_error(
    update_run: UpdateRun,
) -> None:
    checking = update_run.found["automatic_checking"]
    assert checking["settings"]["button"] == "Checking..."
    assert checking["settings"]["buttonDisabled"] is True
    assert checking["settings"]["result"] == ""  # nobody asked: no line
    assert checking["banner"]["hidden"] is False
    failed = update_run.found["automatic_error"]
    assert failed["settings"]["result"] == ""
    assert failed["settings"]["resultError"] is False
    assert failed["settings"]["button"] == "Check now"
    assert failed["settings"]["buttonDisabled"] is False
    assert failed["banner"]["hidden"] is False  # an error never hides the banner
    assert failed["banner"]["text"] == BANNER_TEXT


def test_browser_an_error_after_a_manual_check_shows_the_line_and_keeps_the_banner(
    update_run: UpdateRun,
) -> None:
    error = update_run.found["error_1"]
    assert error["settings"]["result"] == "Could not check for updates. Try again later."
    assert error["settings"]["resultError"] is True
    assert error["settings"]["resultQuiet"] is False
    assert error["settings"]["button"] == "Check now"
    assert error["settings"]["buttonDisabled"] is False
    assert error["banner"]["hidden"] is False
    assert error["banner"]["text"] == BANNER_TEXT
    assert [link["kind"] for link in error["banner"]["links"]] == [
        "update-download",
        "update-notes",
    ]
    polish = update_run.found["error_1_polish"]  # the line follows the language, from state
    assert polish["settings"]["result"] == CONTRACT_TEXTS["update.result.error"][1]
    assert polish["banner"]["hidden"] is False


def test_browser_the_answer_of_a_manual_check_is_reported_in_both_languages(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    available = found["available_result"]
    assert available["settings"]["result"] == "Version 0.2.0 is available."
    assert available["settings"]["resultError"] is False
    assert available["banner"]["hidden"] is False
    assert found["available_result_polish"]["result"] == "Dostępna jest wersja 0.2.0."
    current = found["current_result"]
    assert current["settings"]["result"] == "You have the latest version (0.1.0)."
    assert current["banner"]["hidden"] is True  # nothing newer any more
    assert found["current_result_polish"]["result"] == "Masz najnowszą wersję (0.1.0)."


def test_browser_the_same_version_again_does_not_rebuild_the_banner_or_steal_the_focus(
    update_run: UpdateRun,
) -> None:
    assert update_run.found["focus_kept"] == "update-download"


def test_browser_a_refused_check_is_reported_and_the_button_is_usable_again(
    update_run: UpdateRun,
) -> None:
    refused = update_run.found["refused_check"]
    assert refused["buttonDisabled"] is False
    assert refused["button"] == "Check now"
    assert refused["result"] == "Could not check for updates. Try again later."
    assert refused["resultError"] is True


def test_browser_dismiss_sends_the_tag_hides_at_once_and_a_newer_version_shows_again(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    assert found["dismiss_request"] == [{"action": "dismiss_update", "data": {"tag": "v0.2.0"}}]
    assert found["dismissed_now"]["hidden"] is True  # before any answer
    assert found["dismissed_now"]["focus"] == "query"  # the focus did not fall to the page
    assert found["dismissed_confirmed"]["hidden"] is True  # the answer confirms
    again = found["newer_version_shows_again"]
    assert again["hidden"] is False
    assert again["text"] == "Version 0.3.0 is available (you have 0.1.0)."
    assert again["links"][0]["href"] == installer_url("0.3.0")
    assert found["newer_dismissed"] is True  # a message with dismissed hides it too


def test_browser_a_refused_dismiss_brings_the_banner_back(update_run: UpdateRun) -> None:
    found = update_run.found
    assert found["dismiss_refused_request"] == [
        {"action": "dismiss_update", "data": {"tag": "v0.4.0"}}
    ]
    assert found["dismiss_refused"]["hidden"] is False
    assert found["dismiss_refused"]["text"] == "Version 0.4.0 is available (you have 0.1.0)."


def test_browser_the_checkbox_sends_the_switch_and_follows_the_messages(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    assert found["switch_off_request"] == [
        {"action": "set_update_check", "data": {"enabled": False}}
    ]
    assert found["switched_off"]["checkbox"] is False
    assert found["switched_off_banner_hidden"] is True  # "disabled" is a state without a banner
    assert found["switch_on_request"] == [{"action": "set_update_check", "data": {"enabled": True}}]
    assert found["switched_on"]["checkbox"] is True
    assert found["box_follows_message"] is False  # whoever clicked, the message decides
    assert found["switch_refused"] is True  # Python refused: the box went back


def test_browser_changing_the_setting_clears_the_line_of_the_last_check(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    # the refused check left its error line; the first switch closed it, and it stayed closed
    assert found["refused_check"]["result"] == "Could not check for updates. Try again later."
    for name in ("switched_off", "switched_on", "last_check"):
        assert found[name]["result"] == "", name


def test_browser_the_time_of_the_last_check_is_shown_only_when_it_is_a_number(
    update_run: UpdateRun,
) -> None:
    found = update_run.found
    assert found["last_check"]["checkedHidden"] is False
    assert found["last_check"]["checked"].startswith("Last check: ")
    for name in ("no_last_check", "bad_last_check"):
        assert found[name]["checkedHidden"] is True, name
        assert found[name]["checked"] == "", name


def test_browser_narrow_windows_show_the_polish_notice_and_settings_inside_the_window(
    update_run: UpdateRun,
) -> None:
    fit = update_run.found["fit"]
    assert set(fit) == {"420", "360"}
    for width, result in fit.items():
        assert result["width"] == int(width)
        assert result["outside"] == [], width
        assert result["sideways"] == 0, width


def test_browser_the_page_raised_no_error_and_the_csp_blocked_nothing(
    update_run: UpdateRun,
) -> None:
    assert update_run.report["page_errors"] == []
    assert not [e for e in update_run.report["console_errors"] if "Content Security" in e]
    assert update_run.found["hostile_dialogs"] == []


def test_browser_screenshots_were_taken(update_run: UpdateRun) -> None:
    names = {Path(path).name for path in update_run.report["shots"]}
    assert names == {
        "update_banner_light.png",
        "update_banner_dark.png",
        "update_settings_light.png",
        "update_error_light.png",
        "update_current_light.png",
        "update_pl_420.png",
        "update_pl_dark_420.png",
        "update_pl_360.png",
        "update_pl_dark_360.png",
    }
    for path in update_run.report["shots"]:
        assert Path(path).stat().st_size > 2000
