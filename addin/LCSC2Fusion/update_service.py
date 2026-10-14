"""Schedule, state file and service of the update check (no `adsk`, standard library only).

`update_check.py` asks GitHub; this module decides WHEN to ask and what the window is told.

State file `update.json` (next to `state.json`, see `default_update_path`):

    {"version": 1, "enabled": true, "last_checked_at": 1760000000.0, "last_attempt_ok": true,
     "last_result": {"latest_tag": "v0.2.0", "newer": true, "installer": true},
     "dismissed_tag": null}

`last_checked_at` is the time of the last ATTEMPT (success or failure), `last_result` the outcome of
the last SUCCESSFUL check, so a failed attempt never forgets that a newer version exists. The file
is validated field by field on load and never raises. A missing file means the defaults (the check
is on). A file that exists but cannot be understood counts as "off": the user's choice is unknown,
and a network request is not something to guess about. The user switches it on again in Settings.

Schedule: an automatic check (`begin_check(force=False)`) runs when the window opens, only when it
is on and the last attempt is at least 24 hours old, or 6 hours after a failed one. A manual check
(`force=True`) ignores both limits but not a 60 second flood limit, counted per add-in session.

Threads: everything here runs on Fusion's main thread except `fetch_latest`, which the controller
hands to its job runner (a worker thread) and which touches nothing mutable. So the service needs no
lock. The check never sets the controller's `busy` flag (it must not make a search fail).

What the window is told (`UpdateView`, the fields are `controller.UPDATE_FIELDS`):

* `checking`: a network check is running (the knowledge fields still describe the cached result).
* `disabled`: the automatic check is off, except as the immediate result of a manual check.
* `available`: the cached result is a version newer than the running one (also after a restart);
  `dismissed` says the user hid exactly this version.
* `current`: nothing newer is known (also when nothing is known at all: `latest` is then None).
* `error`: only the immediate result of a check that just failed. The cached view never says
  "error", and the page keeps showing a banner it already has.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from update_check import (
    REASON_INTERNAL,
    STATE_AVAILABLE,
    STATE_CURRENT,
    STATE_ERROR,
    CheckResult,
    Clock,
    Fetch,
    check_latest,
    format_tag,
    format_version,
    installer_download_url,
    parse_tag,
    parse_version,
    release_page_url,
)

UPDATE_FILE_NAME = "update.json"
UPDATE_FILE_VERSION = 1

AUTO_INTERVAL_S = 24 * 60 * 60  # an automatic check at most this often
RETRY_AFTER_FAILURE_S = 6 * 60 * 60  # and not before this long after a failed attempt
FLOOD_LIMIT_S = 60.0  # two manual checks of one session at least this far apart

VIEW_AVAILABLE = "available"
VIEW_CURRENT = "current"
VIEW_ERROR = "error"
VIEW_DISABLED = "disabled"
VIEW_CHECKING = "checking"

Logger = Callable[[str], None]


@dataclass(frozen=True)
class LastResult:
    """The outcome of the last successful check: what was found, not how the user reacted."""

    latest_tag: str  # "v0.2.0"
    newer: bool  # it was newer than the running version when it was found
    installer: bool  # the release listed the installer file


@dataclass(frozen=True)
class UpdateSettings:
    """The content of update.json."""

    enabled: bool = True
    last_checked_at: float | None = None
    last_attempt_ok: bool = True
    last_result: LastResult | None = None
    dismissed_tag: str | None = None


@dataclass(frozen=True)
class UpdateView:
    """The `update` message. Field names are `controller.UPDATE_FIELDS`."""

    state: str
    current: str
    latest: str | None
    release_url: str | None
    installer_url: str | None
    checked_at: float | None
    enabled: bool
    dismissed: bool

    def to_message(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# The state file ----------------------------------------------------------------------------------


def default_update_path(
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """`update.json` in the folder of `state.json` (the rules of `library_registry` apply)."""
    from library_registry import default_state_path  # lazy: keeps this module light to import

    return default_state_path(environ, platform, home).with_name(UPDATE_FILE_NAME)


class UpdateStore:
    """The update file. `load` never raises, `save` is atomic and raises OSError on failure."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._warnings: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def warnings(self) -> tuple[str, ...]:
        """Problems found by the last `load` (empty when the file was fine or absent)."""
        return self._warnings

    def load(self) -> UpdateSettings:
        self._warnings = ()
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return UpdateSettings()
        except (OSError, UnicodeDecodeError) as exc:
            return self._unusable(f"cannot be read ({exc})")
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError) as exc:
            return self._unusable(f"is not valid JSON ({exc})")
        if not isinstance(raw, dict):
            return self._unusable("does not contain a JSON object")
        version = raw.get("version")
        if isinstance(version, bool) or version != UPDATE_FILE_VERSION:  # True == 1 in Python
            return self._unusable(f"has version {version!r}")
        problems: list[str] = []
        settings = _settings_from_json(raw, problems)
        self._warnings = tuple(f"{self._path}: {problem}" for problem in problems)
        return settings

    def save(self, settings: UpdateSettings) -> None:
        """Write the whole file atomically (temporary file in the same folder, then replace)."""
        text = json.dumps(_settings_to_json(settings), indent=2, sort_keys=True, ensure_ascii=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".update-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    def _unusable(self, reason: str) -> UpdateSettings:
        self._warnings = (
            f"The update settings {self._path} {reason}; the daily update check is off "
            "until you switch it on in Settings.",
        )
        return UpdateSettings(enabled=False)


def _settings_to_json(settings: UpdateSettings) -> dict[str, object]:
    last = settings.last_result
    return {
        "version": UPDATE_FILE_VERSION,
        "enabled": settings.enabled,
        "last_checked_at": settings.last_checked_at,
        "last_attempt_ok": settings.last_attempt_ok,
        "last_result": None if last is None else dataclasses.asdict(last),
        "dismissed_tag": settings.dismissed_tag,
    }


def _settings_from_json(raw: dict[str, Any], problems: list[str]) -> UpdateSettings:
    """Field by field: a bad field falls back to its default, the rest of the file is kept.

    The one exception is `enabled`: when it is present but not a boolean the check is switched
    off, for the reason the module docstring gives.
    """
    enabled = True
    if "enabled" in raw:
        if isinstance(raw["enabled"], bool):
            enabled = raw["enabled"]
        else:
            enabled = False
            problems.append("enabled is not true or false, so the check is off")
    checked = raw.get("last_checked_at")
    last_checked_at: float | None = None
    if checked is not None:
        if _is_timestamp(checked):
            last_checked_at = float(checked)
        else:
            problems.append("dropped the invalid last_checked_at")
    attempt_ok = raw.get("last_attempt_ok", True)
    if not isinstance(attempt_ok, bool):
        attempt_ok = True
        problems.append("dropped the invalid last_attempt_ok")
    last_result = _last_result_from_json(raw.get("last_result"), problems)
    dismissed = raw.get("dismissed_tag")
    if dismissed is not None and parse_tag(dismissed) is None:
        dismissed = None
        problems.append("dropped the invalid dismissed_tag")
    return UpdateSettings(enabled, last_checked_at, attempt_ok, last_result, dismissed)


def _is_timestamp(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _last_result_from_json(value: object, problems: list[str]) -> LastResult | None:
    if value is None:
        return None
    if isinstance(value, dict):
        tag, newer, installer = value.get("latest_tag"), value.get("newer"), value.get("installer")
        if parse_tag(tag) is not None and isinstance(newer, bool) and isinstance(installer, bool):
            return LastResult(str(tag), newer, installer)
    problems.append("dropped the invalid last_result")
    return None


# The service -------------------------------------------------------------------------------------


class UpdateService:
    """Decides when to check, runs the lookup through `update_check` and keeps update.json."""

    def __init__(
        self,
        store: UpdateStore,
        *,
        current_version: str | None = None,
        fetch: Fetch | None = None,
        now: Clock = time.time,
        log: Logger | None = None,
    ) -> None:
        if current_version is None:
            import lcsc2fusion

            current_version = lcsc2fusion.__version__
        self._store = store
        self._current_text = current_version
        self._fetch = fetch
        self._now = now
        self._log = log if log is not None else (lambda message: None)
        self._loaded: UpdateSettings | None = None
        self._in_flight = False
        self._session_attempt_at: float | None = None  # for the flood limit, not persisted

    # Reading -------------------------------------------------------------------------------

    @property
    def settings(self) -> UpdateSettings:
        if self._loaded is None:
            self._loaded = self._store.load()
            for warning in self._store.warnings:
                self._log(warning)
        return self._loaded

    def view(self) -> UpdateView:
        """What the window should know now, from the cache. Never touches the network."""
        return self._view()

    # The check -----------------------------------------------------------------------------

    def begin_check(self, force: bool) -> bool:
        """True when the caller must now run `fetch_latest` and then `finish_check`.

        False when nothing should start: a check already runs, a manual check follows the last one
        of this session by less than the flood limit, or (automatic) it is off or not yet due.
        """
        if self._in_flight:
            return False
        now = self._clock()
        if force:
            last = self._session_attempt_at
            if last is not None and 0 <= now - last < FLOOD_LIMIT_S:
                return False
        elif not self._auto_check_due(now):
            return False
        self._in_flight = True
        self._session_attempt_at = now
        return True

    def fetch_latest(self) -> CheckResult:
        """The network part. Runs on a worker thread, never raises, touches no mutable state."""
        return check_latest(self._current_text, fetch=self._fetch, now=self._now)

    def finish_check(self, result: CheckResult) -> UpdateView:
        """Record the outcome (main thread), save it and return the message for the window."""
        self._in_flight = False
        if not isinstance(result, CheckResult):
            result = self._internal_failure()
        attempted_at = result.checked_at if result.checked_at > 0 else self._clock()
        latest = parse_version(result.latest)
        if result.state not in (STATE_AVAILABLE, STATE_CURRENT) or latest is None:
            self._log(f"Update check failed: {result.reason or REASON_INTERNAL}")
            self._store_settings(
                dataclasses.replace(
                    self.settings, last_checked_at=attempted_at, last_attempt_ok=False
                )
            )
            return self._view(immediate=VIEW_ERROR)
        newer = result.state == STATE_AVAILABLE
        installer = result.update is not None and result.update.installer_url is not None
        found = LastResult(format_tag(latest), newer, installer)
        self._log(f"Update check: latest release {found.latest_tag}, running {result.current}")
        self._store_settings(
            dataclasses.replace(
                self.settings, last_checked_at=attempted_at, last_attempt_ok=True, last_result=found
            )
        )
        return self._view(immediate=STATE_AVAILABLE if newer else STATE_CURRENT)

    def abort_check(self) -> UpdateView:
        """The check could not run to its end (the worker crashed): count it as a failed attempt."""
        return self.finish_check(self._internal_failure())

    # Settings ------------------------------------------------------------------------------

    def dismiss(self, tag: str) -> UpdateView:
        """Hide the banner for the release `tag` ("v1.2.3") until a newer one appears."""
        if parse_tag(tag) is None:
            raise ValueError(f"not a release tag: {tag!r}")
        self._store_settings(dataclasses.replace(self.settings, dismissed_tag=tag))
        return self._view()

    def set_enabled(self, enabled: bool) -> UpdateView:
        self._store_settings(dataclasses.replace(self.settings, enabled=bool(enabled)))
        return self._view()

    # Internals -----------------------------------------------------------------------------

    def _internal_failure(self) -> CheckResult:
        return CheckResult(
            state=STATE_ERROR, current=self._current_text, checked_at=0.0, reason=REASON_INTERNAL
        )

    def _clock(self) -> float:
        try:
            value = float(self._now())
        except Exception:  # boundary: a broken clock must not stop the window
            return 0.0
        return value if math.isfinite(value) else 0.0

    def _auto_check_due(self, now: float) -> bool:
        settings = self.settings
        if not settings.enabled:
            return False
        if settings.last_checked_at is None:
            return True
        age = now - settings.last_checked_at
        if age < 0:  # the clock went back: the stored time cannot be trusted
            return True
        wait = AUTO_INTERVAL_S if settings.last_attempt_ok else RETRY_AFTER_FAILURE_S
        return age >= wait

    def _store_settings(self, settings: UpdateSettings) -> None:
        """Keep `settings` in memory for this session and try to save them."""
        self._loaded = settings
        try:
            self._store.save(settings)
        except OSError as exc:
            self._log(f"Could not write {self._store.path}: {exc}")

    def _view(self, *, immediate: str | None = None) -> UpdateView:
        """The message from the cache. `immediate` is the result of a check that just finished."""
        settings = self.settings
        current = parse_version(self._current_text)
        current_text = format_version(current) if current is not None else str(self._current_text)
        last = settings.last_result
        known = parse_tag(last.latest_tag) if last is not None else None
        newer = known is not None and current is not None and known > current
        latest = format_version(known) if known is not None else None
        release_url = installer_url = None
        dismissed = False
        if newer and known is not None and last is not None:
            release_url = release_page_url(known)
            installer_url = installer_download_url(known) if last.installer else None
            dismissed = settings.dismissed_tag == last.latest_tag
        state = VIEW_AVAILABLE if newer else VIEW_CURRENT
        if immediate == VIEW_ERROR:
            state = VIEW_ERROR
        elif self._in_flight:
            state = VIEW_CHECKING
        elif not settings.enabled and immediate is None:
            state = VIEW_DISABLED
            latest = release_url = installer_url = None
            dismissed = False
        return UpdateView(
            state=state,
            current=current_text,
            latest=latest,
            release_url=release_url,
            installer_url=installer_url,
            checked_at=settings.last_checked_at,
            enabled=settings.enabled,
            dismissed=dismissed,
        )
