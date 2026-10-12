"""Static checks of the add-in files and of the build script (no Fusion needed)."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType

import controller
import pytest

from lcsc2fusion import __version__

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"
PALETTE_DIR = ADDIN_DIR / "palette"
ATTRIBUTION = (
    "Data: JLCEDA/EasyEDA Official Library, https://easyeda.com . Personal use; "
    "you are responsible for complying with the EasyEDA and LCSC terms."
)


@pytest.fixture(scope="module")
def build_addin() -> Iterator[ModuleType]:
    """scripts/ is not a package: load the build script by path."""
    spec = importlib.util.spec_from_file_location(
        "build_addin", ROOT / "scripts" / "build_addin.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the defining module up here
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


class _ElementCollector(HTMLParser):
    """Records start tags (with attributes) and the text directly inside each <label>."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.label_texts: list[str] = []
        self.label_children: list[list[str]] = []
        self._in_label = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "label":
            self._in_label = True
            self.label_texts.append("")
            self.label_children.append([])
        elif self._in_label:
            self.label_children[-1].append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._in_label = False

    def handle_data(self, data: str) -> None:
        if self._in_label:
            self.label_texts[-1] += data

    def find(self, element_id: str) -> tuple[str, dict[str, str | None]] | None:
        return next((t for t in self.tags if t[1].get("id") == element_id), None)


def parse_html(name: str = "index.html") -> _ElementCollector:
    collector = _ElementCollector()
    collector.feed((PALETTE_DIR / name).read_text(encoding="utf-8"))
    return collector


def js_action_pairs(source: str, name: str) -> dict[str, str]:
    """KEY -> 'action' entries of `const <name> = Object.freeze({...})` in app.js."""
    match = re.search(rf"const {name} = Object\.freeze\(\{{(.*?)\}}\)", source, re.DOTALL)
    assert match is not None, f"{name} block not found in app.js"
    return dict(re.findall(r"(\w+):\s*'([a-z_]+)'", match.group(1)))


# Manifest and entry point --------------------------------------------------------------------


def test_names_are_consistent_and_manifest_is_valid() -> None:
    name = ADDIN_DIR.name
    assert name == "LCSC2Fusion"
    assert (ADDIN_DIR / f"{name}.py").is_file()
    manifest = json.loads((ADDIN_DIR / f"{name}.manifest").read_text(encoding="utf-8"))
    assert manifest["autodeskProduct"] == "Fusion"
    assert manifest["type"] == "addin"
    assert manifest["runOnStartup"] is True  # the buttons must exist after every Fusion start
    assert manifest["editEnabled"] is True
    assert manifest["supportedOS"] == "windows|mac"
    assert isinstance(manifest["description"], dict)
    assert manifest["description"][""]
    assert manifest["author"]
    assert manifest["version"] == __version__
    assert (ADDIN_DIR / manifest["iconFilename"]).is_file()


def test_resource_icons_exist_for_the_command_button() -> None:
    for size in ("16x16", "32x32", "64x64"):
        icon = ADDIN_DIR / "resources" / f"{size}.png"
        assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_entry_point_has_no_auto_open_switch_and_the_controller_never_auto_opens() -> None:
    entry = (ADDIN_DIR / "LCSC2Fusion.py").read_text(encoding="utf-8")
    assert "AUTO_OPEN" not in entry
    for name in ("LCSC2Fusion.py", "controller.py", "fusion_adapter.py"):
        assert "auto_open" not in (ADDIN_DIR / name).read_text(encoding="utf-8").lower()


