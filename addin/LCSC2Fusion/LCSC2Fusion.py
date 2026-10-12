"""Autodesk Fusion add-in entry point (folder, .py and .manifest share the name LCSC2Fusion).

Fusion calls `run(context)` when the add-in starts and `stop(context)` when it ends. Everything
else lives in sibling modules; this file only prepares `sys.path`, builds the UI through
`fusion_adapter` and reports failures. When Fusion starts the add-in at application startup
(`runOnStartup`) only the toolbar buttons and the local server are set up, no window is shown.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

ADDIN_DIR = Path(__file__).resolve().parent

# Modules of this folder that stop() unloads, so that stop followed by run picks up edited sources.
ADDIN_MODULES = (
    "fusion_adapter",
    "fusion_library_flow",
    "library_registry",
    "core_service",
    "controller",
    "http_transport",
    "local_api",
    "browser_window",
)

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
        if name in ADDIN_MODULES or name.startswith("lcsc2fusion"):
            del sys.modules[name]


def should_show_window(context: Any) -> bool:
    """False when Fusion started the add-in at application startup (run on startup).

    `context` is a JSON string such as `{"IsApplicationStartup": true}`. Anything unexpected
    (no JSON, other types, a missing key) counts as a manual run: the window is shown.
    """
    if isinstance(context, (str, bytes, bytearray)):
        try:
            context = json.loads(context)
        except ValueError:
            return True
    if not isinstance(context, dict):
        return True
    startup = context.get("IsApplicationStartup", False)
    if isinstance(startup, str):
        startup = startup.strip().lower() == "true"
    return startup is not True


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
        _app.start(should_show_window(context))
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
