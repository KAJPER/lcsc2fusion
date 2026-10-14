"""Helpers shared by the installer and release builder tests.

The tests run the real PowerShell scripts, always inside a sandbox: APPDATA and LOCALAPPDATA point
into a temp folder, so that nothing can reach the real `%APPDATA%\\Autodesk\\FusionAddins` of the
person who runs the tests.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_DIR = ROOT / "installer"
IS_WINDOWS = sys.platform == "win32"
WINDOWS_ONLY = pytest.mark.skipif(
    not IS_WINDOWS, reason="needs Windows: the installer is a Windows PowerShell 5.1 script"
)
POWERSHELL = (
    Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)

# Shaped like the real manifest, including the empty key of "description" that the
# ConvertFrom-Json of Windows PowerShell 5.1 cannot read.
MANIFEST = json.dumps(
    {
        "autodeskProduct": "Fusion",
        "type": "addin",
        "id": "",
        "description": {"": "Test add-in"},
        "version": "9.9.9",
        "runOnStartup": True,
    }
).encode("utf-8")
DEFAULT_PAYLOAD: dict[str, bytes] = {
    "LCSC2Fusion.manifest": MANIFEST,
    "LCSC2Fusion.py": b"def run(context):\n    pass\n",
    "palette/index.html": "<p>Zażółć gęślą jaźń</p>\n".encode(),
    "lcsc2fusion/__init__.py": b"",
    "lcsc2fusion/sub/module.py": b"X = 1\r\nY = 2\r\n",
}

_BUILD_RELEASE: ModuleType | None = None


def load_build_release() -> ModuleType:
    """scripts/ is not a package: load build_release.py by path (once per test run)."""
    global _BUILD_RELEASE
    if _BUILD_RELEASE is None:
        name = "build_release_under_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "build_release.py")
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses look the defining module up here
        spec.loader.exec_module(module)
        _BUILD_RELEASE = module
    return _BUILD_RELEASE


def make_release_root(dest: Path, payload: dict[str, bytes] | None = None) -> Path:
    """The release root layout in `dest`: installer scripts, LCSC2Fusion/ and payload.sha256."""
    build_release = load_build_release()
    files = DEFAULT_PAYLOAD if payload is None else payload
    dest.mkdir(parents=True)
    for name in build_release.INSTALLER_ROOT_FILES:
        (dest / name).write_bytes(build_release.read_installer_file(ROOT, name))
    for relative, data in files.items():
        target = dest / "LCSC2Fusion" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (dest / "payload.sha256").write_bytes(build_release.payload_checksums(files).encode("ascii"))
    return dest


def tree_bytes(folder: Path) -> dict[str, bytes]:
    """Every file below `folder` (posix relative path to bytes)."""
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True)
class Sandbox:
    """A fake user profile. The installer's default places resolve inside it."""

    root: Path

    @property
    def appdata(self) -> Path:
        return self.root / "AppData" / "Roaming"

    @property
    def localappdata(self) -> Path:
        return self.root / "AppData" / "Local"

    @property
    def target(self) -> Path:
        """A TargetRoot to pass explicitly."""
        return self.root / "FusionAddins"

    @property
    def data_dir(self) -> Path:
        """What the add-in keeps for itself: %LOCALAPPDATA%\\LCSC2Fusion."""
        return self.localappdata / "LCSC2Fusion"

    def env(self, overrides: dict[str, str | None] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("LCSC2FUSION_")}
        env["APPDATA"] = str(self.appdata)
        env["LOCALAPPDATA"] = str(self.localappdata)
        self.appdata.mkdir(parents=True, exist_ok=True)
        self.localappdata.mkdir(parents=True, exist_ok=True)
        for key, value in (overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env


def run_script(
    script: Path,
    *args: str,
    sandbox: Sandbox,
    env: dict[str, str | None] | None = None,
    stdin: str = "",
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    """Windows PowerShell 5.1 -File, the way the .bat files start it. Output is UTF-8 decoded."""
    command = [
        str(POWERSHELL),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        env=sandbox.env(env),
        timeout=timeout,
        check=False,
    )


def make_junction(link: Path, target: Path) -> None:
    """mklink /J needs no admin rights."""
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