def test_entry_point_defines_run_and_stop() -> None:
    tree = ast.parse((ADDIN_DIR / "LCSC2Fusion.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for name in ("run", "stop"):
        assert name in functions
        assert [arg.arg for arg in functions[name].args.args] == ["context"]


def python_files() -> list[Path]:
    return sorted(ADDIN_DIR.glob("*.py"))


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_adapter_imports_adsk() -> None:
    importers = {path.name for path in python_files() if "adsk" in imported_modules(path)}
    assert importers == {"fusion_adapter.py"}


def test_controller_and_core_service_do_not_import_the_core_service_module_at_top_level() -> None:
    for name in ("controller.py", "core_service.py"):
        tree = ast.parse((ADDIN_DIR / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in ("lcsc2fusion.service", "lcsc2fusion.client.easyeda")


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_sources_parse_with_python_311_grammar(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))


BINARY_SUFFIXES = (".ico", ".png")  # icon files of the browser window, not text
TEXT_FILES = [
    *python_files(),
    *(path for path in PALETTE_DIR.iterdir() if path.suffix not in BINARY_SUFFIXES),
    ROOT / "scripts" / "build_addin.py",
    ROOT / "scripts" / "make_icons.py",
]


@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: p.name)
def test_no_long_dashes_or_double_hyphens_in_text(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2014), chr(0x2013)):  # em dash, en dash
        assert dash not in text
    assert " -- " not in text


# Palette protocol ----------------------------------------------------------------------------


def test_js_actions_match_the_controller_constants() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert set(js_action_pairs(source, "TO_PYTHON").values()) == set(controller.INCOMING_ACTIONS)
    assert set(js_action_pairs(source, "FROM_PYTHON").values()) == set(controller.OUTGOING_ACTIONS)


def test_every_js_action_constant_is_used_by_the_page() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    for group in ("TO_PYTHON", "FROM_PYTHON"):
        for key in js_action_pairs(source, group):
            assert f"{group}.{key}" in source, f"{group}.{key} is defined but never used"


def test_palette_offers_the_fusion_library_and_the_separate_library_buttons() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert f"'{controller.BUTTON_ADD_TO_FUSION}'" in source
    assert f"'{controller.BUTTON_OPEN_SEPARATE}'" in source
    assert "Open in Fusion Electronics" not in source  # renamed: it is the fallback now
    assert "TO_PYTHON.ADD_TO_FUSION" in source
    assert "TO_PYTHON.OPEN_LIBRARY" in source
    assert "TO_PYTHON.OPEN_FOLDER" in source
    assert "FROM_PYTHON.FUSION_DONE" in source


def test_js_add_request_carries_exactly_the_controller_fields() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"send\(TO_PYTHON\.ADD, \{([^}]*)\}\)", source)
    assert match is not None, "send(TO_PYTHON.ADD, {...}) not found in app.js"
    keys = {part.split(":")[0].strip() for part in match.group(1).split(",") if part.strip()}
    assert keys == set(controller.ADD_REQUEST_FIELDS)
    assert "with_3d: wants3d()" in match.group(1)
    assert "fusion: wantsFusion()" in match.group(1)


def test_js_add_to_fusion_request_carries_exactly_the_controller_fields() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    match = re.search(r"send\(TO_PYTHON\.ADD_TO_FUSION, \{([^}]*)\}\)", source)
    assert match is not None, "send(TO_PYTHON.ADD_TO_FUSION, {...}) not found in app.js"
    keys = {part.split(":")[0].strip() for part in match.group(1).split(",") if part.strip()}
    assert keys == set(controller.ADD_TO_FUSION_REQUEST_FIELDS)


def test_js_reads_every_field_of_the_fusion_done_message() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    for field in controller.FUSION_DONE_FIELDS:
        pattern = rf"\b(?:data|result)\.{field}\b"
        assert re.search(pattern, source), f"app.js never reads {field} of fusion_done"


def test_js_reads_every_field_of_the_added_message() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    for field in controller.ADDED_FIELDS:
        assert re.search(rf"\bdata\.{field}\b", source), f"app.js never reads data.{field}"


def test_mock_added_message_has_every_field_and_both_3d_outcomes_in_the_source() -> None:
    mock = (PALETTE_DIR / "mock-bridge.js").read_text(encoding="utf-8")
    block = mock[mock.index("deliver('added', {") :]
    block = block[: block.index("});")]
    for field in controller.ADDED_FIELDS:
        assert re.search(rf"\b{field}:", block), f"mock added message lacks {field}"
    assert "null" in block  # the "3D requested but not produced" case sends null paths


