"""How the MCP server finds the running Fusion add-in: a small file the add-in writes.

The add-in listens on a random loopback port and keeps a secret token. While it runs it stores
both in `api.json` next to its other state file, and removes the file when it stops. The MCP
server (another process, started by another Claude) reads the file to talk to the add-in. Only
the standard library is used, and neither side imports `adsk`.

File content: {"version": 1, "url": "http://127.0.0.1:<port>", "token": "<secret>", "pid": <int>}
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lcsc2fusion.errors import LcscError

API_FILE_NAME = "api.json"
API_ENV_VAR = "LCSC2FUSION_API"  # a full file path, overrides the default place (tests, odd setups)
API_VERSION = 1
_APP_DIR_NAME = "LCSC2Fusion"
_LOOPBACK_PREFIX = "http://127.0.0.1:"
_MIN_TOKEN_CHARS = 16


class DiscoveryError(LcscError):
    """The add-in file is missing or unusable. The message says what to do."""


@dataclass(frozen=True)
class ApiEndpoint:
    url: str
    token: str
    pid: int


def default_api_path(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """`%LOCALAPPDATA%\\LCSC2Fusion\\api.json` on Windows, the usual per-OS place elsewhere.

    Same directory as the add-in's `state.json`. The arguments exist so tests do not touch the
    real environment.
    """
    env = os.environ if environ is None else environ
    override = env.get(API_ENV_VAR)
    if override:
        return Path(override)
    system = sys.platform if platform is None else platform
    home_dir = Path.home() if home is None else home
    if system.startswith("win"):
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home_dir / "AppData" / "Local"
        return base / _APP_DIR_NAME / API_FILE_NAME
    if system == "darwin":
        return home_dir / "Library" / "Application Support" / _APP_DIR_NAME / API_FILE_NAME
    state_home = env.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home_dir / ".local" / "state"
    return base / _APP_DIR_NAME.lower() / API_FILE_NAME


def write_api_file(path: Path, endpoint: ApiEndpoint) -> None:
    """Write the file atomically (readers never see half of it). Raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        {
            "version": API_VERSION,
            "url": endpoint.url,
            "token": endpoint.token,
            "pid": endpoint.pid,
        }
    )
    descriptor, temp_name = tempfile.mkstemp(prefix=".api-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        with contextlib.suppress(OSError):  # not supported everywhere; the directory is per user
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def remove_api_file(path: Path, token: str) -> None:
    """Delete the file, but only when it still holds `token` (a newer add-in may own it now)."""
    with contextlib.suppress(DiscoveryError, OSError):
        if read_api_file(path).token == token:
            path.unlink()


def read_api_file(path: Path) -> ApiEndpoint:
    """The endpoint stored in `path`. Raises `DiscoveryError` with a readable message."""
    hint = "Start Fusion and run the LCSC2Fusion add-in (Shift+S, Add-Ins)."
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DiscoveryError(
            f"The Fusion add-in is not running ({path} is missing). {hint}"
        ) from None
    except OSError as exc:
        raise DiscoveryError(f"Cannot read {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except ValueError:
        raise DiscoveryError(f"{path} is not valid JSON. Restart the LCSC2Fusion add-in.") from None
    if not isinstance(value, dict) or value.get("version") != API_VERSION:
        raise DiscoveryError(f"{path} has an unsupported format. Update LCSC2Fusion on both sides.")
    url, token, pid = value.get("url"), value.get("token"), value.get("pid")
    if (
        not isinstance(url, str)
        or not url.startswith(_LOOPBACK_PREFIX)
        or not url[len(_LOOPBACK_PREFIX) :].isdigit()
        or not isinstance(token, str)
        or len(token) < _MIN_TOKEN_CHARS
        or not isinstance(pid, int)
        or isinstance(pid, bool)
    ):
        raise DiscoveryError(
            f"{path} does not hold a valid endpoint. Restart the LCSC2Fusion add-in."
        )
    return ApiEndpoint(url=url, token=token, pid=pid)
