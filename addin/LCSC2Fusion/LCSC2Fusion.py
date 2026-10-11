"""Autodesk Fusion add-in entry point (folder, .py and .manifest share the name LCSC2Fusion).

Fusion calls `run(context)` when the add-in starts and `stop(context)` when it ends. Everything
else lives in sibling modules; this file only prepares `sys.path`, builds the UI through
`fusion_adapter` and reports failures.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any

ADDIN_DIR = Path(__file__).resolve().parent

_app: Any = None  # fusion_adapter.AddinApp while the add-in runs


def _prepare_import_path() -> None:
    """Make the add-in modules and the `lcsc2fusion` core package importable.

    A built add-in carries its own copy of the core in `<addin>/lcsc2fusion/`. Without it (running
    straight from the repository) the repository root, two levels above this folder, is used.
    """
    paths = [ADDIN_DIR]
    if not (ADDIN_DIR / "lcsc2fusion").is_dir():
        repo_root = ADDIN_DIR.parents[1]
        if (repo_root / "lcsc2fusion").is_dir():
            paths.append(repo_root)
    for path in paths:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def _forget_modules() -> None:
    """Drop our modules so that stop followed by run picks up edited sources."""
    for name in list(sys.modules):
        if name in ("fusion_adapter", "core_service", "controller") or name.startswith(
            "lcsc2fusion"
        ):
            del sys.modules[name]


def _report(where: str) -> bool:
    """Log and show the active exception. False when the adapter itself cannot be imported."""
    details = traceback.format_exc()
    try:
        import fusion_adapter
    except ImportError:
        return False
    fusion_adapter.report_error(f"LCSC2Fusion failed to {where}", details)
    return True


def run(context: Any) -> None:
    global _app
    try:
        _prepare_import_path()
        import fusion_adapter

        _app = fusion_adapter.AddinApp(ADDIN_DIR)
        _app.start()
    except Exception:  # boundary with Fusion: report with a traceback instead of failing silently
        reported = _report("start")
        if _app is not None:
            _app.stop()  # remove whatever was created before the failure
            _app = None
        if not reported:
            raise


def stop(context: Any) -> None:
    global _app
    try:
        if _app is not None:
            _app.stop()
    except Exception:  # boundary with Fusion, see run()
        if not _report("stop"):
            raise
    finally:
        _app = None
        _forget_modules()
