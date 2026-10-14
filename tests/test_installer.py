"""Tests of the Windows installer scripts (installer/): static checks and real runs.

The runs start the real Windows PowerShell 5.1 scripts exactly like the .bat files do, inside a
sandbox profile (see installer_support.py), so nothing can touch the real Fusion add-in folder.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from installer_support import (
    DEFAULT_PAYLOAD,
    INSTALLER_DIR,
    MANIFEST,
    POWERSHELL,
    ROOT,
    WINDOWS_ONLY,
    Sandbox,
    load_build_release,
    make_junction,
    make_release_root,
    run_script,
    tree_bytes,
)

SCRIPTS = ("install.ps1", "uninstall.ps1")
BATCH_FILES = ("Install-LCSC2Fusion.bat", "Uninstall-LCSC2Fusion.bat", "bootstrap.cmd")
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
UNSET = object()  # "do not pass -TargetRoot at all"


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path / "profile")


@pytest.fixture
def release(tmp_path: Path) -> Path:
    return make_release_root(tmp_path / "release")


def install(
    release: Path,
    sandbox: Sandbox,
    *extra: str,
    target: object = UNSET,
    language: str | None = "en",
    env: dict[str, str | None] | None = None,
    script: str = "install.ps1",
) -> subprocess.CompletedProcess[str]:
    args: list[str] = []
    if language is not None:
        args += ["-Language", language]
    if target is UNSET:
        args += ["-TargetRoot", str(sandbox.target)]
    elif target is not None:
        args += ["-TargetRoot", str(target)]
    return run_script(release / script, *args, *extra, sandbox=sandbox, env=env)


def text_of(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr


def fusion_is_running() -> bool:
    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Fusion360.exe", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "Fusion360.exe" in listing.stdout


def leftovers(target: Path) -> list[str]:
    """Names in the target folder that are not the add-in (staging folders and the like)."""
    return sorted(p.name for p in target.iterdir() if p.name != "LCSC2Fusion")


# Static checks (any platform) ----------------------------------------------------------------


def installer_text(name: str) -> str:
    return (INSTALLER_DIR / name).read_bytes().decode("utf-8-sig")


def test_installer_folder_holds_exactly_the_documented_files() -> None:
    build_release = load_build_release()
    expected = {*build_release.INSTALLER_ROOT_FILES, build_release.BOOTSTRAP_FILE}
    assert {path.name for path in INSTALLER_DIR.iterdir()} == expected


@pytest.mark.parametrize("name", ["install.ps1", "uninstall.ps1", "README.txt"])
def test_checked_in_text_files_are_utf8_with_bom_and_crlf(name: str) -> None:
    data = (INSTALLER_DIR / name).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), f"{name} needs a UTF-8 BOM (PowerShell 5.1, Notepad)"
    assert b"\n" in data
    assert data.replace(b"\r\n", b"").count(b"\n") == 0, f"{name} has bare LF line ends"
    assert data.replace(b"\r\n", b"").count(b"\r") == 0, f"{name} has bare CR line ends"


@pytest.mark.parametrize("name", BATCH_FILES)
def test_checked_in_batch_files_are_ascii_with_crlf(name: str) -> None:
    data = (INSTALLER_DIR / name).read_bytes()
    data.decode("ascii")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.replace(b"\r\n", b"").count(b"\n") == 0, f"{name} has bare LF line ends"


def test_gitattributes_keeps_windows_scripts_on_crlf() -> None:
    lines = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for pattern in ("*.bat", "*.cmd", "*.ps1"):
        assert f"{pattern} text eol=crlf" in lines


@pytest.mark.parametrize("name", [*SCRIPTS, *BATCH_FILES, "README.txt"])
def test_installer_texts_have_no_long_dashes_or_double_hyphens(name: str) -> None:
    text = installer_text(name)
    assert EM_DASH not in text
    assert EN_DASH not in text
    assert "--" not in text


@pytest.mark.parametrize("path", [Path(__file__), ROOT / "scripts" / "build_release.py"])
def test_python_sources_have_no_long_dashes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert EM_DASH not in text
    assert EN_DASH not in text
    assert " " + "-" * 2 + " " not in text


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_strict_and_stay_windows_powershell_5_compatible(name: str) -> None:
    text = installer_text(name)
    assert "Set-StrictMode -Version 2" in text
    assert "$ErrorActionPreference = 'Stop'" in text
    for forbidden in ("Invoke-Expression", "iex ", "-AsHashtable", "&&", "||"):
        assert forbidden not in text, forbidden
    assert not re.search(r"\?\.\w", text), "null conditional operator"
    assert not re.search(r"\$\w+\s*\?\s*\S+\s*:\s*\S+", text), "ternary operator"
    assert "??" not in text, "null coalescing operator"
    assert "Start-Process" not in text
    assert "Net.WebClient" not in text
    assert "Invoke-WebRequest" not in text  # no network, ever


def shared_region(name: str) -> str:
    match = re.search(
        r"^#region shared$.*?^#endregion shared$",
        installer_text(name).replace("\r\n", "\n"),
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"{name} has no shared region"
    return match.group(0)


def test_the_shared_region_is_identical_in_both_scripts() -> None:
    assert shared_region("install.ps1") == shared_region("uninstall.ps1")
    assert len(shared_region("install.ps1")) > 2000


def message_tables(name: str) -> dict[str, dict[str, str]]:
    text = installer_text(name).replace("\r\n", "\n")
    tables: dict[str, dict[str, str]] = {}
    for language in ("en", "pl"):
        match = re.search(
            rf"^    {language} = @\{{\n(.*?)^    \}}$", text, re.DOTALL | re.MULTILINE
        )
        assert match is not None, f"{name} has no {language} table"
        body = match.group(1)
        entries = re.findall(r"^        (\w+) = '((?:[^']|'')*)'$", body, re.MULTILINE)
        assert len(entries) == len(body.splitlines()), f"{name} {language}: unparsed line"
        assert len({key for key, _ in entries}) == len(entries), f"{name} {language}: duplicate"
        tables[language] = {key: value.replace("''", "'") for key, value in entries}
    return tables


@pytest.mark.parametrize("name", SCRIPTS)
def test_english_and_polish_tables_have_the_same_keys_and_placeholders(name: str) -> None:
    tables = message_tables(name)
    assert set(tables["en"]) == set(tables["pl"])
    for key, english in tables["en"].items():
        placeholders = sorted(re.findall(r"\{(\d+)\}", english))
        assert placeholders == sorted(re.findall(r"\{(\d+)\}", tables["pl"][key])), key
    assert len(tables["en"]) >= 20


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_key_used_by_the_script_has_a_text(name: str) -> None:
    text = installer_text(name)
    used = set(re.findall(r"Get-Text '(\w+)'", text)) | set(re.findall(r"Fail \d+ '(\w+)'", text))
    assert used
    assert used <= set(message_tables(name)["en"])
    unused = set(message_tables(name)["en"]) - used
    assert unused <= {"title"} | {"err_target_empty"}, f"texts nobody uses: {sorted(unused)}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_polish_texts_have_diacritics_and_english_texts_are_ascii(name: str) -> None:
    tables = message_tables(name)
    for key, value in tables["en"].items():
        value.encode("ascii")
        assert key
    polish_with_diacritics = [v for v in tables["pl"].values() if re.search("[ąćęłńóśźż]", v)]
    assert len(polish_with_diacritics) >= 15
    for value in tables["pl"].values():
        assert EM_DASH not in value
        assert EN_DASH not in value


def test_the_bat_files_start_the_scripts_with_the_documented_options() -> None:
    for bat, script in (
        ("Install-LCSC2Fusion.bat", "install.ps1"),
        ("Uninstall-LCSC2Fusion.bat", "uninstall.ps1"),
    ):
        text = installer_text(bat)
        assert f'-NoProfile -ExecutionPolicy Bypass -File "%~dp0{script}" %*' in text
        assert "pause" in text
        assert "exit /b %RC%" in text


# Real runs: install ---------------------------------------------------------------------------


@WINDOWS_ONLY
def test_scripts_parse_with_the_windows_powershell_parser(tmp_path: Path) -> None:
    probe = (
        "foreach ($f in ($env:PARSE_FILES -split '\\|')) { $e = $null; $t = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); "
        "if ($e.Count) { $e | ForEach-Object { $_.ToString() }; exit 1 } }"
    )
    env = {**os.environ, "PARSE_FILES": "|".join(str(INSTALLER_DIR / name) for name in SCRIPTS)}
    completed = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", probe],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@WINDOWS_ONLY
def test_fresh_install_copies_every_file_and_leaves_nothing_else(
    release: Path, sandbox: Sandbox
) -> None:
    done = install(release, sandbox)
    assert done.returncode == 0, text_of(done)
    assert tree_bytes(sandbox.target / "LCSC2Fusion") == DEFAULT_PAYLOAD
    assert leftovers(sandbox.target) == []
    out = done.stdout
    assert "The installer files are complete and unchanged (5 files, add-in version 9.9.9)." in out
    assert "Done. LCSC2Fusion 9.9.9 is installed in" in out
    assert "start Autodesk Fusion" in out
    assert str(sandbox.data_dir) in out  # says where the untouched state lives
    assert not sandbox.data_dir.exists()  # and did not create it


@WINDOWS_ONLY
def test_reinstall_replaces_the_old_version_and_touches_nothing_else(
    release: Path, sandbox: Sandbox
) -> None:
    old = sandbox.target / "LCSC2Fusion"
    (old / "__pycache__").mkdir(parents=True)
    (old / "old_only.py").write_text("old", encoding="utf-8")
    (old / "LCSC2Fusion.py").write_text("old entry", encoding="utf-8")
    (old / "__pycache__" / "x.pyc").write_bytes(b"\0")
    sibling = sandbox.target / "OtherAddin"
    sibling.mkdir()
    (sibling / "OtherAddin.py").write_text("keep", encoding="utf-8")
    (sandbox.target / "loose.txt").write_text("keep too", encoding="utf-8")
    sandbox.data_dir.mkdir(parents=True)
    (sandbox.data_dir / "state.json").write_text('{"library": 1}', encoding="utf-8")
    (sandbox.data_dir / "browser-profile").mkdir()
    (sandbox.data_dir / "browser-profile" / "Cookies").write_bytes(b"cookie")
    kept = [
        sibling / "OtherAddin.py",
        sandbox.target / "loose.txt",
        sandbox.data_dir / "state.json",
        sandbox.data_dir / "browser-profile" / "Cookies",
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in kept}

    done = install(release, sandbox)

    assert done.returncode == 0, text_of(done)
    assert tree_bytes(old) == DEFAULT_PAYLOAD  # old_only.py and __pycache__ are gone
    assert "The previous version was replaced." in done.stdout
    assert leftovers(sandbox.target) == ["OtherAddin", "loose.txt"]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in kept} == before
    assert sorted(p.name for p in sandbox.data_dir.iterdir()) == ["browser-profile", "state.json"]


@WINDOWS_ONLY
def test_install_into_a_target_that_does_not_exist_yet_creates_it_with_parents(
    release: Path, sandbox: Sandbox
) -> None:
    target = sandbox.root / "deep" / "er" / "FusionAddins"
    done = install(release, sandbox, target=target)
    assert done.returncode == 0, text_of(done)
    assert tree_bytes(target / "LCSC2Fusion") == DEFAULT_PAYLOAD


@WINDOWS_ONLY
def test_without_options_the_default_is_appdata_autodesk_fusionaddins(
    release: Path, sandbox: Sandbox
) -> None:
    done = install(release, sandbox, target=None)
    assert done.returncode == 0, text_of(done)
    default = sandbox.appdata / "Autodesk" / "FusionAddins" / "LCSC2Fusion"
    assert tree_bytes(default) == DEFAULT_PAYLOAD
    assert "does not exist, so Autodesk Fusion may not be installed" in done.stdout


@WINDOWS_ONLY
def test_the_environment_variable_replaces_the_default_target(
    release: Path, sandbox: Sandbox
) -> None:
    target = sandbox.root / "from env"
    done = install(release, sandbox, target=None, env={"LCSC2FUSION_FUSIONADDINS_DIR": str(target)})
    assert done.returncode == 0, text_of(done)
    assert tree_bytes(target / "LCSC2Fusion") == DEFAULT_PAYLOAD
    assert not (sandbox.appdata / "Autodesk").exists()


@WINDOWS_ONLY
def test_paths_with_spaces_and_non_ascii_characters_work(tmp_path: Path, sandbox: Sandbox) -> None:
    odd = "Użytkownik Łódź 中文 (x86) & 'q' [1]"
    release = make_release_root(tmp_path / f"źródło {odd}")
    target = sandbox.root / f"dodatki {odd}" / "FusionAddins"
    done = install(release, sandbox, target=target)
    assert done.returncode == 0, text_of(done)
    assert tree_bytes(target / "LCSC2Fusion") == DEFAULT_PAYLOAD
    assert leftovers(target) == []
    again = install(release, sandbox, target=target)  # and a reinstall over it
    assert again.returncode == 0, text_of(again)
    assert leftovers(target) == []


@WINDOWS_ONLY
def test_the_downloaded_from_the_internet_mark_is_not_carried_over(
    release: Path, sandbox: Sandbox
) -> None:
    mark = Path(str(release / "LCSC2Fusion" / "LCSC2Fusion.py") + ":Zone.Identifier")
    try:
        mark.write_text("[ZoneTransfer]\r\nZoneId=3\r\n", encoding="ascii")
    except OSError:
        pytest.skip("the temp folder is not on NTFS: no alternate data streams")
    done = install(release, sandbox)
    assert done.returncode == 0, text_of(done)
    installed = sandbox.target / "LCSC2Fusion" / "LCSC2Fusion.py"
    assert installed.read_bytes() == DEFAULT_PAYLOAD["LCSC2Fusion.py"]
    with pytest.raises(FileNotFoundError):
        Path(str(installed) + ":Zone.Identifier").read_text()


# Real runs: language, silent and gui ----------------------------------------------------------


@WINDOWS_ONLY
def test_english_and_polish_output_are_complete_and_readable(
    release: Path, sandbox: Sandbox
) -> None:
    english = install(release, sandbox, language="en")
    assert english.returncode == 0, text_of(english)
    assert "Checking the installer files..." in english.stdout
    polish = install(release, sandbox, language="pl")
    assert polish.returncode == 0, text_of(polish)
    assert "Sprawdzanie plików instalatora..." in polish.stdout  # UTF-8 survives the pipe
    assert "Gotowe. LCSC2Fusion 9.9.9 jest zainstalowany w" in polish.stdout
    assert "Następny krok: uruchom Autodesk Fusion." in polish.stdout
    assert "Checking" not in polish.stdout


@WINDOWS_ONLY
def test_auto_follows_the_windows_display_language_and_falls_back_to_english(
    release: Path, sandbox: Sandbox
) -> None:
    def run_with_culture(culture: str, language: list[str]) -> str:
        command = (
            f"[System.Threading.Thread]::CurrentThread.CurrentUICulture = '{culture}'; "
            "& $env:TEST_SCRIPT -TargetRoot $env:TEST_TARGET " + " ".join(language) + "; "
            "exit $LASTEXITCODE"
        )
        completed = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=sandbox.env(
                {"TEST_SCRIPT": str(release / "install.ps1"), "TEST_TARGET": str(sandbox.target)}
            ),
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return completed.stdout

    assert "Sprawdzanie plików" in run_with_culture("pl-PL", [])
    assert "Checking the installer files" in run_with_culture("en-US", [])
    assert "Checking the installer files" in run_with_culture("de-DE", [])
    assert "Checking the installer files" in run_with_culture("pl-PL", ["-Language", "en"])
    assert "Sprawdzanie plików" in run_with_culture("en-US", ["-Language", "pl"])
    # an unknown language value counts as auto
    assert "Sprawdzanie plików" in run_with_culture("pl-PL", ["-Language", "xx", "-Silent"])
    assert "Checking the installer files" in run_with_culture("en-US", ["-Language", "xx"])


@WINDOWS_ONLY
def test_silent_mode_returns_the_exit_codes_and_shows_no_dialog(
    release: Path, sandbox: Sandbox
) -> None:
    hook = {"LCSC2FUSION_TEST_GUI": "yes"}  # would print a dialog line if one were requested
    ok = install(release, sandbox, "-Silent", "-Gui", env=hook)
    assert ok.returncode == 0, text_of(ok)
    assert "[dialog" not in ok.stdout
    (release / "LCSC2Fusion" / "LCSC2Fusion.py").write_bytes(b"changed")
    bad = install(release, sandbox, "-Silent", "-Gui", env=hook)
    assert bad.returncode == 11
    assert "[dialog" not in bad.stdout
    assert "Exit code: 11" in bad.stdout


@WINDOWS_ONLY
def test_gui_mode_reports_the_result_in_a_dialog(release: Path, sandbox: Sandbox) -> None:
    hook = {"LCSC2FUSION_TEST_GUI": "yes"}
    ok = install(release, sandbox, "-Gui", env=hook)
    assert ok.returncode == 0, text_of(ok)
    assert "[dialog:info] LCSC2Fusion setup: Done. LCSC2Fusion 9.9.9 is installed in" in ok.stdout
    (release / "LCSC2Fusion" / "LCSC2Fusion.py").write_bytes(b"changed")
    bad = install(release, sandbox, "-Gui", env=hook)
    assert bad.returncode == 11
    assert "[dialog:error] LCSC2Fusion setup: The installation failed." in bad.stdout
    polish = install(release, sandbox, "-Gui", language="pl", env=hook)
    assert "[dialog:error] Instalator LCSC2Fusion: Instalacja nie powiodła się." in polish.stdout


@WINDOWS_ONLY
def test_a_running_fusion_adds_the_restart_hint(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    fake = tmp_path / "fake" / "Fusion360.exe"  # the installer only looks at the process name
    fake.parent.mkdir()
    shutil.copyfile(Path(r"C:\Windows\System32\cmd.exe"), fake)
    holder = subprocess.Popen(
        [str(fake), "/c", "ping -n 20 127.0.0.1 > nul"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        done = install(release, sandbox)
    finally:
        holder.kill()
        holder.wait()
    assert done.returncode == 0, text_of(done)
    assert "Autodesk Fusion is running." in done.stdout
    assert "press Stop first and then Run" in done.stdout
    if not fusion_is_running():  # the person who runs the tests may have the real Fusion open
        quiet = install(release, sandbox)
        assert "Autodesk Fusion is not running." in quiet.stdout
        assert "press Stop first" not in quiet.stdout


# Real runs: refusals --------------------------------------------------------------------------


def _change_file(release: Path) -> None:
    (release / "LCSC2Fusion" / "LCSC2Fusion.py").write_bytes(b"def run(context): evil()\n")


def _add_file(release: Path) -> None:
    (release / "LCSC2Fusion" / "palette" / "extra.js").write_bytes(b"alert(1)")


def _remove_listed_file(release: Path) -> None:
    (release / "LCSC2Fusion" / "palette" / "index.html").unlink()


def _remove_hash_list(release: Path) -> None:
    (release / "payload.sha256").unlink()


def _garble_hash_list(release: Path) -> None:
    (release / "payload.sha256").write_bytes(b"this is not a checksum list\n")


def _empty_hash_list(release: Path) -> None:
    (release / "payload.sha256").write_bytes(b"")


def _hash_list_with_traversal(release: Path) -> None:
    lines = (release / "payload.sha256").read_bytes() + b"0" * 64 + b"  ../../evil.py\n"
    (release / "payload.sha256").write_bytes(lines)


def _hash_list_with_backslash(release: Path) -> None:
    lines = (release / "payload.sha256").read_bytes() + b"0" * 64 + b"  palette\\x.js\n"
    (release / "payload.sha256").write_bytes(lines)


def _hash_list_with_duplicate(release: Path) -> None:
    first = (release / "payload.sha256").read_bytes().splitlines(keepends=True)[0]
    (release / "payload.sha256").write_bytes((release / "payload.sha256").read_bytes() + first)


def _junction_in_payload(release: Path, tmp: Path) -> None:
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    make_junction(release / "LCSC2Fusion" / "linked", outside)


def _remove_manifest(release: Path) -> None:
    (release / "LCSC2Fusion" / "LCSC2Fusion.manifest").unlink()


def _remove_payload_folder(release: Path) -> None:
    shutil.rmtree(release / "LCSC2Fusion")


def _manifest_not_json(release: Path) -> None:
    (release / "LCSC2Fusion" / "LCSC2Fusion.manifest").write_bytes(b"{not json")


def _manifest_wrong_type(release: Path) -> None:
    manifest = release / "LCSC2Fusion" / "LCSC2Fusion.manifest"
    manifest.write_bytes(MANIFEST.replace(b"addin", b"script"))


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("tamper", "code", "message"),
    [
        (_change_file, 11, "damaged or were changed: LCSC2Fusion.py"),
        (_add_file, 11, "not on the checksum list: palette/extra.js"),
        (_remove_listed_file, 11, "damaged or were changed: palette/index.html"),
        (_remove_hash_list, 11, "checksum list is missing or unreadable"),
        (_garble_hash_list, 11, "checksum list is damaged (line 1)"),
        (_empty_hash_list, 11, "checksum list is damaged"),
        (_hash_list_with_traversal, 11, "checksum list is damaged"),
        (_hash_list_with_backslash, 11, "checksum list is damaged"),
        (_hash_list_with_duplicate, 11, "checksum list is damaged"),
        (_remove_manifest, 10, "manifest is missing or is not a valid add-in manifest"),
        (_remove_payload_folder, 10, "add-in files were not found next to the installer"),
        (_manifest_not_json, 10, "manifest is missing or is not a valid add-in manifest"),
        (_manifest_wrong_type, 10, "manifest is missing or is not a valid add-in manifest"),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else None,
)
def test_a_damaged_installer_is_refused_before_anything_is_written(
    release: Path,
    sandbox: Sandbox,
    tamper: Callable[[Path], None],
    code: int,
    message: str,
) -> None:
    old = sandbox.target / "LCSC2Fusion"
    old.mkdir(parents=True)
    (old / "LCSC2Fusion.py").write_text("old version", encoding="utf-8")
    tamper(release)
    done = install(release, sandbox)
    assert done.returncode == code, text_of(done)
    assert message in done.stdout
    assert "The installation failed." in done.stdout
    assert tree_bytes(old) == {"LCSC2Fusion.py": b"old version"}
    assert leftovers(sandbox.target) == []


@WINDOWS_ONLY
def test_a_junction_inside_the_installer_files_is_refused(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    _junction_in_payload(release, tmp_path)
    done = install(release, sandbox)
    assert done.returncode == 11, text_of(done)
    assert "contains a link" in done.stdout
    assert not sandbox.target.exists()
    assert (tmp_path / "outside" / "secret.txt").read_text(encoding="utf-8") == "x"


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "target",
    [
        "",
        "   ",
        "relative\\folder",
        "FusionAddins",
        "C:folder",
        "\\\\",
        "C:\\data\\..\\other",
        "C:/data/../other",
        "C:\\bad<name",
        "C:\\star*",
        "\\\\?\\C:\\x",
        "C:\\x:stream",
    ],
    ids=lambda value: repr(value),
)
def test_invalid_target_roots_are_refused_with_exit_code_12(
    release: Path, sandbox: Sandbox, target: str
) -> None:
    done = install(release, sandbox, target=target)
    assert done.returncode == 12, text_of(done)
    assert "The installation failed." in done.stdout
    assert "Exit code: 12" in done.stdout


@WINDOWS_ONLY
def test_a_junction_at_the_install_folder_is_refused_and_its_target_left_alone(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "LCSC2Fusion.manifest").write_text("keep", encoding="utf-8")
    (precious / "data.txt").write_text("keep", encoding="utf-8")
    sandbox.target.mkdir(parents=True)
    make_junction(sandbox.target / "LCSC2Fusion", precious)
    done = install(release, sandbox)
    assert done.returncode == 12, text_of(done)
    assert "is a link (junction or symbolic link)" in done.stdout
    assert tree_bytes(precious) == {"LCSC2Fusion.manifest": b"keep", "data.txt": b"keep"}
    assert leftovers(sandbox.target) == []


@WINDOWS_ONLY
def test_a_file_in_place_of_the_target_or_the_install_folder_is_refused(
    release: Path, sandbox: Sandbox
) -> None:
    sandbox.root.mkdir(parents=True, exist_ok=True)
    sandbox.target.write_text("i am a file", encoding="utf-8")
    done = install(release, sandbox)
    assert done.returncode == 12, text_of(done)
    assert "is a file, not a folder" in done.stdout
    assert sandbox.target.read_text(encoding="utf-8") == "i am a file"
    sandbox.target.unlink()
    sandbox.target.mkdir()
    (sandbox.target / "LCSC2Fusion").write_text("also a file", encoding="utf-8")
    again = install(release, sandbox)
    assert again.returncode == 12, text_of(again)
    assert (sandbox.target / "LCSC2Fusion").read_text(encoding="utf-8") == "also a file"


@WINDOWS_ONLY
def test_running_from_inside_the_install_location_is_refused(sandbox: Sandbox) -> None:
    # the extracted installer sits in the target itself: its LCSC2Fusion folder is the install dir
    in_target = make_release_root(sandbox.target)
    done = install(in_target, sandbox)
    assert done.returncode == 12, text_of(done)
    assert "running from inside the install location" in done.stdout
    assert tree_bytes(sandbox.target / "LCSC2Fusion") == DEFAULT_PAYLOAD
    # the installer sits inside an installed add-in folder
    nested = make_release_root(sandbox.root / "n" / "LCSC2Fusion" / "installer")
    done = install(nested, sandbox, target=sandbox.root / "n")
    assert done.returncode == 12, text_of(done)


# Real runs: failures and rollback -------------------------------------------------------------


def older_release(tmp_path: Path) -> Path:
    payload = {
        **DEFAULT_PAYLOAD,
        "LCSC2Fusion.py": b"old entry point\n",
        "old_only.py": b"gone after the update\n",
    }
    return make_release_root(tmp_path / "older release", payload)


@WINDOWS_ONLY
@pytest.mark.parametrize("fault", ["copy-midway", "after-aside", "after-swap"])
def test_a_failure_in_any_step_restores_the_previous_version(
    release: Path, sandbox: Sandbox, tmp_path: Path, fault: str
) -> None:
    first = install(older_release(tmp_path), sandbox)
    assert first.returncode == 0, text_of(first)
    before = tree_bytes(sandbox.target / "LCSC2Fusion")
    assert "old_only.py" in before
    (sandbox.target / "OtherAddin").mkdir()
    (sandbox.target / "OtherAddin" / "a.py").write_text("keep", encoding="utf-8")

    done = install(release, sandbox, env={"LCSC2FUSION_TEST_FAULT": fault})

    assert done.returncode == 13, text_of(done)
    assert "The previous version was left in place." in done.stdout
    assert "injected fault: " + fault in done.stdout
    assert tree_bytes(sandbox.target / "LCSC2Fusion") == before
    assert leftovers(sandbox.target) == ["OtherAddin"]  # no staging or aside folders
    assert (sandbox.target / "OtherAddin" / "a.py").read_text(encoding="utf-8") == "keep"
    if fault != "copy-midway":
        assert "The previous version was restored." in done.stdout
    retry = install(release, sandbox)  # and the next run works
    assert retry.returncode == 0, text_of(retry)
    assert tree_bytes(sandbox.target / "LCSC2Fusion") == DEFAULT_PAYLOAD


@WINDOWS_ONLY
@pytest.mark.parametrize("fault", ["copy-midway", "after-swap"])
def test_a_failed_first_install_leaves_no_trace(
    release: Path, sandbox: Sandbox, fault: str
) -> None:
    done = install(release, sandbox, env={"LCSC2FUSION_TEST_FAULT": fault})
    assert done.returncode == 13, text_of(done)
    assert list(sandbox.target.iterdir()) == []


@WINDOWS_ONLY
def test_a_locked_file_in_the_old_install_stops_the_swap_and_keeps_the_old_version(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    first = install(older_release(tmp_path), sandbox)
    assert first.returncode == 0, text_of(first)
    installed = sandbox.target / "LCSC2Fusion"
    before = tree_bytes(installed)
    with (installed / "LCSC2Fusion.py").open("rb"):  # Python opens without delete sharing
        done = install(release, sandbox)
    assert done.returncode == 13, text_of(done)
    assert "Could not replace the installed add-in" in done.stdout
    assert "press Stop" in done.stdout
    assert tree_bytes(installed) == before
    assert leftovers(sandbox.target) == []
    retry = install(release, sandbox)  # the lock is gone
    assert retry.returncode == 0, text_of(retry)
    assert tree_bytes(installed) == DEFAULT_PAYLOAD


# Real runs: uninstall -------------------------------------------------------------------------


def uninstall(
    release: Path,
    sandbox: Sandbox,
    *extra: str,
    target: object = UNSET,
    language: str | None = "en",
    env: dict[str, str | None] | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    args: list[str] = []
    if language is not None:
        args += ["-Language", language]
    if target is UNSET:
        args += ["-TargetRoot", str(sandbox.target)]
    elif target is not None:
        args += ["-TargetRoot", str(target)]
    return run_script(
        release / "uninstall.ps1", *args, *extra, sandbox=sandbox, env=env, stdin=stdin
    )


def installed(release: Path, sandbox: Sandbox) -> Path:
    done = install(release, sandbox)
    assert done.returncode == 0, text_of(done)
    return sandbox.target / "LCSC2Fusion"


def fill_data_dir(sandbox: Sandbox) -> None:
    (sandbox.data_dir / "browser-profile").mkdir(parents=True)
    (sandbox.data_dir / "state.json").write_text("{}", encoding="utf-8")
    (sandbox.data_dir / "api.json").write_text("{}", encoding="utf-8")


@WINDOWS_ONLY
def test_uninstall_removes_only_the_add_in_folder(release: Path, sandbox: Sandbox) -> None:
    addin = installed(release, sandbox)
    (sandbox.target / "OtherAddin").mkdir()
    (sandbox.target / "OtherAddin" / "OtherAddin.manifest").write_text("keep", encoding="utf-8")
    fill_data_dir(sandbox)

    done = uninstall(release, sandbox)

    assert done.returncode == 0, text_of(done)
    assert not addin.exists()
    assert leftovers(sandbox.target) == ["OtherAddin"]
    assert (sandbox.target / "OtherAddin" / "OtherAddin.manifest").read_text() == "keep"
    assert sorted(p.name for p in sandbox.data_dir.iterdir()) == [
        "api.json",
        "browser-profile",
        "state.json",
    ]
    assert "Done. LCSC2Fusion was uninstalled." in done.stdout


@WINDOWS_ONLY
def test_uninstall_in_polish(release: Path, sandbox: Sandbox) -> None:
    installed(release, sandbox)
    done = uninstall(release, sandbox, language="pl")
    assert done.returncode == 0, text_of(done)
    assert "Usunięto dodatek z" in done.stdout
    assert "Gotowe. LCSC2Fusion został odinstalowany." in done.stdout


@WINDOWS_ONLY
def test_uninstall_with_nothing_installed_is_exit_15(release: Path, sandbox: Sandbox) -> None:
    done = uninstall(release, sandbox)  # the target does not even exist
    assert done.returncode == 15, text_of(done)
    assert "Nothing to uninstall" in done.stdout
    assert not sandbox.target.exists()
    sandbox.target.mkdir()
    assert uninstall(release, sandbox).returncode == 15


@WINDOWS_ONLY
def test_uninstall_refuses_a_folder_without_the_manifest(release: Path, sandbox: Sandbox) -> None:
    folder = sandbox.target / "LCSC2Fusion"
    (folder / "important").mkdir(parents=True)
    (folder / "important" / "thesis.docx").write_text("do not delete", encoding="utf-8")
    done = uninstall(release, sandbox)
    assert done.returncode == 12, text_of(done)
    assert "does not contain LCSC2Fusion.manifest" in done.stdout
    assert tree_bytes(folder) == {"important/thesis.docx": b"do not delete"}


@WINDOWS_ONLY
def test_uninstall_refuses_a_junction_and_a_file(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "LCSC2Fusion.manifest").write_text("keep", encoding="utf-8")
    sandbox.target.mkdir(parents=True)
    link = sandbox.target / "LCSC2Fusion"
    make_junction(link, precious)
    done = uninstall(release, sandbox)
    assert done.returncode == 12, text_of(done)
    assert (precious / "LCSC2Fusion.manifest").read_text(encoding="utf-8") == "keep"
    subprocess.run(["cmd", "/c", "rmdir", str(link)], check=True)  # removes the link only
    link.write_text("a file", encoding="utf-8")
    assert uninstall(release, sandbox).returncode == 12
    assert link.read_text(encoding="utf-8") == "a file"


@WINDOWS_ONLY
@pytest.mark.parametrize("target", ["", "relative", "C:\\a\\..\\b"])
def test_uninstall_validates_the_target_root(release: Path, sandbox: Sandbox, target: str) -> None:
    done = uninstall(release, sandbox, target=target)
    assert done.returncode == 12, text_of(done)


@WINDOWS_ONLY
def test_uninstall_with_a_locked_file_keeps_the_add_in_whole(
    release: Path, sandbox: Sandbox
) -> None:
    addin = installed(release, sandbox)
    before = tree_bytes(addin)
    with (addin / "LCSC2Fusion.py").open("rb"):
        done = uninstall(release, sandbox)
    assert done.returncode == 13, text_of(done)
    assert "press Stop" in done.stdout
    assert tree_bytes(addin) == before
    assert leftovers(sandbox.target) == []
    assert uninstall(release, sandbox).returncode == 0


@WINDOWS_ONLY
def test_remove_data_asks_first_and_keeps_the_data_unless_answered_yes(
    release: Path, sandbox: Sandbox
) -> None:
    installed(release, sandbox)
    fill_data_dir(sandbox)
    refused = uninstall(release, sandbox, "-RemoveData", stdin="n\r\n")
    assert refused.returncode == 0, text_of(refused)
    assert sandbox.data_dir.is_dir()
    assert "Also delete your LCSC2Fusion settings and state in" in refused.stdout
    assert f"Your settings and state in {sandbox.data_dir} were kept." in refused.stdout
    installed(release, sandbox)
    no_answer = uninstall(release, sandbox, "-RemoveData", stdin="")  # EOF means no
    assert no_answer.returncode == 0, text_of(no_answer)
    assert sandbox.data_dir.is_dir()
    installed(release, sandbox)
    agreed = uninstall(release, sandbox, "-RemoveData", stdin="y\r\n")
    assert agreed.returncode == 0, text_of(agreed)
    assert not sandbox.data_dir.exists()
    assert f"Removed {sandbox.data_dir}" in agreed.stdout
    assert not (sandbox.target / "LCSC2Fusion").exists()


@WINDOWS_ONLY
def test_remove_data_in_silent_mode_does_not_ask(release: Path, sandbox: Sandbox) -> None:
    installed(release, sandbox)
    fill_data_dir(sandbox)
    done = uninstall(release, sandbox, "-RemoveData", "-Silent")
    assert done.returncode == 0, text_of(done)
    assert "[y/N]" not in done.stdout
    assert not sandbox.data_dir.exists()
    assert sandbox.localappdata.is_dir()  # only the LCSC2Fusion folder went


@WINDOWS_ONLY
def test_remove_data_in_gui_mode_uses_a_question_dialog(release: Path, sandbox: Sandbox) -> None:
    installed(release, sandbox)
    fill_data_dir(sandbox)
    declined = uninstall(
        release, sandbox, "-RemoveData", "-Gui", env={"LCSC2FUSION_TEST_GUI": "no"}
    )
    assert declined.returncode == 0, text_of(declined)
    assert "[dialog:question] LCSC2Fusion uninstall: Also delete your LCSC2Fusion settings" in (
        declined.stdout
    )
    assert sandbox.data_dir.is_dir()
    installed(release, sandbox)
    accepted = uninstall(
        release, sandbox, "-RemoveData", "-Gui", env={"LCSC2FUSION_TEST_GUI": "yes"}
    )
    assert accepted.returncode == 0, text_of(accepted)
    assert "[dialog:info] LCSC2Fusion uninstall: " in accepted.stdout
    assert not sandbox.data_dir.exists()


@WINDOWS_ONLY
def test_remove_data_works_when_the_add_in_is_already_gone(release: Path, sandbox: Sandbox) -> None:
    fill_data_dir(sandbox)
    done = uninstall(release, sandbox, "-RemoveData", "-Silent")
    assert done.returncode == 0, text_of(done)
    assert not sandbox.data_dir.exists()
    nothing = uninstall(release, sandbox, "-RemoveData", "-Silent")
    assert nothing.returncode == 15, text_of(nothing)
    assert "There is no settings folder" in nothing.stdout


@WINDOWS_ONLY
def test_remove_data_refuses_a_junction_and_a_broken_localappdata(
    release: Path, sandbox: Sandbox, tmp_path: Path
) -> None:
    installed(release, sandbox)
    precious = tmp_path / "precious-data"
    precious.mkdir()
    (precious / "state.json").write_text("keep", encoding="utf-8")
    sandbox.localappdata.mkdir(parents=True, exist_ok=True)
    make_junction(sandbox.data_dir, precious)
    done = uninstall(release, sandbox, "-RemoveData", "-Silent")
    assert done.returncode == 12, text_of(done)
    assert (precious / "state.json").read_text(encoding="utf-8") == "keep"
    subprocess.run(["cmd", "/c", "rmdir", str(sandbox.data_dir)], check=True)
    broken = uninstall(
        release, sandbox, "-RemoveData", "-Silent", env={"LOCALAPPDATA": "not\\absolute"}
    )
    assert broken.returncode == 12, text_of(broken)
    assert "settings location could not be determined" in broken.stdout


# The .bat wrappers ----------------------------------------------------------------------------


def run_bat(
    bat: Path, sandbox: Sandbox, args: str = "", env: dict[str, str | None] | None = None
) -> subprocess.CompletedProcess[str]:
    """cmd /s /c ""bat" args": the quoting that survives spaces and & ( ) in the path."""
    return subprocess.run(
        f'cmd /s /c ""{bat}" {args}"',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input="",  # pause reads end of file and goes on
        env=sandbox.env(env),
        timeout=180,
        check=False,
    )


@WINDOWS_ONLY
def test_install_and_uninstall_bat_files_run_the_scripts_and_return_their_exit_code(
    tmp_path: Path, sandbox: Sandbox
) -> None:
    release = make_release_root(tmp_path / "Użytkownik Łódź 中文 (x86) & more" / "release")
    target = sandbox.root / "bat target"
    env = {"LCSC2FUSION_FUSIONADDINS_DIR": str(target)}
    done = run_bat(release / "Install-LCSC2Fusion.bat", sandbox, "-Language en", env)
    assert done.returncode == 0, text_of(done)
    assert "Done. LCSC2Fusion 9.9.9 is installed in" in done.stdout
    assert tree_bytes(target / "LCSC2Fusion") == DEFAULT_PAYLOAD
    (release / "LCSC2Fusion" / "LCSC2Fusion.py").write_bytes(b"changed")
    failed = run_bat(release / "Install-LCSC2Fusion.bat", sandbox, "-Silent -Language pl", env)
    assert failed.returncode == 11
    assert "Instalacja nie powiodła się." in failed.stdout
    assert tree_bytes(target / "LCSC2Fusion") == DEFAULT_PAYLOAD  # the failed run changed nothing
    gone = run_bat(release / "Uninstall-LCSC2Fusion.bat", sandbox, "-Language en", env)
    assert gone.returncode == 0, text_of(gone)
    assert not (target / "LCSC2Fusion").exists()
    nothing = run_bat(release / "Uninstall-LCSC2Fusion.bat", sandbox, "-Language en", env)
    assert nothing.returncode == 15, text_of(nothing)


@WINDOWS_ONLY
def test_the_bat_files_explain_a_missing_script_instead_of_failing_silently(
    tmp_path: Path, sandbox: Sandbox
) -> None:
    lone = tmp_path / "just the bat"
    lone.mkdir()
    for name in ("Install-LCSC2Fusion.bat", "Uninstall-LCSC2Fusion.bat"):
        shutil.copyfile(INSTALLER_DIR / name, lone / name)
        done = run_bat(lone / name, sandbox)
        assert done.returncode == 10, text_of(done)
        assert "Extract the whole zip first" in done.stdout
        assert "rozpakuj caly plik zip" in done.stdout


@pytest.mark.parametrize("name", [*SCRIPTS, *BATCH_FILES])
def test_python_is_not_needed_by_the_installer(name: str) -> None:
    assert "python" not in installer_text(name).lower()
