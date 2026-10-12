"""Tests for the add-in browser window (pure Python, no real browser, no real process).

Every OS and process interaction of `browser_window` is injected here: the registry, the
filesystem lookups, Popen, taskkill, the default browser and the Win32 window calls.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import browser_window
import pytest
from browser_window import (
    APP_PATHS_SUBKEY,
    BrowserChoice,
    BrowserLaunchError,
    BrowserWindow,
    LaunchResult,
    Win32WindowApi,
    build_app_command,
    default_profile_dir,
    find_browser,
)

TOKEN = "s3cr3t-launch-token-0123456789"
PORT = 51234
URL = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
ORIGIN = f"127.0.0.1:{PORT}"

# ----- discovery fixtures -----

PF86 = r"C:\Program Files (x86)"
PF = r"C:\Program Files"
LOCAL = r"C:\Users\u\AppData\Local"
WIN_ENV = {"ProgramFiles(x86)": PF86, "ProgramFiles": PF, "LOCALAPPDATA": LOCAL}

EDGE_PF86 = Path(PF86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
EDGE_PF = Path(PF) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
EDGE_LOCAL = Path(LOCAL) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
CHROME_PF = Path(PF) / "Google" / "Chrome" / "Application" / "chrome.exe"
CHROME_PF86 = Path(PF86) / "Google" / "Chrome" / "Application" / "chrome.exe"
CHROME_LOCAL = Path(LOCAL) / "Google" / "Chrome" / "Application" / "chrome.exe"

EDGE_KEY = APP_PATHS_SUBKEY + "\\msedge.exe"
CHROME_KEY = APP_PATHS_SUBKEY + "\\chrome.exe"


class FakeRegistry:
    def __init__(self, values: dict[tuple[str, str], str] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, hive: str, subkey: str) -> str | None:
        self.calls.append((hive, subkey))
        return self.values.get((hive, subkey))


def finder_for(
    present: set[Path],
    *,
    registry: FakeRegistry | None = None,
    platform_name: str = "win32",
    environ: dict[str, str] | None = None,
) -> BrowserChoice | None:
    return find_browser(
        platform_name=platform_name,
        environ=WIN_ENV if environ is None else environ,
        registry_default=registry or FakeRegistry(),
        exists=lambda path: path in present,
    )


# ----- find_browser -----


def test_edge_is_preferred_over_chrome() -> None:
    assert finder_for({EDGE_PF86, CHROME_PF}) == BrowserChoice("edge", EDGE_PF86)


def test_chrome_is_used_when_edge_is_missing() -> None:
    assert finder_for({CHROME_PF}) == BrowserChoice("chrome", CHROME_PF)


def test_nothing_found() -> None:
    registry = FakeRegistry()
    assert finder_for(set(), registry=registry) is None
    assert registry.calls == [
        ("HKLM", EDGE_KEY),
        ("HKCU", EDGE_KEY),
        ("HKLM", CHROME_KEY),
        ("HKCU", CHROME_KEY),
    ]


def test_registry_is_consulted_before_standard_locations() -> None:
    custom = r"D:\Apps\Edge\msedge.exe"
    registry = FakeRegistry({("HKLM", EDGE_KEY): custom})
    choice = finder_for({Path(custom), EDGE_PF86}, registry=registry)
    assert choice == BrowserChoice("edge", Path(custom))
    assert registry.calls == [("HKLM", EDGE_KEY)]  # found at once, nothing else looked up


def test_hkcu_is_the_fallback_after_hklm() -> None:
    custom = r"D:\Mine\msedge.exe"
    registry = FakeRegistry({("HKCU", EDGE_KEY): custom})
    assert finder_for({Path(custom)}, registry=registry) == BrowserChoice("edge", Path(custom))
    assert registry.calls == [("HKLM", EDGE_KEY), ("HKCU", EDGE_KEY)]


def test_registry_wins_between_hives_in_hklm_first_order() -> None:
    registry = FakeRegistry(
        {("HKLM", EDGE_KEY): r"D:\machine\msedge.exe", ("HKCU", EDGE_KEY): r"D:\user\msedge.exe"}
    )
    choice = finder_for(
        {Path(r"D:\machine\msedge.exe"), Path(r"D:\user\msedge.exe")}, registry=registry
    )
    assert choice == BrowserChoice("edge", Path(r"D:\machine\msedge.exe"))


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Edge dir\msedge.exe",
        r'"C:\Edge dir\msedge.exe"',
        r'"C:\Edge dir\msedge.exe" --profile-directory=Default',
        r"C:\Edge dir\msedge.exe --flag value",
        r'  "C:\Edge dir\msedge.exe"  ',
        r"C:\Edge dir\MSEDGE.EXE /arg",
        '"C:\\Edge dir\\msedge.exe',  # unterminated quote
        r"%EDGEHOME%\msedge.exe",
    ],
)
def test_registry_value_is_normalised(raw: str) -> None:
    present = {Path(r"C:\Edge dir\msedge.exe"), Path(r"C:\Edge dir\MSEDGE.EXE")}
    env = {**WIN_ENV, "EDGEHOME": r"C:\Edge dir"}
    registry = FakeRegistry({("HKLM", EDGE_KEY): raw})
    choice = finder_for(present, registry=registry, environ=env)
    assert choice is not None
    assert choice.kind == "edge"
    assert str(choice.path).lower() == r"c:\edge dir\msedge.exe"


def test_registry_value_pointing_to_a_missing_file_falls_through() -> None:
    registry = FakeRegistry({("HKLM", EDGE_KEY): r'"C:\Gone\msedge.exe" --x'})
    assert finder_for({EDGE_PF}, registry=registry) == BrowserChoice("edge", EDGE_PF)


@pytest.mark.parametrize("raw", ["", "   ", "msedge.exe", r"..\msedge.exe", '"', r"C:\x\other.txt"])
def test_registry_value_unusable_is_ignored(raw: str) -> None:
    registry = FakeRegistry({("HKLM", EDGE_KEY): raw})
    # a relative name must be rejected even when the file "exists" (it would hit the cwd)
    assert finder_for({Path("msedge.exe"), Path(r"..\msedge.exe")}, registry=registry) is None


def test_registry_reader_raising_oserror_is_treated_as_absent() -> None:
    def broken(hive: str, subkey: str) -> str | None:
        raise OSError("access denied")

    choice = find_browser(
        platform_name="win32",
        environ=WIN_ENV,
        registry_default=broken,
        exists=lambda path: path == CHROME_LOCAL,
    )
    assert choice == BrowserChoice("chrome", CHROME_LOCAL)


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ({EDGE_PF86, EDGE_PF, EDGE_LOCAL}, EDGE_PF86),
        ({EDGE_PF, EDGE_LOCAL}, EDGE_PF),
        ({EDGE_LOCAL}, EDGE_LOCAL),
        ({CHROME_PF86, CHROME_PF, CHROME_LOCAL}, CHROME_PF86),
        ({CHROME_PF, CHROME_LOCAL}, CHROME_PF),
        ({CHROME_LOCAL}, CHROME_LOCAL),
    ],
)
def test_standard_locations_follow_the_environment(present: set[Path], expected: Path) -> None:
    choice = finder_for(present)
    assert choice is not None
    assert choice.path == expected


def test_standard_locations_use_the_given_environment_not_the_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ProgramFiles", r"Z:\Real")
    other = {"ProgramFiles": r"Y:\Fake"}
    expected = Path(r"Y:\Fake") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    assert finder_for({expected}, environ=other) == BrowserChoice("edge", expected)


def test_environment_variable_names_are_case_insensitive() -> None:
    env = {"programfiles(x86)": PF86}
    assert finder_for({EDGE_PF86}, environ=env) == BrowserChoice("edge", EDGE_PF86)


def test_missing_environment_variables_are_skipped() -> None:
    assert (
        finder_for({Path("Microsoft") / "Edge" / "Application" / "msedge.exe"}, environ={}) is None
    )


def test_injected_callables_never_touch_the_real_system(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the real system was touched")

    monkeypatch.setitem(sys.modules, "winreg", None)  # `import winreg` would raise ImportError
    monkeypatch.setattr(Path, "is_file", boom)
    monkeypatch.setattr(Path, "exists", boom)
    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setattr(os.path, "isfile", boom)
    assert finder_for({EDGE_PF86}) == BrowserChoice("edge", EDGE_PF86)
    assert finder_for(set()) is None


EDGE_MAC = Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
CHROME_MAC = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
EDGE_MAC_USER = Path("/Users/u/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
CHROME_MAC_USER = Path("/Users/u/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ({EDGE_MAC, CHROME_MAC}, BrowserChoice("edge", EDGE_MAC)),
        ({EDGE_MAC_USER, EDGE_MAC}, BrowserChoice("edge", EDGE_MAC)),
        ({EDGE_MAC_USER, CHROME_MAC}, BrowserChoice("edge", EDGE_MAC_USER)),
        ({CHROME_MAC, CHROME_MAC_USER}, BrowserChoice("chrome", CHROME_MAC)),
        ({CHROME_MAC_USER}, BrowserChoice("chrome", CHROME_MAC_USER)),
        (set(), None),
    ],
)
def test_macos_locations(present: set[Path], expected: BrowserChoice | None) -> None:
    registry = FakeRegistry()
    choice = finder_for(
        present, registry=registry, platform_name="darwin", environ={"HOME": "/Users/u"}
    )
    assert choice == expected
    assert registry.calls == []  # the registry only exists on Windows


@pytest.mark.parametrize("platform_name", ["linux", "freebsd13", "emscripten", ""])
def test_unknown_platform_finds_nothing(platform_name: str) -> None:
    registry = FakeRegistry()
    present = {EDGE_MAC, CHROME_PF, EDGE_PF86}
    assert finder_for(present, registry=registry, platform_name=platform_name) is None
    assert registry.calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="reads the real Windows registry")
def test_real_lookup_on_windows_does_not_raise() -> None:
    choice = find_browser()
    assert choice is None or (choice.kind in ("edge", "chrome") and choice.path.is_file())


# ----- default_profile_dir -----


def test_profile_dir_windows() -> None:
    result = default_profile_dir(platform_name="win32", environ={"LOCALAPPDATA": LOCAL})
    assert result == Path(LOCAL) / "LCSC2Fusion" / "browser-profile"


def test_profile_dir_windows_without_localappdata_uses_the_profile_folder() -> None:
    result = default_profile_dir(platform_name="win32", environ={"USERPROFILE": r"C:\Users\u"})
    assert result == Path(r"C:\Users\u") / "AppData" / "Local" / "LCSC2Fusion" / "browser-profile"


def test_profile_dir_macos() -> None:
    result = default_profile_dir(platform_name="darwin", environ={"HOME": "/Users/u"})
    expected = Path("/Users/u/Library/Application Support/LCSC2Fusion/browser-profile")
    assert result == expected


@pytest.mark.parametrize("platform_name", ["linux", "freebsd13"])
def test_profile_dir_elsewhere(platform_name: str) -> None:
    result = default_profile_dir(platform_name=platform_name, environ={"HOME": "/home/u"})
    assert result == Path("/home/u/.local/share/lcsc2fusion/browser-profile")


# ----- build_app_command -----


def test_build_app_command_exact() -> None:
    choice = BrowserChoice("edge", Path(r"C:\x\msedge.exe"))
    profile = Path(r"C:\p\browser-profile")
    assert build_app_command(choice, URL, profile) == [
        str(choice.path),
        f"--app={URL}",
        f"--user-data-dir={profile}",
        "--window-size=520,780",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def test_build_app_command_custom_size() -> None:
    choice = BrowserChoice("chrome", Path("/opt/chrome"))
    command = build_app_command(choice, URL, Path("/p"), width=600, height=900)
    assert "--window-size=600,900" in command


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://127.0.0.1:5000/",
        "http://localhost:5000/",
        "http://127.0.0.1/",  # no port
        "http://127.0.0.1:/",
        "http://127.0.0.1:abc/",
        "http://127.0.0.1:99999/",
        "http://127.0.0.1:0/",
        "http://127.0.0.2:5000/",
        "http://127.0.0.1.evil.com:5000/",
        "http://127.0.0.1:5000@evil.com/",  # userinfo trick: the real host is evil.com
        "http://127.0.0.1:5000\\@evil.com/",
        "http://example.com/",
        "HTTP://127.0.0.1:5000/",
        " http://127.0.0.1:5000/",
        "http://127.0.0.1:5000/ --remote-debugging-port=9222",
        "http://127.0.0.1:5000/\n--flag",
        "file:///C:/Windows/System32/calc.exe",
        "javascript:alert(1)",
        "data:text/html,hi",
        "about:blank",
        "ftp://127.0.0.1:21/",
    ],
)
def test_build_app_command_refuses_other_urls(url: str) -> None:
    choice = BrowserChoice("edge", Path("/x/msedge"))
    with pytest.raises(ValueError, match="loopback"):
        build_app_command(choice, url, Path("/p"))


def test_the_refusal_does_not_echo_the_url() -> None:
    choice = BrowserChoice("edge", Path("/x/msedge"))
    with pytest.raises(ValueError) as excinfo:
        build_app_command(choice, f"https://evil.example/?t={TOKEN}", Path("/p"))
    assert TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1",
        "http://127.0.0.1:65535/",
        "http://127.0.0.1:8080/?t=abc",
        "http://127.0.0.1:8080/#frag",
        "http://127.0.0.1:8080?t=abc",
    ],
)
def test_build_app_command_accepts_loopback_urls(url: str) -> None:
    command = build_app_command(BrowserChoice("edge", Path("/x")), url, Path("/p"))
    assert command[1] == f"--app={url}"


# ----- BrowserWindow fakes -----


class FakeProc:
    """A `Popen` stand-in. `dies_on` is the call that ends it: "terminate", "kill" or "" (never)."""

    def __init__(
        self,
        pid: int = 4242,
        *,
        dies_on: str = "terminate",
        events: list[str] | None = None,
        wait_error: bool = False,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.dies_on = dies_on
        self.events = events if events is not None else []
        self.terminate_error: OSError | None = None
        self.kill_error: OSError | None = None
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.events.append("terminate")
        if self.terminate_error is not None:
            raise self.terminate_error
        if self.dies_on == "terminate":
            self.returncode = 1

    def kill(self) -> None:
        self.events.append("kill")
        if self.kill_error is not None:
            raise self.kill_error
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.events.append(f"wait({timeout})")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout if timeout is not None else 0.0)
        return self.returncode


class FakePopen:
    def __init__(self, proc: FakeProc | None = None, error: Exception | None = None) -> None:
        self.proc = proc if proc is not None else FakeProc()
        self.error = error
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.proc


class FakeRun:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        kills: FakeProc | None = None,
        error: Exception | None = None,
        returncode: int = 0,
    ) -> None:
        self.events = events if events is not None else []
        self.kills = kills
        self.error = error
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        self.events.append("taskkill")
        if self.error is not None:
            raise self.error
        if self.kills is not None:
            self.kills.returncode = 1
        return subprocess.CompletedProcess(args, self.returncode)


class FakeTab:
    def __init__(self, result: object = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url: str) -> object:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.result


class FakeWindowApi:
    def __init__(self, windows: list[int] | None = None, *, front_ok: bool = True) -> None:
        self.windows = windows or []
        self.front_ok = front_ok
        self.queried: list[int] = []
        self.fronted: list[int] = []

    def top_level_windows(self, pid: int) -> list[int]:
        self.queried.append(pid)
        return list(self.windows)

    def bring_to_front(self, hwnd: int) -> bool:
        self.fronted.append(hwnd)
        return self.front_ok


EDGE = BrowserChoice("edge", Path(r"C:\Edge\msedge.exe"))
CHROME = BrowserChoice("chrome", Path(r"C:\Chrome\chrome.exe"))


def make_window(
    tmp_path: Path,
    *,
    choice: BrowserChoice | None = EDGE,
    popen: FakePopen | None = None,
    tab: FakeTab | None = None,
    run: FakeRun | None = None,
    platform_name: str = "win32",
    window_api: FakeWindowApi | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[BrowserWindow, FakePopen, FakeTab, FakeRun, list[str]]:
    logs: list[str] = []
    popen = popen or FakePopen()
    tab = tab or FakeTab()
    run = run or FakeRun()
    window = BrowserWindow(
        log=logs.append,
        finder=lambda: choice,
        popen=popen,
        open_tab=tab,
        profile_dir=tmp_path / "profile" / "browser-profile",
        run_command=run,
        platform_name=platform_name,
        window_api=window_api,
        environ={"PATH": "x"} if environ is None else environ,  # never the developer's real one
    )
    return window, popen, tab, run, logs


def assert_token_not_logged(logs: list[str]) -> None:
    assert logs, "expected some log lines"
    assert not [line for line in logs if TOKEN in line or URL in line], logs


# ----- launch -----


def test_launch_in_app_mode(tmp_path: Path) -> None:
    window, popen, tab, _run, logs = make_window(tmp_path)
    profile = tmp_path / "profile" / "browser-profile"
    assert not profile.exists()

    result = window.launch(URL)

    assert result == LaunchResult(mode="app", pid=4242, browser="edge")
    assert profile.is_dir()  # created before the browser starts
    assert len(popen.calls) == 1
    args, kwargs = popen.calls[0]
    assert args == build_app_command(EDGE, URL, profile)
    assert isinstance(args, list)
    assert kwargs["shell"] is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert tab.urls == []
    assert window.is_alive()
    assert window.pid == 4242
    assert any(ORIGIN in line for line in logs)
    assert_token_not_logged(logs)


def test_launch_uses_the_browser_found(tmp_path: Path) -> None:
    window, popen, *_ = make_window(tmp_path, choice=CHROME)
    assert window.launch(URL) == LaunchResult("app", 4242, "chrome")
    assert popen.calls[0][0][0] == str(CHROME.path)


def test_launch_sets_no_window_flag_on_windows_only(tmp_path: Path) -> None:
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    window, popen, *_ = make_window(tmp_path, platform_name="win32")
    window.launch(URL)
    kwargs = popen.calls[0][1]
    if flag:
        assert kwargs["creationflags"] == flag
    else:
        assert "creationflags" not in kwargs  # not defined on this platform: never passed

    for platform_name in ("darwin", "linux"):
        window, popen, *_ = make_window(tmp_path, platform_name=platform_name)
        window.launch(URL)
        assert "creationflags" not in popen.calls[0][1]


def test_launch_does_not_wait_for_the_process(tmp_path: Path) -> None:
    window, popen, *_ = make_window(tmp_path)
    window.launch(URL)
    assert popen.proc.wait_calls == []
    assert popen.proc.events == []


def test_launch_inherits_the_environment_when_there_is_nothing_to_remove(tmp_path: Path) -> None:
    window, popen, *_ = make_window(tmp_path, environ={"PATH": "x"})
    window.launch(URL)
    assert "env" not in popen.calls[0][1]


@pytest.mark.parametrize("name", ["__COMPAT_LAYER", "__compat_layer"])
def test_launch_removes_the_compat_layer_variable_on_windows(tmp_path: Path, name: str) -> None:
    """With it set, Edge re-launches itself and the Popen pid exits at once (seen on a real PC)."""
    environ = {"PATH": "x", name: "DetectorsAppHealth", "TEMP": "t"}
    window, popen, _tab, _run, logs = make_window(tmp_path, environ=environ)
    window.launch(URL)
    assert popen.calls[0][1]["env"] == {"PATH": "x", "TEMP": "t"}
    assert environ[name] == "DetectorsAppHealth"  # the caller's mapping is not modified
    assert any("DetectorsAppHealth" in line for line in logs)
    assert_token_not_logged(logs)


def test_launch_leaves_the_environment_alone_off_windows(tmp_path: Path) -> None:
    environ = {"PATH": "x", "__COMPAT_LAYER": "Whatever"}
    window, popen, *_ = make_window(tmp_path, platform_name="darwin", environ=environ)
    window.launch(URL)
    assert "env" not in popen.calls[0][1]


def test_launch_falls_back_to_a_tab_when_no_browser_is_found(tmp_path: Path) -> None:
    window, popen, tab, _run, logs = make_window(tmp_path, choice=None)
    result = window.launch(URL)
    assert result == LaunchResult(mode="tab", pid=None, browser="default")
    assert tab.urls == [URL]
    assert popen.calls == []
    assert not (tmp_path / "profile").exists()
    assert any("No Edge or Chrome" in line for line in logs)
    assert any(ORIGIN in line for line in logs)
    assert_token_not_logged(logs)


def test_launch_falls_back_to_a_tab_on_oserror(tmp_path: Path) -> None:
    popen = FakePopen(error=FileNotFoundError(2, "not found", r"C:\Edge\msedge.exe"))
    window, _popen, tab, _run, logs = make_window(tmp_path, popen=popen)
    result = window.launch(URL)
    assert result == LaunchResult("tab", None, "default")
    assert tab.urls == [URL]
    assert not window.is_alive()
    assert any("Could not start edge" in line and "FileNotFoundError" in line for line in logs)
    assert_token_not_logged(logs)


def test_launch_falls_back_when_the_profile_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "profile"
    blocker.write_text("a file where the profile folder should go")
    window, popen, tab, _run, logs = make_window(tmp_path)
    assert window.launch(URL).mode == "tab"
    assert popen.calls == []
    assert tab.urls == [URL]
    assert_token_not_logged(logs)


def test_launch_redacts_the_token_from_error_messages(tmp_path: Path) -> None:
    popen = FakePopen(error=OSError(f"boom with {URL} and {TOKEN}"))
    window, _popen, _tab, _run, logs = make_window(tmp_path, popen=popen)
    window.launch(URL)
    assert any("<redacted>" in line for line in logs)
    assert_token_not_logged(logs)


@pytest.mark.parametrize(
    "tab",
    [
        FakeTab(result=False),
        FakeTab(error=webbrowser.Error("no runnable browser")),
        FakeTab(error=OSError("nope")),
    ],
)
def test_launch_raises_when_even_the_tab_fallback_fails(tmp_path: Path, tab: FakeTab) -> None:
    window, *_ = make_window(tmp_path, choice=None, tab=tab)
    with pytest.raises(BrowserLaunchError):
        window.launch(URL)
    assert not window.is_alive()
    assert window.pid is None


def test_a_tab_open_returning_none_counts_as_success(tmp_path: Path) -> None:
    window, *_ = make_window(tmp_path, choice=None, tab=FakeTab(result=None))
    assert window.launch(URL).mode == "tab"


@pytest.mark.parametrize(
    "url",
    ["", "https://127.0.0.1:1/", "http://evil.example/", "file:///etc/passwd", "javascript:1"],
)
def test_launch_refuses_non_loopback_urls_on_both_paths(tmp_path: Path, url: str) -> None:
    for choice in (EDGE, None):  # app mode and tab fallback both validate
        window, popen, tab, _run, _logs = make_window(tmp_path, choice=choice)
        with pytest.raises(ValueError, match="loopback"):
            window.launch(url)
        assert popen.calls == []
        assert tab.urls == []


def test_launch_closes_a_window_that_is_still_running(tmp_path: Path) -> None:
    events: list[str] = []
    first = FakeProc(pid=100, events=events)
    second = FakeProc(pid=200, events=events)
    queue = [first, second]
    popen_calls: list[list[str]] = []

    def popen(args: list[str], **kwargs: Any) -> FakeProc:
        popen_calls.append(args)
        events.append(f"popen({queue[0].pid})")
        return queue.pop(0)

    logs: list[str] = []
    window = BrowserWindow(
        log=logs.append,
        finder=lambda: EDGE,
        popen=popen,
        open_tab=FakeTab(),
        profile_dir=tmp_path / "p",
        run_command=FakeRun(),
        platform_name="win32",
    )
    window.launch(URL)
    result = window.launch(URL)

    assert result.pid == 200
    assert window.pid == 200
    assert events == ["popen(100)", "terminate", "wait(2.0)", "popen(200)"]
    assert any("previous window" in line for line in logs)


def test_launch_after_the_previous_window_exited_just_starts_a_new_one(tmp_path: Path) -> None:
    first = FakeProc(pid=100)
    window, popen, *_ = make_window(tmp_path, popen=FakePopen(first))
    window.launch(URL)
    first.returncode = 0  # the page closed itself
    popen.proc = FakeProc(pid=200)
    assert window.launch(URL).pid == 200
    assert first.events == []  # nothing was terminated


# ----- is_alive / pid -----


def test_is_alive_transitions(tmp_path: Path) -> None:
    window, popen, _tab, _run, logs = make_window(tmp_path)
    assert not window.is_alive()
    assert window.pid is None

    window.launch(URL)
    assert window.is_alive()
    assert window.pid == 4242

    popen.proc.returncode = 0  # page called window.close()
    assert not window.is_alive()
    assert window.pid is None
    assert not window.is_alive()
    assert sum("exited with code 0" in line for line in logs) == 1  # logged once


def test_a_very_early_exit_is_explained_in_the_log(tmp_path: Path) -> None:
    window, popen, _tab, _run, logs = make_window(tmp_path)
    window.launch(URL)
    popen.proc.returncode = 0  # the launcher handed off and left within a moment
    assert not window.is_alive()
    line = next(line for line in logs if "exited with code 0" in line)
    assert "re-launched itself or handed off" in line
    assert_token_not_logged(logs)


def test_tab_mode_is_never_alive(tmp_path: Path) -> None:
    window, *_ = make_window(tmp_path, choice=None)
    window.launch(URL)
    assert not window.is_alive()
    assert window.pid is None
    window.close()  # no-op
    assert window.focus() is False


# ----- close -----


def test_close_after_terminate_without_taskkill(tmp_path: Path) -> None:
    events: list[str] = []
    proc = FakeProc(dies_on="terminate", events=events)
    window, _popen, _tab, run, logs = make_window(
        tmp_path, popen=FakePopen(proc), run=FakeRun(events)
    )
    window.launch(URL)
    window.close()
    assert events == ["terminate", "wait(2.0)"]
    assert run.calls == []
    assert not window.is_alive()
    assert any("terminate" in line for line in logs)
    assert_token_not_logged(logs)


def test_close_uses_taskkill_after_the_grace_period_on_windows(tmp_path: Path) -> None:
    events: list[str] = []
    proc = FakeProc(dies_on="", events=events)  # ignores terminate()
    run = FakeRun(events, kills=proc)
    window, _popen, _tab, _run, logs = make_window(tmp_path, popen=FakePopen(proc), run=run)
    window.launch(URL)

    window.close(grace_s=0.5)

    assert events == ["terminate", "wait(0.5)", "taskkill", "wait(1.0)"]
    command, kwargs = run.calls[0]
    assert "taskkill" in command[0].lower()
    assert command[1:] == ["/PID", "4242", "/T", "/F"]
    assert isinstance(command, list)
    assert kwargs["check"] is False
    assert kwargs.get("shell", False) is False
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    if getattr(subprocess, "CREATE_NO_WINDOW", 0):
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined,unused-ignore]
    assert not window.is_alive()
    assert any("taskkill" in line for line in logs)
    assert_token_not_logged(logs)


def test_close_skips_taskkill_when_the_process_exits_right_after_the_wait(
    tmp_path: Path,
) -> None:
    proc = FakeProc(dies_on="")

    def racing_wait(timeout: float | None = None) -> int:
        proc.wait_calls.append(timeout)
        proc.returncode = 0  # the process ends just after the wait gave up
        raise subprocess.TimeoutExpired("fake", timeout or 0.0)

    proc.wait = racing_wait  # type: ignore[method-assign]
    run = FakeRun()
    window, *_ = make_window(tmp_path, popen=FakePopen(proc), run=run)
    window.launch(URL)
    window.close()
    assert run.calls == []
    assert not window.is_alive()


def test_close_kills_on_other_platforms(tmp_path: Path) -> None:
    for platform_name in ("darwin", "linux"):
        events: list[str] = []
        proc = FakeProc(dies_on="kill", events=events)
        run = FakeRun(events)
        window, *_ = make_window(
            tmp_path, popen=FakePopen(proc), run=run, platform_name=platform_name
        )
        window.launch(URL)
        window.close(grace_s=1.5)
        assert events == ["terminate", "wait(1.5)", "kill", "wait(1.0)"]
        assert run.calls == []  # no taskkill off Windows


def test_close_is_idempotent(tmp_path: Path) -> None:
    events: list[str] = []
    proc = FakeProc(events=events)
    window, *_ = make_window(tmp_path, popen=FakePopen(proc))
    window.launch(URL)
    window.close()
    first = list(events)
    window.close()
    window.close(grace_s=0)
    assert events == first


def test_close_before_launch_is_a_noop(tmp_path: Path) -> None:
    window, _popen, _tab, run, logs = make_window(tmp_path)
    window.close()
    assert run.calls == []
    assert logs == []


def test_close_when_the_process_already_exited(tmp_path: Path) -> None:
    proc = FakeProc()
    window, _popen, _tab, run, logs = make_window(tmp_path, popen=FakePopen(proc))
    window.launch(URL)
    proc.returncode = 0
    window.close()
    assert proc.events == []  # nothing to terminate
    assert run.calls == []
    assert not window.is_alive()
    assert_token_not_logged(logs)


def test_close_when_terminate_raises_because_the_process_is_gone(tmp_path: Path) -> None:
    events: list[str] = []
    proc = FakeProc(dies_on="", events=events)
    proc.terminate_error = PermissionError("access denied")
    run = FakeRun(events, kills=proc)
    window, *_ = make_window(tmp_path, popen=FakePopen(proc), run=run)
    window.launch(URL)
    window.close(grace_s=0)  # must not raise
    assert "taskkill" in events  # still escalated: the process did not exit
    assert not window.is_alive()


@pytest.mark.parametrize(
    "error",
    [
        OSError("taskkill not found"),
        subprocess.TimeoutExpired("taskkill", 10),
        subprocess.SubprocessError("odd"),
    ],
)
def test_close_never_raises_when_taskkill_fails(tmp_path: Path, error: Exception) -> None:
    proc = FakeProc(dies_on="")
    run = FakeRun(error=error)
    window, _popen, _tab, _run, logs = make_window(tmp_path, popen=FakePopen(proc), run=run)
    window.launch(URL)
    window.close(grace_s=0)
    assert len(run.calls) == 1
    assert not window.is_alive()
    assert any("taskkill" in line and "failed" in line for line in logs)
    assert_token_not_logged(logs)


def test_close_reports_a_nonzero_taskkill_code(tmp_path: Path) -> None:
    proc = FakeProc(dies_on="")
    run = FakeRun(returncode=128)
    window, _popen, _tab, _run, logs = make_window(tmp_path, popen=FakePopen(proc), run=run)
    window.launch(URL)
    window.close(grace_s=0)
    assert any("code 128" in line for line in logs)


def test_close_never_raises_when_kill_fails(tmp_path: Path) -> None:
    proc = FakeProc(dies_on="")
    proc.kill_error = ProcessLookupError("gone")
    window, *_ = make_window(tmp_path, popen=FakePopen(proc), platform_name="linux")
    window.launch(URL)
    window.close(grace_s=0)
    assert not window.is_alive()


def test_close_never_raises_on_unexpected_errors(tmp_path: Path) -> None:
    class Exploding(FakeProc):
        def terminate(self) -> None:
            raise RuntimeError("surprise")

    window, _popen, _tab, _run, logs = make_window(tmp_path, popen=FakePopen(Exploding()))
    window.launch(URL)
    window.close()
    assert any("Closing the browser window failed" in line for line in logs)
    assert not window.is_alive()


def test_close_negative_grace_is_clamped(tmp_path: Path) -> None:
    proc = FakeProc()
    window, *_ = make_window(tmp_path, popen=FakePopen(proc))
    window.launch(URL)
    window.close(grace_s=-5)
    assert proc.wait_calls == [0.0]


def test_a_failing_logger_does_not_break_the_window(tmp_path: Path) -> None:
    def broken_log(message: str) -> None:
        raise RuntimeError("log is gone")

    proc = FakeProc()
    window = BrowserWindow(
        log=broken_log,
        finder=lambda: EDGE,
        popen=FakePopen(proc),
        open_tab=FakeTab(),
        profile_dir=tmp_path / "p",
        run_command=FakeRun(),
        platform_name="win32",
    )
    assert window.launch(URL).mode == "app"
    window.close()
    assert not window.is_alive()


# ----- focus -----


def test_focus_brings_the_window_to_the_front(tmp_path: Path) -> None:
    api = FakeWindowApi([111, 222])
    window, _popen, _tab, _run, logs = make_window(tmp_path, window_api=api)
    window.launch(URL)
    assert window.focus() is True
    assert api.queried == [4242]
    assert api.fronted == [111]  # stops at the first window that works
    assert_token_not_logged(logs)


def test_focus_tries_the_next_window_when_one_is_refused(tmp_path: Path) -> None:
    class PickyApi(FakeWindowApi):
        def bring_to_front(self, hwnd: int) -> bool:
            self.fronted.append(hwnd)
            return hwnd == 222

    api = PickyApi([111, 222, 333])
    window, *_ = make_window(tmp_path, window_api=api)
    window.launch(URL)
    assert window.focus() is True
    assert api.fronted == [111, 222]


def test_focus_without_a_window_of_that_process(tmp_path: Path) -> None:
    api = FakeWindowApi([])
    window, _popen, _tab, _run, logs = make_window(tmp_path, window_api=api)
    window.launch(URL)
    assert window.focus() is False
    assert api.fronted == []
    assert any("No visible window" in line for line in logs)


def test_focus_when_windows_refuses_the_foreground(tmp_path: Path) -> None:
    api = FakeWindowApi([5], front_ok=False)
    window, *_ = make_window(tmp_path, window_api=api)
    window.launch(URL)
    assert window.focus() is False
    assert api.fronted == [5]


def test_focus_without_a_tracked_process(tmp_path: Path) -> None:
    api = FakeWindowApi([1])
    window, _popen, *_ = make_window(tmp_path, window_api=api)
    assert window.focus() is False  # never launched
    window.launch(URL)
    window.close()
    assert window.focus() is False  # closed
    assert api.queried == []


def test_focus_after_the_process_exited(tmp_path: Path) -> None:
    api = FakeWindowApi([1])
    window, popen, *_ = make_window(tmp_path, window_api=api)
    window.launch(URL)
    popen.proc.returncode = 0
    assert window.focus() is False
    assert api.queried == []  # a stale pid must never be used


@pytest.mark.parametrize("platform_name", ["darwin", "linux"])
def test_focus_is_unavailable_off_windows(tmp_path: Path, platform_name: str) -> None:
    api = FakeWindowApi([1])
    window, *_ = make_window(tmp_path, platform_name=platform_name, window_api=api)
    window.launch(URL)
    assert window.focus() is False
    assert api.queried == []


def test_focus_never_raises(tmp_path: Path) -> None:
    class BrokenApi(FakeWindowApi):
        def top_level_windows(self, pid: int) -> list[int]:
            raise OSError("ctypes exploded")

    window, _popen, _tab, _run, logs = make_window(tmp_path, window_api=BrokenApi())
    window.launch(URL)
    assert window.focus() is False
    assert any("Focusing the window failed" in line for line in logs)


@pytest.mark.skipif(sys.platform != "win32", reason="user32 only exists on Windows")
def test_real_win32_api_enumerates_without_error() -> None:
    api = Win32WindowApi()
    # this process owns no visible top level window under pytest; the point is that the
    # ctypes signatures work and nothing is raised or left in a bad state
    assert isinstance(api.top_level_windows(os.getpid()), list)
    assert api.top_level_windows(0xFFFFFFF0) == []  # no such process
    assert api.bring_to_front(0) is False  # no such window


@pytest.mark.skipif(sys.platform == "win32", reason="checks the off-Windows refusal")
def test_win32_api_refuses_to_load_off_windows() -> None:
    with pytest.raises(OSError, match="Windows"):
        Win32WindowApi()


# ----- module hygiene -----

MODULE_PATH = Path(browser_window.__file__)


def _imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_module_imports_only_the_standard_library() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = _imported_modules(tree)
    assert "adsk" not in imported
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


def test_module_has_no_top_level_side_effects() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.FunctionDef,
        ast.Assign,
        ast.AnnAssign,
    )
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.Expr):
            assert index == 0 and isinstance(node.value, ast.Constant), ast.dump(node)  # docstring
        else:
            assert isinstance(node, allowed), ast.dump(node)


def test_importing_the_module_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load a private copy with every process, browser and registry entry point booby-trapped."""

    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the import touched the system")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(os, "makedirs", boom)
    monkeypatch.setattr(os, "startfile", boom, raising=False)
    monkeypatch.setattr(webbrowser, "open", boom)
    monkeypatch.setitem(sys.modules, "winreg", None)
    spec = importlib.util.spec_from_file_location("browser_window_private_copy", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    assert "adsk" not in sys.modules
    assert module.BrowserWindow is not browser_window.BrowserWindow  # really a separate copy


def test_log_callable_contract(tmp_path: Path) -> None:
    """The log callable receives plain strings only."""
    seen: list[object] = []
    log: Callable[[str], None] = seen.append
    window = BrowserWindow(
        log=log,
        finder=lambda: EDGE,
        popen=FakePopen(),
        open_tab=FakeTab(),
        profile_dir=tmp_path / "p",
        run_command=FakeRun(),
        platform_name="win32",
    )
    window.launch(URL)
    window.close()
    assert seen
    assert all(isinstance(line, str) for line in seen)
