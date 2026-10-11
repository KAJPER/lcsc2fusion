"""Shared pytest setup for the add-in tests.

`addin/LCSC2Fusion` goes on sys.path so tests can `import controller`, as Fusion does at runtime.
The Fusion API stubs are NOT part of the repository (CC BY-NC-SA). Tests that need them use the
`adsk_stubs` fixture, which reads the path from the ADSK_DEFS environment variable and falls back
to the default Fusion install location. Without stubs those tests are skipped.
"""

from __future__ import annotations

import importlib
import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"

if str(ADDIN_DIR) not in sys.path:
    sys.path.insert(0, str(ADDIN_DIR))


def adsk_defs_path() -> Path | None:
    """Folder that contains the `adsk` stub package, or None when it cannot be found."""
    candidates: list[Path] = []
    configured = os.environ.get("ADSK_DEFS")
    if configured:
        candidates.append(Path(configured))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "Autodesk" / "Autodesk Fusion 360" / "API" / "Python" / "defs"
        )
    for candidate in candidates:
        if (candidate / "adsk" / "core.py").is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def adsk_stubs() -> Iterator[Path]:
    """Make the official `adsk` stubs importable for one test module, or skip."""
    defs = adsk_defs_path()
    if defs is None:
        pytest.skip("Fusion API stubs not found (set ADSK_DEFS to the API/Python/defs folder)")
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(defs))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # invalid escapes inside the stubs
            importlib.import_module("adsk.core")
        yield defs
    for name in list(sys.modules):
        if name == "adsk" or name.startswith("adsk.") or name == "fusion_adapter":
            del sys.modules[name]
