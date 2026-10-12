"""Separate browser window for the add-in (no `adsk` import, stdlib only).

The add-in serves its page from a local HTTP server and shows it in Edge or Chrome started in
"app mode" (`--app=URL`: a window without tabs or address bar). Facts this module relies on,
measured on a real PC:

* A dedicated `--user-data-dir` makes the process returned by `Popen` the live browser process,
  even while the user's own browser is running. Without it the call hands off to the running
  browser and the launcher exits at once, so the window could be neither tracked nor closed.
* Starting a second `--app` window with the same profile opens a second window, the browser does
  not de-duplicate. `BrowserWindow.launch` therefore closes a still running window first and the
  caller decides whether to reuse a live window (`is_alive`, `focus`) instead of launching.
* When the page calls `window.close()` the process exits within about 0.5 s.
* The first start of a fresh profile can take many seconds, so `launch` never waits for the
  window to appear.
* Edge re-launches itself and the started process exits at once (code 0) when the environment
  variable `__COMPAT_LAYER` is set, which happens in some host processes (seen in a developer
  shell: `DetectorsAppHealth`). The re-launched browser is a child that nobody tracks, so the
  variable is removed from the browser's environment on Windows.

Every OS and process interaction is injectable, so tests start no browser and kill no process.
The launch URL carries a one-time token: nothing here logs it (only `host:port` is logged).
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

__all__ = [
    "APP_PATHS_SUBKEY",
    "BrowserChoice",
    "BrowserLaunchError",
    "BrowserWindow",
    "LaunchResult",
    "Win32WindowApi",
    "WindowApi",
    "build_app_command",
    "default_profile_dir",
    "find_browser",
]

MODE_APP = "app"
MODE_TAB = "tab"
BROWSER_EDGE = "edge"
BROWSER_CHROME = "chrome"
BROWSER_DEFAULT = "default"

DEFAULT_WINDOW_WIDTH = 520
DEFAULT_WINDOW_HEIGHT = 780

# Registry location of the per-application install paths (default value = full path of the exe).
APP_PATHS_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
HIVE_LOCAL_MACHINE = "HKLM"
HIVE_CURRENT_USER = "HKCU"

# Windows compatibility shim variable that makes Edge re-launch itself (see the module docstring).
COMPAT_LAYER_VARIABLE = "__COMPAT_LAYER"
# An exit sooner than this after launch means the browser handed off or re-launched itself.
EARLY_EXIT_S = 10.0
TASKKILL_TIMEOUT_S = 10.0
REAP_TIMEOUT_S = 1.0
# A token is 128 bit; shorter query values are not secrets and would garble log lines if redacted.
MIN_SECRET_LENGTH = 8

_LOOPBACK_URL = re.compile(
    r"http://127\.0\.0\.1:(?P<port>[0-9]{1,5})(?:[/?#][^\s\x00-\x1f\x7f]*)?",
    re.ASCII,
)
_ENV_REFERENCE = re.compile(r"%([^%]+)%")
_EXE_END = re.compile(r"\.exe(?=\s|$)", re.IGNORECASE)


@dataclass(frozen=True)
class BrowserChoice:
    kind: str  # "edge" or "chrome"
    path: Path


@dataclass(frozen=True)
class LaunchResult:
    mode: str  # "app" (tracked process) or "tab" (default browser tab, untracked)
    pid: int | None
    browser: str  # "edge", "chrome" or "default"


class BrowserLaunchError(RuntimeError):
    """Neither the app-mode window nor the default browser fallback could be started."""


class _Process(Protocol):
    """The slice of `subprocess.Popen` that `BrowserWindow` uses."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class WindowApi(Protocol):
    """Window operations behind `BrowserWindow.focus`; `Win32WindowApi` is the real one."""

    def top_level_windows(self, pid: int) -> Sequence[int]:
        """Handles of visible, unowned top level windows that belong to process `pid`."""
        ...

    def bring_to_front(self, hwnd: int) -> bool:
        """Restore the window when minimized and make it the foreground window."""
        ...


# Browser discovery ----------------------------------------------------------------------


@dataclass(frozen=True)
class _BrowserSpec:
    kind: str
    exe_name: str  # Windows file name, also the App Paths key
    windows_dir: tuple[str, ...]  # below a Program Files or LOCALAPPDATA root
    mac_binary: tuple[str, ...]  # below /Applications or ~/Applications


