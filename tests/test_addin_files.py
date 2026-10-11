"""Static checks of the add-in files and of the build script (no Fusion needed)."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
import zipfile
from collections.abc import Iterator
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
    assert manifest["runOnStartup"] is False
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


TEXT_FILES = [*python_files(), *PALETTE_DIR.iterdir(), ROOT / "scripts" / "build_addin.py"]


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


def test_palette_offers_the_open_in_fusion_button() -> None:
    source = (PALETTE_DIR / "app.js").read_text(encoding="utf-8")
    assert "Open in Fusion Electronics" in source
    assert "TO_PYTHON.OPEN_LIBRARY" in source
    assert "TO_PYTHON.OPEN_FOLDER" in source


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
    assert {"style.css", "app.js", "mock-bridge.js"} <= set(references)
    for reference in references:
        assert not re.match(r"[a-z]+://", reference), f"external resource: {reference}"
        assert (PALETTE_DIR / reference).is_file(), reference


def test_no_external_urls_in_scripts_and_styles() -> None:
    for name in ("app.js", "mock-bridge.js", "style.css"):
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


def test_css_supports_both_color_schemes() -> None:
    css = (PALETTE_DIR / "style.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css


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