def test_mock_fusion_done_message_has_every_field() -> None:
    mock = (PALETTE_DIR / "mock-bridge.js").read_text(encoding="utf-8")
    start = mock.index("function handOff(")
    block = mock[start : mock.index("function add(", start)]
    for field in controller.FUSION_DONE_FIELDS:
        assert re.search(rf"\b{field}\b", block), f"mock fusion_done lacks {field}"
    assert "deliver('fusion_done', result)" in block


def test_mock_handles_every_page_to_python_action() -> None:
    mock = (PALETTE_DIR / "mock-bridge.js").read_text(encoding="utf-8")
    handled = set(re.findall(r"case '([a-z_]+)':", mock))
    assert set(controller.INCOMING_ACTIONS) <= handled


def test_js_follows_the_documented_bridge_contract() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "fusionSendData" in source
    assert "window.fusionJavaScriptHandler" in source
    assert "return 'OK'" in source
    assert "innerHTML" not in source
    assert "eval(" not in source


MOCK_SCRIPT = """
global.window = {};
require(process.argv[1]);
const sent = [];
const bridge = window.LcscMock.install((action, data) => sent.push([action, data]));
const cases = [['C25804', true], ['C2913202', true], ['C25804', false], ['C2913202', false]];
for (const [id, with3d] of cases) {
  bridge.fusionSendData('add', JSON.stringify({ lcsc_id: id, with_3d: with3d }));
}
setTimeout(() => {
  const added = sent.filter(([action]) => action === 'added');
  console.log(JSON.stringify(added.map(([, data]) => data)));
}, 1500);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_mock_bridge_shows_3d_produced_and_3d_unavailable() -> None:
    completed = subprocess.run(
        [str(shutil.which("node")), "-e", MOCK_SCRIPT, str(PALETTE_DIR / "mock-bridge.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    produced, unavailable, plain, plain_no_model = json.loads(completed.stdout)
    for message in (produced, unavailable, plain, plain_no_model):
        assert set(message) == set(controller.ADDED_FIELDS)

    assert produced["with_3d"] is True
    assert produced["step_path"].endswith("C25804.step")
    assert produced["placed_step_path"].endswith("C25804.placed.step")

    assert unavailable["with_3d"] is True
    assert unavailable["step_path"] is None
    assert unavailable["placed_step_path"] is None
    assert any("3D model" in warning for warning in unavailable["warnings"])

    for message in (plain, plain_no_model):
        assert message["with_3d"] is False
        assert message["step_path"] is None
        assert message["placed_step_path"] is None
        assert not any("3D" in warning for warning in message["warnings"])


MOCK_FUSION_SCRIPT = """
global.window = {};
const realSetTimeout = setTimeout;
global.setTimeout = (fn, ms) => realSetTimeout(fn, Math.floor(ms / 25));  // run the mock fast
require(process.argv[1]);
const sent = [];
const bridge = window.LcscMock.install((action, data) => sent.push([action, data]));
const send = (action, payload) => bridge.fusionSendData(action, JSON.stringify(payload));
const at = (ms, fn) => realSetTimeout(fn, ms);