# Edge before Chrome: Edge ships with Windows 10/11.
_BROWSERS: tuple[_BrowserSpec, ...] = (
    _BrowserSpec(
        BROWSER_EDGE,
        "msedge.exe",
        ("Microsoft", "Edge", "Application"),
        ("Microsoft Edge.app", "Contents", "MacOS", "Microsoft Edge"),
    ),
    _BrowserSpec(
        BROWSER_CHROME,
        "chrome.exe",
        ("Google", "Chrome", "Application"),
        ("Google Chrome.app", "Contents", "MacOS", "Google Chrome"),
    ),
)
_WINDOWS_ROOT_VARIABLES = ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
_REGISTRY_HIVES = (HIVE_LOCAL_MACHINE, HIVE_CURRENT_USER)


def _is_windows(platform_name: str) -> bool:
    return platform_name.startswith("win")


def _is_file(path: Path) -> bool:
    return path.is_file()


def _env(environ: Mapping[str, str], name: str) -> str | None:
    """Value of an environment variable; Windows names are case insensitive."""
    value = environ.get(name)
    if value is None:
        wanted = name.lower()
        for key, candidate in environ.items():
            if key.lower() == wanted:
                value = candidate
                break
    return value or None


def _home(environ: Mapping[str, str], *, windows: bool) -> Path:
    names = ("USERPROFILE", "HOME") if windows else ("HOME",)
    for name in names:
        value = _env(environ, name)
        if value:
            return Path(value)
    return Path.home()


def _read_registry_default(hive: str, subkey: str) -> str | None:
    """Default value of `hive\\subkey` (HKLM or HKCU), None when absent or not a string."""
    if sys.platform != "win32":
        return None
    import winreg

    roots = {
        HIVE_LOCAL_MACHINE: winreg.HKEY_LOCAL_MACHINE,
        HIVE_CURRENT_USER: winreg.HKEY_CURRENT_USER,
    }
    root = roots.get(hive)
    if root is None:
        return None
    try:
        with winreg.OpenKey(root, subkey) as key:
            value, value_type = winreg.QueryValueEx(key, "")
    except OSError:  # key or value does not exist
        return None
    if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
        return value
    return None


def _existing_executable(
    raw: str, environ: Mapping[str, str], exists: Callable[[Path], bool]
) -> Path | None:
    """Turn a registry value into an existing absolute file path, or None.

    The value may be quoted, carry trailing arguments or contain `%VAR%` references.
    """
    text = _ENV_REFERENCE.sub(lambda m: _env(environ, m.group(1)) or m.group(0), raw.strip())
    candidates: list[str] = []
    if text.startswith('"'):
        end = text.find('"', 1)
        candidates.append(text[1:end] if end > 0 else text[1:])
    else:
        candidates.append(text)
        match = _EXE_END.search(text)
        if match:
            candidates.append(text[: match.end()])
    for candidate in candidates:
        candidate = candidate.strip()
        # Relative paths are rejected: they would resolve against the current directory.
        if candidate and PureWindowsPath(candidate).is_absolute():
            path = Path(candidate)
            if exists(path):
                return path
    return None


def _find_on_windows(
    spec: _BrowserSpec,
    environ: Mapping[str, str],
    read_registry: Callable[[str, str], str | None],
    exists: Callable[[Path], bool],
) -> Path | None:
    subkey = f"{APP_PATHS_SUBKEY}\\{spec.exe_name}"
    for hive in _REGISTRY_HIVES:
        try:
            raw = read_registry(hive, subkey)
        except OSError:
            raw = None
        if raw:
            path = _existing_executable(raw, environ, exists)
            if path is not None:
                return path
    for variable in _WINDOWS_ROOT_VARIABLES:
        root = _env(environ, variable)
        if root:
            path = Path(root).joinpath(*spec.windows_dir, spec.exe_name)
            if exists(path):
                return path
    return None


