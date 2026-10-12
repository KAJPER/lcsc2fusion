"""Tests of the settings area of the add-in window: theme (System, Light, Dark) and language.

Layers, all offline and without Fusion:

* the real theme-init.js and i18n.js run under Node in a vm with fake DOM objects
  (tests/js/i18n_harness.js): saved choices, validation, blocked storage, language selection,
  plural forms, the data-i18n hooks and the mechanism of the message patterns;
* static checks of the dictionaries (same keys, same placeholders, no dashes, real Polish letters),
  of index.html (every data-i18n key exists and the English default text equals the dictionary),
  of the keys that app.js and http-bridge.js use, and of the settings panel;
* style.css: the two copies of the dark tokens are identical;
* the real HttpTransport serving the files, and the build script shipping them.

The patterns for the texts that Python sends are tested in tests/test_addin_i18n_messages.py.
The same page in a real browser (Playwright) is exercised by tests/test_addin_http_bridge.py.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any

import http_transport as ht
import pytest

ROOT = Path(__file__).resolve().parents[1]
PALETTE_DIR = ROOT / "addin" / "LCSC2Fusion" / "palette"
HARNESS = ROOT / "tests" / "js" / "i18n_harness.js"
NODE = shutil.which("node")

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
POLISH_LETTERS = "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"
JS_TYPE = "text/javascript; charset=utf-8"
# Texts that are the same in every language on purpose (names, abbreviations, numbers).
SAME_IN_EVERY_LANGUAGE = {
    "col.lcsc",
    "col.mpn",
    "card.badge3d",
    "status.level.info",
    "status.level.ok",
}

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# Node harness -------------------------------------------------------------------------------------


def _run_harness(mode: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(HARNESS), str(PALETTE_DIR), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def _scenario_names() -> list[str]:
    if NODE is None:
        return []
    try:
        names = json.loads(_run_harness("--list").stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return [str(name) for name in names]


SCENARIOS = _scenario_names()


@pytest.fixture(scope="module")
def harness_results() -> dict[str, str]:
    completed = _run_harness("--all")
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert isinstance(results, dict)
    return {str(name): str(outcome) for name, outcome in results.items()}


@pytest.fixture(scope="module")
def dump() -> dict[str, Any]:
    if NODE is None:
        pytest.skip("node is not installed")
    completed = _run_harness("--dump")
    assert completed.returncode == 0, completed.stderr
    data = json.loads(completed.stdout)
    assert isinstance(data, dict)
    return data


@needs_node
def test_harness_lists_the_expected_scenarios() -> None:
    expected = {
        "theme_default_is_system_without_an_attribute",
        "theme_saved_dark_is_applied_before_anything_else",
        "invalid_saved_themes_count_as_nothing_saved",
        "language_follows_the_browser_when_nothing_is_saved",
        "a_saved_language_wins_over_the_browser",
        "blocked_storage_still_gives_a_working_page",
        "t_picks_the_plural_form_of_the_language",
        "apply_writes_text_attributes_params_and_messages",
        "translate_message_never_touches_english_and_unknown_text",
    }
    assert expected <= set(SCENARIOS)


@needs_node
@pytest.mark.parametrize("name", SCENARIOS)
def test_script_scenario(name: str, harness_results: dict[str, str]) -> None:
    assert harness_results[name] == "ok", harness_results[name]


# Dictionaries -------------------------------------------------------------------------------------


def _forms(value: str | dict[str, str]) -> list[str]:
    return list(value.values()) if isinstance(value, dict) else [value]


def _placeholders(value: str | dict[str, str]) -> set[str]:
    return {name for text in _forms(value) for name in re.findall(r"\{(\w+)\}", text)}


def _plain(value: str | dict[str, str]) -> str:
    """The English text to compare with a page: a plural entry shows its `other` form."""
    return value["other"] if isinstance(value, dict) else value


def test_the_languages_are_english_and_polish_and_match_the_prefs_script(
    dump: dict[str, Any],
) -> None:
    assert sorted(dump["languages"]) == ["en", "pl"]
    assert sorted(dump["prefs_languages"]) == sorted(dump["languages"])
    assert dump["prefs_themes"] == ["system", "light", "dark"]


def test_every_language_has_the_same_keys(dump: dict[str, Any]) -> None:
    dictionaries = dump["dictionaries"]
    english = set(dictionaries["en"])
    assert len(english) > 100
    for language, entries in dictionaries.items():
        assert set(entries) == english, (language, sorted(set(entries) ^ english))


def test_entries_have_the_same_shape_and_placeholders_in_every_language(
    dump: dict[str, Any],
) -> None:
    dictionaries = dump["dictionaries"]
    for key, english in dictionaries["en"].items():
        for language, entries in dictionaries.items():
            value = entries[key]
            assert isinstance(value, dict) == isinstance(english, dict), (language, key)
            assert _placeholders(value) == _placeholders(english), (language, key)
            if isinstance(value, dict):
                assert "other" in value, (language, key)
                assert set(value) <= {"zero", "one", "two", "few", "many", "other"}, (language, key)
                assert "n" in _placeholders(value), (
                    language,
                    key,
                )  # a plural entry counts something


def test_no_text_is_empty_or_markup(dump: dict[str, Any]) -> None:
    for language, entries in dump["dictionaries"].items():
        for key, value in entries.items():
            for text in _forms(value):
                assert text.strip() == text and text, (language, key)
                assert "<" not in text, (language, key)  # texts are set as text, never as HTML


def test_no_dashes_in_any_text(dump: dict[str, Any]) -> None:
    for language, entries in dump["dictionaries"].items():
        for key, value in entries.items():
            for text in _forms(value):
                assert EM_DASH not in text and EN_DASH not in text, (language, key)
                assert "--" not in text, (language, key)
    for entry in dump["messages"]:
        for language, template in entry["templates"].items():
            for text in _forms(template):
                assert EM_DASH not in text and EN_DASH not in text, (language, entry["source"])
                assert "--" not in text, (language, entry["source"])


def test_polish_texts_have_real_diacritics(dump: dict[str, Any]) -> None:
    polish = dump["dictionaries"]["pl"]
    spot = {
        "settings.language": "Język",
        "settings.open": "Ustawienia",
        "settings.theme.light": "Jasny",
        "settings.theme.dark": "Ciemny",
        "status.level.error": "Błąd",
        "results.empty.title": "Nie znaleziono części",
        "stock.in": "w magazynie",
        "stock.out": "Brak w magazynie",
        "button.addToFusion": "Dodaj do biblioteki Fusion",
        "button.openSeparate": "Otwórz jako osobną bibliotekę",
        "button.add": "Dodaj do biblioteki",
        "card.saved": "Zapisano",
        "col.datasheet": "Karta katalogowa",
    }
    for key, expected in spot.items():
        assert polish[key] == expected, key
    letters = {char for value in polish.values() for text in _forms(value) for char in text}
    for char in "ąćęłńóśźż":
        assert char in letters, f"no Polish text uses {char!r}"
    for key, value in polish.items():
        for text in _forms(value):
            assert "�" not in text, key  # a broken encoding
            assert not re.search(r"[ÃÅÄ][\x80-\xbf]", text), key  # UTF-8 read as Latin 1


def test_polish_is_not_a_copy_of_english(dump: dict[str, Any]) -> None:
    dictionaries = dump["dictionaries"]
    for key, english in dictionaries["en"].items():
        if key in SAME_IN_EVERY_LANGUAGE:
            continue
        assert dictionaries["pl"][key] != english, f"{key} is not translated"


def test_the_buttons_that_python_texts_quote_exist_in_both_languages(dump: dict[str, Any]) -> None:
    import controller

    english = dump["dictionaries"]["en"]
    assert english["button.addToFusion"] == controller.BUTTON_ADD_TO_FUSION
    assert english["button.openSeparate"] == controller.BUTTON_OPEN_SEPARATE
    polish = dump["dictionaries"]["pl"]
    # The 3D steps point at the button by name: the Polish text quotes the Polish caption.
    assert f'"{polish["button.openSeparate"]}"' in polish["model.step1"]
    assert f'"{english["button.openSeparate"]}"' in english["model.step1"]


def test_every_message_entry_has_a_template_for_every_other_language(dump: dict[str, Any]) -> None:
    others = [language for language in dump["languages"] if language != "en"]
    assert dump["messages"], "the table of message patterns is empty"
    for entry in dump["messages"]:
        assert entry["flags"] in ("", "i", "s", "is", "si"), entry["source"]
        for language in others:
            assert entry["templates"].get(language), (language, entry["source"])


# The page and the scripts use keys that exist -----------------------------------------------------


class _Collector(HTMLParser):
    """Elements of index.html with their attributes, and the text inside data-i18n elements."""

    VOID = frozenset({"meta", "link", "img", "input", "br", "hr"})

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.texts: list[tuple[str, str, str]] = []  # (tag, data-i18n key, text inside)
        self._stack: list[tuple[str, list[str] | None, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag in self.VOID:
            return
        key = attributes.get("data-i18n")
        self._stack.append((tag, [] if key else None, key or ""))

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            name, captured, key = self._stack.pop()
            if captured is not None:
                self.texts.append((name, key, " ".join("".join(captured).split())))
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        for _, captured, _ in self._stack:
            if captured is not None:
                captured.append(data)

    def find(self, element_id: str) -> tuple[str, dict[str, str | None]] | None:
        return next((t for t in self.tags if t[1].get("id") == element_id), None)


def _page() -> _Collector:
    collector = _Collector()
    collector.feed((PALETTE_DIR / "index.html").read_text(encoding="utf-8"))
    return collector


def _attr_pairs(value: str) -> list[tuple[str, str]]:
    pairs = []
    for item in value.split(";"):
        name, _, key = item.partition(":")
        pairs.append((name.strip(), key.strip()))
    return pairs


def test_every_data_i18n_key_of_the_page_exists_and_its_default_text_is_the_english_one(
    dump: dict[str, Any],
) -> None:
    page = _page()
    dictionaries = dump["dictionaries"]
    assert len(page.texts) > 40
    for tag, key, text in page.texts:
        for language in dictionaries:
            assert key in dictionaries[language], (tag, key, language)
        assert text == " ".join(_plain(dictionaries["en"][key]).split()), (tag, key)
    for tag, attrs in page.tags:
        value = attrs.get("data-i18n-attr")
        if not value:
            continue
        for attribute, key in _attr_pairs(value):
            assert key in dictionaries["en"] and key in dictionaries["pl"], (tag, key)
            assert attrs.get(attribute) == _plain(dictionaries["en"][key]), (tag, attribute, key)


def test_elements_with_a_key_hold_only_text(dump: dict[str, Any]) -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(
        r"<(\w+)\b[^>]*\bdata-i18n=\"[^\"]+\"[^>]*>(.*?)</\1>", html, re.DOTALL
    ):
        assert "<" not in match.group(2), match.group(0)[:80]  # the text is replaced as a whole


def test_the_state_driven_nodes_of_the_page_have_no_static_key() -> None:
    # app.js and http-bridge.js write these themselves, from state; a data-i18n here would let a
    # language switch put the first text back.
    page = _page()
    for element_id in (
        "status-badge",
        "status-text",
        "error-text",
        "results-heading",
        "results-count",
        "conn-banner",
    ):
        found = page.find(element_id)
        assert found is not None, element_id
        assert "data-i18n" not in found[1], element_id


KEY_LITERAL = re.compile(r"'([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+)'")


def _code(name: str) -> str:
    """A palette script without comments, so that prose does not look like code."""
    text = (PALETTE_DIR / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|(?<=\s)//.*$", "", text)


def _used_keys(name: str, known: set[str]) -> set[str]:
    prefixes = {key.split(".")[0] for key in known}
    return {
        literal for literal in KEY_LITERAL.findall(_code(name)) if literal.split(".")[0] in prefixes
    }


def test_every_key_that_app_js_and_the_bridge_use_exists_in_both_languages(
    dump: dict[str, Any],
) -> None:
    dictionaries = dump["dictionaries"]
    known = set(dictionaries["en"])
    for name in ("app.js", "http-bridge.js"):
        used = _used_keys(name, known)
        assert used, name
        for key in used:
            assert key in dictionaries["en"], (name, key)
            assert key in dictionaries["pl"], (name, key)


def test_no_key_is_dead(dump: dict[str, Any]) -> None:
    known = set(dump["dictionaries"]["en"])
    used: set[str] = set()
    for _tag, attrs in _page().tags:
        if attrs.get("data-i18n"):
            used.add(str(attrs["data-i18n"]))
        if attrs.get("data-i18n-attr"):
            used.update(key for _, key in _attr_pairs(str(attrs["data-i18n-attr"])))
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    used.update(re.findall(r'data-i18n="([^"]+)"', html))  # also inside the <template>
    for name in ("app.js", "http-bridge.js"):
        used |= _used_keys(name, known)
    for entry in dump["messages"]:
        for template in entry["templates"].values():
            if isinstance(template, str) and template.startswith("@"):
                used.add(template[1:])
    assert sorted(known - used) == []


def test_app_js_never_builds_a_key_from_pieces() -> None:
    # A key made at run time ('status.level.' + level) cannot be found by the tests above.
    code = _code("app.js") + _code("http-bridge.js")
    assert not re.search(r"\bt\(\s*'[^']*'\s*\+", code)
    for call in re.findall(r"\bt\(([^)]*)\)", code):
        first = call.split(",")[0].strip()
        assert first.startswith("'") or first in {
            "key",
            "keys[name]",
            "text.key",
            "LEVEL_KEYS[status.level]",
            "KEY_REQUEST_FAILED",
        }, call


# The settings panel -------------------------------------------------------------------------------


def test_the_settings_button_and_panel_are_accessible_and_consistent(dump: dict[str, Any]) -> None:
    page = _page()
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    toggle = page.find("settings-toggle")
    panel = page.find("settings-panel")
    heading = page.find("settings-heading")
    assert toggle and panel and heading
    assert toggle[0] == "button"
    assert toggle[1]["type"] == "button"
    assert toggle[1]["aria-expanded"] == "false"
    assert toggle[1]["aria-controls"] == "settings-panel"
    assert toggle[1]["data-i18n"] == "settings.open"
    assert panel[1]["aria-labelledby"] == "settings-heading"
    assert "hidden" in panel[1]  # closed until the button opens it
    assert "sr-only" in str(heading[1]["class"])
    # a text button: nothing but text inside (no drawn icon)
    button = re.search(r'<button id="settings-toggle".*?>(.*?)</button>', html, re.DOTALL)
    assert button is not None
    assert "<" not in button.group(1)
    # the button is in the header, the panel right below it and above the search form
    header = html[html.index('<header class="app-header">') : html.index("</header>")]
    assert 'id="settings-toggle"' in header
    assert html.index('id="settings-panel"') < html.index('id="search-form"')
    assert html.index('id="settings-panel"') > html.index("</header>")


def test_the_theme_radios_offer_exactly_the_themes_of_the_prefs_script(
    dump: dict[str, Any],
) -> None:
    page = _page()
    radios = [a for t, a in page.tags if t == "input" and a.get("name") == "theme"]
    assert [a["value"] for a in radios] == dump["prefs_themes"]
    for attrs in radios:
        assert attrs["type"] == "radio"
        assert attrs["id"] == f"theme-{attrs['value']}"
        label = next(a for t, a in page.tags if t == "label" and a.get("for") == attrs["id"])
        assert label.get("class") == "check"
    keys = {key for _, key, _ in page.texts}
    assert {f"settings.theme.{value}" for value in dump["prefs_themes"]} <= keys
    container = page.find("language-options")
    assert container is not None  # filled by app.js, one radio per language of the prefs script


def test_app_js_wires_the_settings_without_touching_the_storage_itself() -> None:
    app = _code("app.js")
    for needle in (
        "$('settings-toggle')",
        "$('settings-panel')",
        "$('language-options')",
        "'aria-expanded'",
        "'Escape'",
        "Prefs.setTheme(",
        "Prefs.setLanguage(",
        "I18n.setLanguage(",
        "Prefs.LANGUAGES",
        "function retranslate()",
        ".focus()",
    ):
        assert needle in app, needle
    for name in ("app.js", "i18n.js", "http-bridge.js", "mock-bridge.js"):
        code = _code(name)
        for word in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            assert word not in code, (name, word)


def test_the_retranslation_redraws_what_a_switch_must_not_lose() -> None:
    app = _code("app.js")
    body = app[app.index("function retranslate()") :]
    body = body[: body.index("\n  }\n")]
    for call in (
        "I18n.apply(document)",
        "renderStatus()",
        "renderError()",
        "drawResultRows()",
        "showResultsView()",
        "refreshAllFusion(false)",  # false: a switch does not reopen help the user closed
        "updateAddButtons()",
    ):
        assert call in body, call
    assert "replaceChildren" not in body  # the saved cards are never rebuilt: open details stay
    assert "added-list" not in body


# theme-init.js and i18n.js are CSP clean and small ------------------------------------------------


@pytest.mark.parametrize("name", ["theme-init.js", "i18n.js"])
def test_the_new_scripts_are_plain_csp_clean_browser_scripts(name: str) -> None:
    raw = (PALETTE_DIR / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    text = raw.decode("utf-8")
    code = _code(name)
    assert "'use strict'" in code
    for forbidden in (
        "eval(",
        "new Function",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "require(",
    ):
        assert forbidden not in code, forbidden
    assert not re.search(r"(?m)^\s*(?:import|export)\s+[\w{*]", code)  # a classic script
    assert not re.search(r"setTimeout\(\s*['\"`]", code)
    for pattern in (r"\?\.", r"\?\?", r"\.replaceAll\(", r"\.at\("):
        assert not re.search(pattern, code), pattern  # the same ES2019 floor as http-bridge.js
    for url in re.findall(r"https?://[^\s'\"`)]*", text):
        assert name == "i18n.js" and url.startswith("https://easyeda.com"), (name, url)


def test_theme_init_is_tiny_and_every_storage_access_is_guarded() -> None:
    raw = (PALETTE_DIR / "theme-init.js").read_text(encoding="utf-8")
    code = _code("theme-init.js")
    assert len(raw.encode("utf-8")) < 6000  # it blocks the first paint
    accesses = [m.start() for m in re.finditer(r"localStorage", code)]
    assert len(accesses) == 2  # one read, one write
    for position in accesses:
        before = code[:position]
        assert before.rfind("try {") > before.rfind("catch"), "storage access outside try/catch"
    assert "'lcsc2fusion.theme'" in code
    assert "'lcsc2fusion.lang'" in code


# style.css: one dark palette, applied in two ways -------------------------------------------------


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _block(css: str, header: str) -> str:
    """The text between the braces that follow `header` (nested braces are counted)."""
    start = css.index(header)
    open_at = css.index("{", start)
    depth = 0
    for position in range(open_at, len(css)):
        if css[position] == "{":
            depth += 1
        elif css[position] == "}":
            depth -= 1
            if depth == 0:
                return css[open_at + 1 : position]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _rules(css: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _colour_tokens(body: str) -> dict[str, str]:
    return {
        name: value.lower()
        for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", body)
    }


def test_the_two_copies_of_the_dark_tokens_are_identical() -> None:
    css = _strip_comments((PALETTE_DIR / "style.css").read_text(encoding="utf-8"))
    media = _block(css, "@media (prefers-color-scheme: dark)")
    system_dark = [
        body for selector, body in _rules(media) if selector == ':root:not([data-theme="light"])'
    ]
    assert len(system_dark) == 1
    forced_dark = [body for selector, body in _rules(css) if selector == ':root[data-theme="dark"]']
    token_blocks = [_colour_tokens(body) for body in forced_dark if "--bg" in body]
    assert len(token_blocks) == 1
    assert _colour_tokens(system_dark[0]) == token_blocks[0]
    light = _colour_tokens(_block(css, ":root"))
    assert set(token_blocks[0]) == set(light)  # dark overrides every colour of the light set
    assert len(light) >= 20
    assert all(light[name] != token_blocks[0][name] for name in ("bg", "surface", "fg", "border"))


def test_only_the_allowed_selectors_depend_on_data_theme() -> None:
    css = _strip_comments((PALETTE_DIR / "style.css").read_text(encoding="utf-8"))
    selectors = {selector for selector, _ in _rules(css) if "data-theme" in selector}
    assert selectors == {
        ':root[data-theme="light"]',
        ':root[data-theme="dark"]',
        ':root:not([data-theme="light"])',
    }
    # a forced light scheme has no token overrides: the :root set IS the light palette
    for selector, body in _rules(css):
        if selector == ':root[data-theme="light"]':
            assert "--" not in body


def test_color_scheme_follows_the_choice() -> None:
    css = _strip_comments((PALETTE_DIR / "style.css").read_text(encoding="utf-8"))
    rules = dict(_rules(css))
    assert "color-scheme: light dark;" in rules[":root"]  # System: both, the OS decides
    assert re.search(r"color-scheme:\s*light;", rules[':root[data-theme="light"]'])
    dark_bodies = [body for selector, body in _rules(css) if selector == ':root[data-theme="dark"]']
    assert any(re.search(r"color-scheme:\s*dark;", body) for body in dark_bodies)
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    assert '<meta name="color-scheme" content="light dark">' in html


def test_the_theme_color_metas_and_the_first_script_are_in_the_head_in_order() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    head = html[: html.index("</head>")]
    last_meta = head.rindex('<meta name="theme-color"')
    assert last_meta < head.index('src="theme-init.js"') < head.index('rel="stylesheet"')
    assert head.count('<meta name="theme-color"') == 2


# Served by the real transport and shipped by the build --------------------------------------------


def test_the_new_files_are_on_the_allow_list_with_the_utf8_script_type() -> None:
    assert ht.STATIC_FILES["theme-init.js"] == JS_TYPE
    assert ht.STATIC_FILES["i18n.js"] == JS_TYPE
    for name in ht.STATIC_FILES:
        assert (PALETTE_DIR / name).is_file(), name


def _get(
    server: ht.HttpTransport, path: str, cookie: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    port = int(server.url.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Cookie": cookie} if cookie else {})
        response = connection.getresponse()
        body = response.read()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, body
    finally:
        connection.close()


@pytest.fixture
def served() -> Iterator[tuple[ht.HttpTransport, str]]:
    server = ht.HttpTransport(ht.Hub(), lambda action, data: "OK", lambda line: None, PALETTE_DIR)
    server.start()
    try:
        status, headers, _ = _get(server, server.launch_url().removeprefix(server.url))
        assert status == 302
        yield server, headers["set-cookie"].split(";")[0]
    finally:
        server.stop()


@pytest.mark.parametrize("name", ["theme-init.js", "i18n.js"])
def test_the_transport_serves_the_script_as_utf8_behind_the_session_cookie(
    served: tuple[ht.HttpTransport, str], name: str
) -> None:
    server, cookie = served
    status, headers, body = _get(server, "/" + name, cookie)
    assert status == 200
    assert headers["content-type"] == JS_TYPE
    assert headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert body == (PALETTE_DIR / name).read_bytes()
    body.decode("utf-8")  # valid UTF-8 end to end
    refused, _, refused_body = _get(server, "/" + name)
    assert refused == 403
    assert len(refused_body) < 64


def test_the_served_dictionary_carries_the_polish_letters_as_utf8_bytes(
    served: tuple[ht.HttpTransport, str],
) -> None:
    server, cookie = served
    _, headers, body = _get(server, "/i18n.js", cookie)
    assert "charset=utf-8" in headers["content-type"]
    assert "Język".encode() in body
    assert b"J\xc4\x99zyk" in body
    for letter in "ąćęłńóśźż":
        assert letter.encode("utf-8") in body, letter


def test_the_page_served_by_the_transport_loads_the_new_scripts(
    served: tuple[ht.HttpTransport, str],
) -> None:
    server, cookie = served
    _, headers, body = _get(server, "/", cookie)
    assert headers["content-type"] == "text/html; charset=utf-8"
    page = body.decode("utf-8")
    assert '<script src="theme-init.js"></script>' in page
    assert '<script src="i18n.js"></script>' in page


def test_the_build_ships_the_new_scripts_byte_for_byte(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "build_addin_for_i18n_tests", ROOT / "scripts" / "build_addin.py"
    )
    assert spec is not None
    assert spec.loader is not None
    build_addin: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = build_addin  # dataclasses look the defining module up here
    try:
        spec.loader.exec_module(build_addin)
        result = build_addin.build(root=ROOT, dist=tmp_path)
    finally:
        del sys.modules[spec.name]
    for name in ("theme-init.js", "i18n.js"):
        assert f"palette/{name}" in result.files
        shipped = result.folder / "palette" / name
        assert shipped.read_bytes() == (PALETTE_DIR / name).read_bytes()
    every_palette_file = sorted(path.name for path in PALETTE_DIR.iterdir())
    shipped_names = sorted(path.name for path in (result.folder / "palette").iterdir())
    assert shipped_names == every_palette_file  # the whole folder is copied, no list to forget


# The mock bridge speaks like the controller, so the Polish page can be tried without Fusion -----

MOCK_MESSAGES_SCRIPT = """
global.window = {};
const realSetTimeout = setTimeout;
global.setTimeout = (fn, ms) => realSetTimeout(fn, Math.floor(ms / 50));
require(process.argv[1]);
require(process.argv[2]);
const i18n = window.LcscI18n;
const texts = [];
const bridge = window.LcscMock.install((action, data) => {
  if (action === 'status') texts.push(data.text);
  if (action === 'error') texts.push(data.message);
  if (action === 'added') texts.push(...data.warnings, data.instructions);
  if (action === 'fusion_done') texts.push(data.message, ...data.warnings);
});
const send = (action, payload) => bridge.fusionSendData(action, JSON.stringify(payload));
const at = (ms, fn) => realSetTimeout(fn, ms);
send('ready', {});
send('search', { query: 'C2040' });
at(100, () => send('search', { query: 'none' }));
at(200, () => send('search', { query: 'all' }));
at(300, () => send('add', { lcsc_id: 'C25804', with_3d: false, fusion: true }));
at(500, () => send('add', { lcsc_id: 'C2040', with_3d: true, fusion: true }));
at(700, () => send('add', { lcsc_id: 'C14663', with_3d: true, fusion: true }));
at(900, () => send('add', { lcsc_id: 'C2913202', with_3d: true, fusion: true }));
at(1100, () => send('add_to_fusion', { lcsc_id: 'C2913202' }));
at(1300, () => send('add', { lcsc_id: 'C25804', with_3d: false, fusion: true }));
at(1500, () => send('add_to_fusion', { lcsc_id: 'C1' }));
at(1600, () => send('open_library', { path: 'C:/x/C2913202.lbr' }));
at(1700, () => send('open_library', { path: 'C:/x/C25804.lbr' }));
at(2200, () => {
  const unique = Array.from(new Set(texts));
  const report = unique.map((text) => ({
    text,
    matched: i18n.findMessage(text) !== null,
    polish: i18n.translateMessage(text, 'pl'),
  }));
  console.log(JSON.stringify(report));
});
"""


@needs_node
def test_every_status_of_the_mock_has_a_polish_translation_except_quoted_data() -> None:
    assert NODE is not None
    completed = subprocess.run(
        [
            NODE,
            "-e",
            MOCK_MESSAGES_SCRIPT,
            str(PALETTE_DIR / "i18n.js"),
            str(PALETTE_DIR / "mock-bridge.js"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert len(report) >= 20
    # texts that quote EasyEDA data, and the developer notes of the mock, are shown as they are
    quoted = re.compile(r"^C\d+: (pad \d+ is shared|polygon pad)")
    untranslated = [
        entry["text"]
        for entry in report
        if not entry["matched"]
        and not quoted.match(entry["text"])
        and not entry["text"].startswith("Mock")
    ]
    assert untranslated == []
    for entry in report:
        if entry["matched"]:
            assert entry["polish"] != entry["text"], entry["text"]