send('add', { lcsc_id: 'C25804', with_3d: true, fusion: true });     // creates, 3D started
at(300, () => send('add', { lcsc_id: 'C14663', with_3d: true, fusion: true }));  // 3D fails
at(600, () => send('add', { lcsc_id: 'C2040', with_3d: false, fusion: false })); // no hand-off
at(900, () => send('add_to_fusion', { lcsc_id: 'C2040' }));          // merged by the button
at(1200, () => send('add', { lcsc_id: 'C2040', with_3d: true, fusion: true }));  // already there
at(1500, () => send('add', { lcsc_id: 'C2913202', with_3d: false, fusion: true }));  // fails
at(1800, () => send('add_to_fusion', { lcsc_id: 'C2913202' }));      // retry works
at(2100, () => send('add_to_fusion', { lcsc_id: 'C1' }));            // unknown id
at(2600, () => console.log(JSON.stringify(sent)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_mock_bridge_plays_every_fusion_hand_off_outcome() -> None:
    completed = subprocess.run(
        [str(shutil.which("node")), "-e", MOCK_FUSION_SCRIPT, str(PALETTE_DIR / "mock-bridge.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    sent = json.loads(completed.stdout)
    done: dict[str, list[dict[str, object]]] = {}
    for action, data in sent:
        if action == "fusion_done":
            assert set(data) == set(controller.FUSION_DONE_FIELDS)
            done.setdefault(data["lcsc_id"], []).append(data)

    created = done["C25804"][0]
    assert (created["ok"], created["created_library"], created["merged"]) == (True, True, False)
    assert created["three_d"] == "started"
    assert created["library_name"] == "LCSC2Fusion Library"

    broken_3d = done["C14663"][0]
    assert broken_3d["ok"] is True
    assert broken_3d["three_d"] == "failed"
    assert broken_3d["merged"] is True
    assert str(broken_3d["warnings"][0]).startswith("3D: ")  # type: ignore[index]

    first, second = done["C2040"]  # the button, then the second add with 3D
    assert (first["merged"], first["already_present"], first["three_d"]) == (True, False, "none")
    assert (second["already_present"], second["merged"], second["three_d"]) == (
        True,
        False,
        "started",
    )

    failed, retried = done["C2913202"]
    assert failed["ok"] is False
    assert failed["message"]
    assert (retried["ok"], retried["merged"]) == (True, True)

    errors = [data["message"] for action, data in sent if action == "error"]
    assert len(errors) == 1
    assert "C1" in errors[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_mock_page_stays_busy_from_the_add_until_fusion_done() -> None:
    script = """
global.window = {};
const realSetTimeout = setTimeout;
global.setTimeout = (fn, ms) => realSetTimeout(fn, Math.floor(ms / 25));
require(process.argv[1]);
const sent = [];
const bridge = window.LcscMock.install((action, data) => sent.push([action, data]));
bridge.fusionSendData('add', JSON.stringify({ lcsc_id: 'C25804', with_3d: false, fusion: true }));
realSetTimeout(() => console.log(JSON.stringify(sent)), 800);
"""
    completed = subprocess.run(
        [str(shutil.which("node")), "-e", script, str(PALETTE_DIR / "mock-bridge.js")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    names = [action for action, _ in json.loads(completed.stdout)]
    busy_values = [d["value"] for a, d in json.loads(completed.stdout) if a == "busy"]
    assert names.index("added") < names.index("fusion_done")
    assert names[-2:] == ["fusion_done", "busy"]
    assert busy_values[-1] is False
    assert busy_values.count(False) == 1  # idle only after fusion_done


def test_mock_is_inert_in_fusion() -> None:
    app = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    mock = (PALETTE_DIR / "mock-bridge.js").read_text(encoding="utf-8")
    assert not re.search(r"window\.adsk\s*=[^=]", app)
    assert not re.search(r"window\.adsk\s*=[^=]", mock)
    assert not re.search(r"\badsk\s*=[^=]", mock)
    assert "window.LcscMock.install" in app  # only called after the bridge wait timed out


# HTML, CSS, accessibility, attribution -------------------------------------------------------


def test_html_references_existing_local_files_only() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert {"style.css", "app.js", "mock-bridge.js", "http-bridge.js"} <= set(references)
    for reference in references:
        assert not re.match(r"[a-z]+://", reference), f"external resource: {reference}"
        assert (PALETTE_DIR / reference).is_file(), reference


def test_no_external_urls_in_scripts_and_styles() -> None:
    for name in ("app.js", "mock-bridge.js", "http-bridge.js", "style.css"):
        text = (PALETTE_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), name
        assert "@import" not in text


def test_footer_carries_the_source_attribution() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    assert ATTRIBUTION in html


def test_html_has_the_required_controls_and_accessibility_hooks() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    assert '<label for="query">' in html
    assert 'id="query"' in html
    assert 'type="submit"' in html
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html
    assert 'lang="en"' in html
    assert "<table" in html
    for column in ("LCSC", "MPN", "Type", "Manufacturer", "Package", "Description"):
        assert f">{column}</th>" in html


def test_fusion_checkbox_has_a_label_and_is_on_by_default() -> None:
    page = parse_html()
    found = page.find("to-fusion")
    assert found is not None, "checkbox #to-fusion is missing"
    tag, attrs = found
    assert tag == "input"
    assert attrs["type"] == "checkbox"
    assert "checked" in attrs  # the hand-off is what most people want
    index = [a.get("for") for t, a in page.tags if t == "label"].index("to-fusion")
    assert "Add to the Fusion library right away" in " ".join(page.label_texts[index].split())
    assert "input" in page.label_children[index]  # the input sits inside its own label
    describedby = attrs["aria-describedby"]
    assert describedby is not None
    assert page.find(describedby) is not None
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    form = html[html.index('<form id="search-form"') : html.index("</form>")]
    assert 'id="to-fusion"' in form
    assert 'id="with-3d"' in form  # next to the 3D choice


def test_the_manual_3d_steps_are_titled_as_the_fallback() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    template = html[html.index('<template id="model-template">') : html.index("</template>")]
    assert "<summary>Attach the 3D model by hand</summary>" in template
    assert controller.BUTTON_OPEN_SEPARATE in template  # step 1 names the renamed button


def test_fusion_state_has_styles_and_is_never_written_as_markup() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    for state_class in ("fusion-idle", "fusion-sending", "fusion-ok", "fusion-warn", "fusion-fail"):
        assert f".{state_class}" in css, state_class
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "insertAdjacentHTML" not in source
    assert "fusion-" in source  # the class names app.js sets on the card


def test_with_3d_checkbox_has_a_label_and_is_off_by_default() -> None:
    page = parse_html()
    found = page.find("with-3d")
    assert found is not None, "checkbox #with-3d is missing"
    tag, attrs = found
    assert tag == "input"
    assert attrs["type"] == "checkbox"
    assert "checked" not in attrs  # opt-in
    label = next(a for t, a in page.tags if t == "label" and a.get("for") == "with-3d")
    assert label is not None
    index = [a.get("for") for t, a in page.tags if t == "label"].index("with-3d")
    assert "Include 3D model (STEP)" in " ".join(page.label_texts[index].split())
    assert "input" in page.label_children[index]  # the input sits inside its own label
    describedby = attrs["aria-describedby"]
    assert describedby is not None
    assert page.find(describedby) is not None
    # it belongs to the search form, next to the search field
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    form = html[html.index('<form id="search-form"') : html.index("</form>")]
    assert 'id="with-3d"' in form
    assert 'id="query"' in form


def test_palette_remembers_the_3d_choice_without_browser_storage() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "indexedDB" not in source


def test_every_element_id_used_by_app_js_exists_in_the_html() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", source))
    assert {"with-3d", "model-template"} <= used
    page = parse_html()
    missing = sorted(name for name in used if page.find(name) is None)
    assert missing == []


def test_3d_instructions_use_the_official_fusion_steps() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    template = html[html.index('<template id="model-template">') : html.index("</template>")]
    steps = re.search(r'<ol class="model-steps">(.*?)</ol>', template, re.DOTALL)
    assert steps is not None
    items = [
        " ".join(re.sub(r"<[^>]+>", "", item).split())
        for item in re.findall(r"<li>(.*?)</li>", steps.group(1), re.DOTALL)
    ]
    assert len(items) == 5
    assert "Open the library in Fusion Electronics" in items[0]
    assert "save it to a project" in items[0]
    assert "Content Manager" in items[1]
    assert "Footprints tab" in items[1]
    assert "right click the footprint" in items[1]
    assert '"Attach Existing 3D Model"' in items[1]
    assert '"Select from my computer..."' in items[2]
    assert "placed STEP file" in items[2]
    assert 'class="model-pick"' in template  # the file name goes here
    assert "Name the package and press OK" in items[3]
    assert "edit the package to adjust the alignment" in items[4]
    assert "File &gt; Save" in template
    for selector in ("model-placed", "model-raw", "model-placed-row", "model-raw-row"):
        assert f'"{selector}"' in template or f" {selector}" in template
    assert (
        "Model data: JLCEDA/EasyEDA Official Library, https://easyeda.com . Personal use only."
        in template
    )


def test_3d_ui_pieces_have_styles() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    for selector in (
        ".model-help",
        ".model-steps",
        ".model-files",
        ".model-missing",
        "label.check",
    ):
        assert selector in css, selector
    app = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "model-missing" in app  # the "requested but not produced" warning line
    assert "buildModelHelp" in app


def test_css_supports_both_color_schemes() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css


# Separate browser window: http-bridge.js and the connection banner -----------------------------


def js_code(name: str) -> str:
    """A palette script without comments, so that the checks do not trip over prose."""
    text = (PALETTE_DIR / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|(?<=\s)//.*$", "", text)


def test_html_loads_the_http_bridge_after_the_mock_and_before_app_js() -> None:
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', html)
    assert scripts == ["mock-bridge.js", "http-bridge.js", "app.js"]


def test_html_has_no_inline_scripts_styles_or_event_handlers() -> None:
    # The window is served with CSP default-src 'self': any of these would be blocked.
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE):
        assert match.group(1).strip() == ""
    for tag, attrs in parse_html().tags:
        assert tag != "style"
        assert "style" not in attrs, f"inline style on <{tag}>"
        assert not [name for name in attrs if name.startswith("on")], f"event handler on <{tag}>"


def test_html_has_the_connection_banner_at_the_top_of_main() -> None:
    page = parse_html()
    found = page.find("conn-banner")
    assert found is not None, "#conn-banner is missing"
    tag, attrs = found
    assert tag == "div"
    assert attrs["class"] == "conn-banner"
    assert attrs["role"] == "status"
    assert attrs["aria-live"] == "polite"
    assert "hidden" in attrs  # shown only by http-bridge.js
    main_index = [name for name, _ in page.tags].index("main")
    assert page.tags[main_index + 1][1].get("id") == "conn-banner"
    html = (PALETTE_DIR / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<div id="conn-banner"[^>]*></div>', html)  # the text is set by the script


def test_http_bridge_installs_only_for_http_pages_without_adsk() -> None:
    code = js_code("http-bridge.js")
    guard = r"if \(window\.location\.protocol !== 'http:' \|\| window\.adsk\) return;"
    assert re.search(guard, code)
    # the guard comes before anything is installed
    assert code.index("return;") < code.index("window.adsk =")
    assert code.index("return;") < code.index("new EventSource")


def test_http_bridge_is_csp_clean_and_writes_text_only() -> None:
    raw = (PALETTE_DIR / "http-bridge.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://", raw)
    code = js_code("http-bridge.js")
    for forbidden in (
        "eval(",
        "new Function",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "indexedDB",
    ):
        assert forbidden not in code, forbidden
    assert not re.search(r"setTimeout\(\s*['\"`]", code)  # no string timers
    assert "'use strict'" in code
    assert code.count("textContent") >= 2  # the banner text is set (and cleared) as text


def test_http_bridge_talks_to_the_documented_endpoints_only() -> None:
    code = js_code("http-bridge.js")
    assert set(re.findall(r"'(/[a-z/]+)'", code)) == {"/api/act", "/api/events", "/api/ping"}
    assert "method: 'POST'" in code
    assert "'Content-Type': 'application/json'" in code
    assert "'X-LCSC': '1'" in code
    assert code.count("credentials: 'same-origin'") == 2  # the POST and the ping probe
    assert "new EventSource(EVENTS_URL)" in code
    assert "AbortController" in code


def test_http_bridge_handles_the_shutdown_action_that_the_page_does_not_know() -> None:
    code = js_code("http-bridge.js")
    assert "ACTION_SHUTDOWN = 'shutdown'" in code
    assert "window.close()" in code
    app = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "shutdown" not in js_action_pairs(app, "FROM_PYTHON").values()


def test_http_bridge_leaves_the_pages_handler_and_the_mock_alone() -> None:
    code = js_code("http-bridge.js")
    assert not re.search(r"fusionJavaScriptHandler\s*=[^=]", code)  # app.js defines it
    assert "LcscMock" not in code
    assert set(re.findall(r"window\.([A-Za-z_]\w*)\s*=[^=]", code)) == {"adsk"}
    assert "'__lcscHttpBridge'" in code  # the read only test hook


def test_http_bridge_sticks_to_es2019_syntax() -> None:
    code = js_code("http-bridge.js")
    for pattern in (
        r"\?\.",
        r"\?\?",
        r"\.replaceAll\(",
        r"\.at\(",
        r"allSettled",
        r"Promise\.any",
        r"\bBigInt\b",
    ):
        assert not re.search(pattern, code), pattern


def test_connection_banner_is_styled_with_the_theme_variables() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    match = re.search(r"\.conn-banner \{(.*?)\}", css, re.DOTALL)
    assert match is not None
    block = match.group(1)
    assert "var(--warn)" in block
    assert "var(--warn-bg)" in block
    assert "#" not in block  # no hard coded colour, so both themes work


# Build script --------------------------------------------------------------------------------


def make_fake_root(root: Path) -> None:
    addin = root / "addin" / "LCSC2Fusion"
    (addin / "palette").mkdir(parents=True)
    (addin / "LCSC2Fusion.py").write_text("def run(context): pass\n", encoding="utf-8")
    (addin / "LCSC2Fusion.manifest").write_text(
        (ADDIN_DIR / "LCSC2Fusion.manifest").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (addin / "palette" / "index.html").write_text("<html></html>", encoding="utf-8")
    (addin / "__pycache__").mkdir()
    (addin / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\0")
    core = root / "lcsc2fusion"
    (core / "sub").mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "sub" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (core / "sub" / "mod.pyc").write_bytes(b"\0")
    (core / "__pycache__").mkdir()
    (core / "__pycache__" / "y.pyc").write_bytes(b"\0")
    (core / "tests").mkdir()
    (core / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (root / "LICENSE").write_text("MIT", encoding="utf-8")


def test_build_produces_folder_and_zip_with_expected_members(
    build_addin: ModuleType, tmp_path: Path
) -> None:
    result = build_addin.build(root=ROOT, dist=tmp_path)
    assert result.folder == tmp_path / "LCSC2Fusion"
    assert result.zip_path == tmp_path / "LCSC2Fusion.zip"
    with zipfile.ZipFile(result.zip_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
    assert names == sorted(names)
    assert all(name.startswith("LCSC2Fusion/") for name in names)
    expected = {
        "LCSC2Fusion/LCSC2Fusion.py",
        "LCSC2Fusion/LCSC2Fusion.manifest",
        "LCSC2Fusion/controller.py",
        "LCSC2Fusion/core_service.py",
        "LCSC2Fusion/fusion_adapter.py",
        "LCSC2Fusion/palette/index.html",
        "LCSC2Fusion/palette/app.js",
        "LCSC2Fusion/palette/style.css",
        "LCSC2Fusion/palette/mock-bridge.js",
        "LCSC2Fusion/palette/http-bridge.js",
        "LCSC2Fusion/resources/16x16.png",
        "LCSC2Fusion/lcsc2fusion/__init__.py",
        "LCSC2Fusion/lcsc2fusion/model.py",
        "LCSC2Fusion/lcsc2fusion/errors.py",
        "LCSC2Fusion/lcsc2fusion/service.py",
    }
    assert expected <= set(names)
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    assert not [n for n in names if "/tests/" in n]
    assert set(result.files) == {n.removeprefix("LCSC2Fusion/") for n in names}
    extracted = tmp_path / "LCSC2Fusion"
    assert (extracted / "LCSC2Fusion.manifest").is_file()
    assert (extracted / "lcsc2fusion" / "__init__.py").is_file()


ICON_FILES = (
    "palette/favicon.ico",
    "palette/icon-192.png",
    "palette/icon-512.png",
    "palette/manifest.webmanifest",
    "resources/16x16.png",
    "resources/32x32.png",
    "resources/64x64.png",
)


def test_build_ships_the_icon_files_byte_for_byte(build_addin: ModuleType, tmp_path: Path) -> None:
    result = build_addin.build(root=ROOT, dist=tmp_path)
    with zipfile.ZipFile(result.zip_path) as archive:
        for relative in ICON_FILES:
            shipped = archive.read(f"LCSC2Fusion/{relative}")
            assert shipped == (ADDIN_DIR / relative).read_bytes(), relative
            assert (tmp_path / "LCSC2Fusion" / relative).read_bytes() == shipped
            assert relative in result.files
    assert not [name for name in result.files if name.endswith("make_icons.py")]  # dev tool only


def test_the_build_script_stays_stdlib_only_so_the_icons_are_committed_assets() -> None:
    tree = ast.parse((ROOT / "scripts" / "build_addin.py").read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }
    assert "PIL" not in imported
    assert imported <= set(sys.stdlib_module_names)


def test_build_is_deterministic(build_addin: ModuleType, tmp_path: Path) -> None:
    digests = []
    for run in ("a", "b"):
        result = build_addin.build(root=ROOT, dist=tmp_path / run)
        digests.append(hashlib.sha256(result.zip_path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_build_filters_caches_bytecode_and_tests(build_addin: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    result = build_addin.build(root=root, dist=tmp_path / "dist")
    assert result.files == (
        "LCSC2Fusion.manifest",
        "LCSC2Fusion.py",
        "LICENSE",
        "lcsc2fusion/__init__.py",
        "lcsc2fusion/sub/mod.py",
        "palette/index.html",
    )


def test_build_replaces_a_previous_build(build_addin: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    dist = tmp_path / "dist"
    build_addin.build(root=root, dist=dist)
    stale = dist / "LCSC2Fusion" / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    build_addin.build(root=root, dist=dist)
    assert not stale.exists()


def test_validation_rejects_mismatched_names(build_addin: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    addin = root / "addin" / "LCSC2Fusion"
    (addin / "LCSC2Fusion.py").rename(addin / "Other.py")
    with pytest.raises(build_addin.BuildError, match=r"LCSC2Fusion\.py"):
        build_addin.build(root=root, dist=tmp_path / "dist")


@pytest.mark.parametrize(
    ("manifest_text", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "JSON object"),
        ('{"type": "addin"}', "required keys"),
    ],
)
def test_validation_rejects_bad_manifests(
    build_addin: ModuleType, tmp_path: Path, manifest_text: str, message: str
) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    (root / "addin" / "LCSC2Fusion" / "LCSC2Fusion.manifest").write_text(
        manifest_text, encoding="utf-8"
    )
    with pytest.raises(build_addin.BuildError, match=message):
        build_addin.build(root=root, dist=tmp_path / "dist")


def test_validation_rejects_wrong_type(build_addin: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    path = root / "addin" / "LCSC2Fusion" / "LCSC2Fusion.manifest"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["type"] = "script"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(build_addin.BuildError, match="addin"):
        build_addin.build(root=root, dist=tmp_path / "dist")


def test_build_requires_the_core_package(build_addin: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "root"
    make_fake_root(root)
    (root / "lcsc2fusion" / "__init__.py").unlink()
    with pytest.raises(build_addin.BuildError, match="Core package"):
        build_addin.build(root=root, dist=tmp_path / "dist")


def test_main_prints_install_hint_and_returns_zero(
    build_addin: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert build_addin.main(["--dist", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "FusionAddins" in out
    assert "LCSC2Fusion.zip" in out