def find_browser(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    registry_default: Callable[[str, str], str | None] | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> BrowserChoice | None:
    """Edge, else Chrome, else None. Everything the lookup touches can be injected.

    Windows: App Paths in the registry (HKLM, then HKCU), then the standard install locations
    below `ProgramFiles(x86)`, `ProgramFiles` and `LOCALAPPDATA`. macOS: the app bundles in
    `/Applications` and `~/Applications`. Other platforms: None (the caller opens a tab).
    """
    platform = sys.platform if platform_name is None else platform_name
    env = os.environ if environ is None else environ
    file_exists = _is_file if exists is None else exists
    if _is_windows(platform):
        read_registry = _read_registry_default if registry_default is None else registry_default
        for spec in _BROWSERS:
            path = _find_on_windows(spec, env, read_registry, file_exists)
            if path is not None:
                return BrowserChoice(spec.kind, path)
        return None
    if platform == "darwin":
        for spec in _BROWSERS:
            for base in (Path("/Applications"), _home(env, windows=False) / "Applications"):
                candidate = base.joinpath(*spec.mac_binary)
                if file_exists(candidate):
                    return BrowserChoice(spec.kind, candidate)
        return None
    return None


def default_profile_dir(
    *, platform_name: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Private browser profile of the add-in (a dedicated profile is what makes the window
    process trackable, see the module docstring)."""
    platform = sys.platform if platform_name is None else platform_name
    env = os.environ if environ is None else environ
    if _is_windows(platform):
        local = _env(env, "LOCALAPPDATA")
        base = Path(local) if local else _home(env, windows=True) / "AppData" / "Local"
        return base / "LCSC2Fusion" / "browser-profile"
    home = _home(env, windows=False)
    if platform == "darwin":
        return home / "Library" / "Application Support" / "LCSC2Fusion" / "browser-profile"
    return home / ".local" / "share" / "lcsc2fusion" / "browser-profile"


# Command line ---------------------------------------------------------------------------


def _validate_loopback_url(url: str) -> None:
    """Only `http://127.0.0.1:<port>` is ever handed to a browser. The message omits the URL
    because it carries the launch token."""
    match = _LOOPBACK_URL.fullmatch(url)
    if match is None or not 1 <= int(match.group("port")) <= 65535:
        raise ValueError("The window URL must be http://127.0.0.1:<port>/... (loopback only).")


def build_app_command(
    choice: BrowserChoice,
    url: str,
    profile_dir: Path,
    *,
    width: int = DEFAULT_WINDOW_WIDTH,
    height: int = DEFAULT_WINDOW_HEIGHT,
) -> list[str]:
    """Argument list (no shell involved) for an app-mode window. Raises ValueError for a URL that
    is not `http://127.0.0.1:<port>...`."""
    _validate_loopback_url(url)
    return [
        str(choice.path),
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={int(width)},{int(height)}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


# Windows window API ---------------------------------------------------------------------


class Win32WindowApi:
    """`WindowApi` on user32 through ctypes. Raises OSError when constructed off Windows."""

    GW_OWNER = 4
    SW_RESTORE = 9

    _ctypes: Any
    _wintypes: Any
    _enum_proc: Any
    _user32: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("The Win32 window API is only available on Windows.")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32 = user32

    def top_level_windows(self, pid: int) -> list[int]:
        user32 = self._user32
        ctypes = self._ctypes
        found: list[int] = []

        def visit(hwnd: int | None, _lparam: int) -> bool:
            owner_pid = self._wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if (
                owner_pid.value == pid
                and user32.IsWindowVisible(hwnd)
                and not user32.GetWindow(hwnd, self.GW_OWNER)
                and hwnd
            ):
                found.append(int(hwnd))
            return True  # keep enumerating

        user32.EnumWindows(self._enum_proc(visit), 0)
        return found

    def bring_to_front(self, hwnd: int) -> bool:
        user32 = self._user32
        if user32.IsIconic(hwnd):  # only a minimized window is restored, a maximized one stays
            user32.ShowWindow(hwnd, self.SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))


# The window -----------------------------------------------------------------------------


def _creation_flags() -> int:
    """`CREATE_NO_WINDOW` where the platform defines it, otherwise 0."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _secrets_in(url: str) -> tuple[str, ...]:
    """Parts of the URL that must not reach a log line: the whole URL and its query values."""
    parts = urlsplit(url)
    values = [value for _, value in parse_qsl(parts.query) if len(value) >= MIN_SECRET_LENGTH]
    if len(parts.fragment) >= MIN_SECRET_LENGTH:
        values.append(parts.fragment)
    return (url, *values)


class BrowserWindow:
    """One tracked app-mode browser window. Thread safe; nothing here needs the Fusion API.

    `launch` returns at once. A window that was started in a plain tab (fallback) cannot be
    tracked: `is_alive` is False, `close` and `focus` do nothing.
    """

    def __init__(
        self,
        *,
        log: Callable[[str], None],
        finder: Callable[[], BrowserChoice | None] = find_browser,
        popen: Callable[..., _Process] = subprocess.Popen,
        open_tab: Callable[[str], object] = webbrowser.open,
        profile_dir: Path | None = None,
        run_command: Callable[..., object] = subprocess.run,
        platform_name: str | None = None,
        window_api: WindowApi | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._log = log
        self._environ = environ
        self._finder = finder
        self._popen = popen
        self._open_tab = open_tab
        self._run_command = run_command
        self._platform = sys.platform if platform_name is None else platform_name
        self._is_windows = _is_windows(self._platform)
        self._profile_dir = (
            profile_dir
            if profile_dir is not None
            else default_profile_dir(platform_name=self._platform)
        )
        self._window_api = window_api
        self._proc: _Process | None = None
        self._started_at = 0.0
        self._secrets: tuple[str, ...] = ()
        self._lock = threading.RLock()

    # Public API -------------------------------------------------------------------------

    @property
    def pid(self) -> int | None:
        """Process id of the tracked window while it runs, otherwise None."""
        with self._lock:
            return self._proc.pid if self.is_alive() and self._proc is not None else None

    def is_alive(self) -> bool:
        """True while the tracked app-mode process runs (False in tab mode or after it exited)."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            code = proc.poll()
            if code is None:
                return True
            self._proc = None
            elapsed = time.monotonic() - self._started_at
            note = ""
            if elapsed < EARLY_EXIT_S:
                note = (
                    " This is very early: the browser probably re-launched itself or handed "
                    "off to another process, so its window can no longer be tracked or closed."
                )
            self._say(
                f"The browser window process {proc.pid} exited with code {code} "
                f"after {elapsed:.1f} s.{note}"
            )
            return False

    def launch(self, url: str) -> LaunchResult:
        """Open `url` in an app-mode window, or in a default browser tab as the fallback.

        Raises ValueError for a URL that is not loopback http, and BrowserLaunchError when
        even the fallback fails. A window that is still running is closed first, because a
        second window on the same profile could not be tracked.
        """
        _validate_loopback_url(url)
        with self._lock:
            self._secrets = _secrets_in(url)
            origin = urlsplit(url).netloc
            if self.is_alive():
                self._say("A previous window is still running: closing it before launching.")
                self.close()
            choice = self._finder()
            if choice is None:
                self._say("No Edge or Chrome found: using the default browser instead.")
                return self._fallback_to_tab(url, origin)
            try:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                command = build_app_command(choice, url, self._profile_dir)
                proc = self._popen(command, **self._popen_options())
            except (OSError, ValueError) as exc:
                self._say(
                    f"Could not start {choice.kind} ({type(exc).__name__}: {exc}): "
                    "using the default browser instead."
                )
                return self._fallback_to_tab(url, origin)
            self._proc = proc
            self._started_at = time.monotonic()
            self._say(
                f"Opened {choice.kind} ({choice.path}) in app mode, pid {proc.pid}, "
                f"page {origin}, profile {self._profile_dir}."
            )
            return LaunchResult(MODE_APP, proc.pid, choice.kind)

    def close(self, *, grace_s: float = 2.0) -> None:
        """Close the tracked window. Idempotent and never raises.

        `terminate()`, wait up to `grace_s`, then (still running) `taskkill /PID <pid> /T /F`
        on Windows, `kill()` elsewhere. Normally the page already closed itself on the
        `shutdown` event and this finds the process gone.
        """
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is None:
                return
            try:
                self._stop(proc, max(0.0, grace_s))
            except Exception as exc:  # close() must never raise, the failure is only logged
                self._say(f"Closing the browser window failed: {type(exc).__name__}: {exc}")

    def focus(self) -> bool:
        """Best effort: bring the tracked window to the front. Windows only, else False.

        The windows of the tracked process id are enumerated (the app window belongs to the
        browser's main process); renderer child processes own no top level windows.
        """
        if not self._is_windows:
            return False
        pid = self.pid
        if pid is None:
            return False
        try:
            api = self._window_api
            if api is None:
                api = self._window_api = Win32WindowApi()
            handles = list(api.top_level_windows(pid))
            if not handles:
                self._say(f"No visible window found for process {pid}.")
                return False
            for hwnd in handles:
                if api.bring_to_front(hwnd):
                    self._say(f"Brought the window of process {pid} to the front.")
                    return True
            self._say(f"Windows refused to bring the window of process {pid} to the front.")
            return False
        except Exception as exc:  # ctypes failures of any kind: focus is only a convenience
            self._say(f"Focusing the window failed: {type(exc).__name__}: {exc}")
            return False

    # Internals --------------------------------------------------------------------------

    def _say(self, message: str) -> None:
        for secret in self._secrets:
            message = message.replace(secret, "<redacted>")
        with contextlib.suppress(Exception):  # a logging failure must not break the window logic
            self._log(message)

    def _popen_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if self._is_windows:
            if _creation_flags():
                options["creationflags"] = _creation_flags()
            environment = self._environment_without_compat_layer()
            if environment is not None:
                options["env"] = environment
        return options

    def _environment_without_compat_layer(self) -> dict[str, str] | None:
        """The environment for the browser without `__COMPAT_LAYER`, None when it is not set
        (the child then simply inherits the environment)."""
        source = os.environ if self._environ is None else self._environ
        removed = {k: v for k, v in source.items() if k.upper() == COMPAT_LAYER_VARIABLE}
        if not removed:
            return None
        self._say(
            f"Removed {', '.join(f'{k}={v}' for k, v in removed.items())} from the browser "
            "environment (Edge would re-launch itself and the window could not be tracked)."
        )
        return {k: v for k, v in source.items() if k not in removed}

    def _fallback_to_tab(self, url: str, origin: str) -> LaunchResult:
        self._proc = None
        try:
            opened = self._open_tab(url)
        except (webbrowser.Error, OSError) as exc:
            raise BrowserLaunchError(
                f"Could not open the default browser ({type(exc).__name__})."
            ) from exc
        if opened is False:  # webbrowser.open reports "no browser could be started" this way
            raise BrowserLaunchError("The default browser could not be opened.")
        self._say(f"Opened {origin} in a default browser tab (not tracked, cannot be closed).")
        return LaunchResult(MODE_TAB, None, BROWSER_DEFAULT)

    def _stop(self, proc: _Process, grace_s: float) -> None:
        pid = proc.pid
        if proc.poll() is not None:
            self._say(f"Browser window process {pid} had already exited.")
            return
        try:
            proc.terminate()
        except OSError as exc:  # most likely the process ended meanwhile
            self._say(f"terminate() on process {pid} failed: {exc}")
        else:
            self._say(f"Sent terminate to browser window process {pid}.")
        if self._wait(proc, grace_s):
            self._say(f"Browser window process {pid} exited.")
            return
        if proc.poll() is not None:
            self._say(f"Browser window process {pid} exited.")
            return
        self._say(f"Process {pid} still running after {grace_s:g} s: forcing it to stop.")
        if self._is_windows:
            self._taskkill(pid)
        else:
            try:
                proc.kill()
            except OSError as exc:
                self._say(f"kill() on process {pid} failed: {exc}")
            else:
                self._say(f"Sent kill to browser window process {pid}.")
        if not self._wait(proc, REAP_TIMEOUT_S):
            self._say(f"Process {pid} did not report an exit status.")

    @staticmethod
    def _wait(proc: _Process, timeout_s: float) -> bool:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _taskkill(self, pid: int) -> None:
        system_root = _env(os.environ, "SystemRoot")
        executable = (
            str(Path(system_root) / "System32" / "taskkill.exe") if system_root else "taskkill"
        )
        options: dict[str, Any] = {
            "check": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": TASKKILL_TIMEOUT_S,
        }
        if _creation_flags():
            options["creationflags"] = _creation_flags()
        command = [executable, "/PID", str(pid), "/T", "/F"]
        try:
            result = self._run_command(command, **options)
        except (OSError, subprocess.SubprocessError) as exc:
            self._say(f"taskkill for process {pid} failed: {type(exc).__name__}: {exc}")
            return
        code = getattr(result, "returncode", None)
        self._say(f"taskkill /T /F for process {pid} finished with code {code}.")
