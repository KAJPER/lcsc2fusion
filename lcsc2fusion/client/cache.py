"""Small on-disk cache with a time to live, one file per key."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

CACHE_ENV_VAR = "LCSC2FUSION_CACHE"
_APP_DIR = "lcsc2fusion"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_STEM_LEN = 80


def default_cache_dir(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Pick the cache directory: env override, else the per-OS conventional location.

    The arguments exist so tests can run without touching the real environment.
    """
    env = os.environ if environ is None else environ
    override = env.get(CACHE_ENV_VAR)
    if override:
        return Path(override)
    system = sys.platform if platform is None else platform
    home_dir = Path.home() if home is None else home
    if system.startswith("win"):
        local = env.get("LOCALAPPDATA")
        base = Path(local) if local else home_dir / "AppData" / "Local"
        return base / _APP_DIR / "cache"
    if system == "darwin":
        return home_dir / "Library" / "Caches" / _APP_DIR
    return home_dir / ".cache" / _APP_DIR


def _filename(key: str) -> str:
    """Map an arbitrary key to a safe, collision free file name."""
    stem = _UNSAFE_CHARS.sub("_", key)[:_MAX_STEM_LEN].lstrip(".") or "key"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{stem}-{digest}.cache"


class DiskCache:
    """Key to bytes cache. Freshness is judged by the file modification time.

    Every operation is best effort: a missing, unreadable or corrupt cache directory never raises,
    it just behaves like an empty cache. `ttl_s <= 0` means entries are never considered fresh.
    """

    def __init__(
        self,
        directory: Path | str,
        ttl_s: float,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._directory = Path(directory)
        self._ttl_s = ttl_s
        self._clock = clock

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    def path_for(self, key: str) -> Path:
        return self._directory / _filename(key)

    def get(self, key: str, *, ttl_s: float | None = None) -> bytes | None:
        """Return the cached bytes, or None when missing, expired or unreadable.

        `ttl_s` overrides the default time to live for this lookup only.
        """
        limit = self._ttl_s if ttl_s is None else ttl_s
        if limit <= 0:
            return None
        path = self.path_for(key)
        try:
            age = self._clock() - path.stat().st_mtime
            if age > limit:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> None:
        """Store bytes atomically (temp file in the same directory, then `os.replace`)."""
        temp_name: str | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=self._directory, prefix=".tmp-", suffix=".part")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, self.path_for(key))
            temp_name = None
        except OSError:
            # A cache that cannot be written is not worth failing the request for.
            pass
        finally:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)

    def discard(self, key: str) -> None:
        """Remove one entry if present (used to drop corrupt content)."""
        with contextlib.suppress(OSError):
            self.path_for(key).unlink()
