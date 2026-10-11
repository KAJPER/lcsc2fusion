"""Offline tests for DiskCache and default_cache_dir."""

from __future__ import annotations

import os
from pathlib import Path

from lcsc2fusion.client.cache import CACHE_ENV_VAR, DiskCache, default_cache_dir


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_put_then_get_round_trip(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    assert cache.get("C2040") is None
    cache.put("C2040", b'{"x": 1}')
    assert cache.get("C2040") == b'{"x": 1}'


def test_put_creates_missing_directories(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path / "a" / "b" / "c", ttl_s=60)
    cache.put("k", b"v")
    assert cache.get("k") == b"v"


def test_put_overwrites_and_leaves_no_temp_files(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    cache.put("k", b"one")
    cache.put("k", b"two")
    assert cache.get("k") == b"two"
    names = [p.name for p in tmp_path.iterdir()]
    assert len(names) == 1
    assert names[0].endswith(".cache")


def test_entry_expires_after_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = DiskCache(tmp_path, ttl_s=100, clock=clock)
    cache.put("k", b"v")
    mtime = cache.path_for("k").stat().st_mtime
    clock.now = mtime + 99
    assert cache.get("k") == b"v"
    clock.now = mtime + 101
    assert cache.get("k") is None


def test_ttl_override_per_lookup(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = DiskCache(tmp_path, ttl_s=1000, clock=clock)
    cache.put("k", b"v")
    clock.now = cache.path_for("k").stat().st_mtime + 500
    assert cache.get("k") == b"v"
    assert cache.get("k", ttl_s=100) is None
    assert cache.get("k", ttl_s=600) == b"v"


def test_non_positive_ttl_disables_reads(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=0)
    cache.put("k", b"v")
    assert cache.get("k") is None
    assert cache.get("k", ttl_s=60) == b"v"


def test_expiry_follows_file_mtime(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=3600)
    cache.put("k", b"v")
    old = cache.path_for("k").stat().st_mtime - 7200
    os.utime(cache.path_for("k"), (old, old))
    assert cache.get("k") is None


def test_get_from_missing_directory_returns_none(tmp_path: Path) -> None:
    assert DiskCache(tmp_path / "does" / "not" / "exist", ttl_s=60).get("k") is None


def test_directory_path_is_a_file_is_harmless(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    cache = DiskCache(blocker, ttl_s=60)
    cache.put("k", b"v")  # must not raise
    assert cache.get("k") is None


def test_entry_path_is_a_directory_is_harmless(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    cache.path_for("k").mkdir()
    assert cache.get("k") is None
    cache.put("k", b"v")  # cannot replace a directory, must not raise
    assert cache.get("k") is None
    assert [p.name for p in tmp_path.iterdir()] == [cache.path_for("k").name]


def test_garbage_content_is_returned_verbatim_for_the_caller_to_judge(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    cache.path_for("k").write_bytes(b"\x00\xff not json")
    assert cache.get("k") == b"\x00\xff not json"


def test_discard_removes_entry_and_tolerates_absence(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    cache.put("k", b"v")
    cache.discard("k")
    assert cache.get("k") is None
    cache.discard("k")


def test_keys_are_sanitised_into_safe_filenames(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    nasty = [
        "../../etc/passwd",
        'a/b\\c:d*e?f"g<h>i|j',
        "CON",
        "",
        ".hidden",
        "zażółć gęślą jaźń 100nF 0603",
        "x" * 500,
    ]
    for key in nasty:
        cache.put(key, key.encode("utf-8"))
        path = cache.path_for(key)
        assert path.parent == tmp_path
        assert len(path.name) < 120
        assert not set('/\\:*?"<>|').intersection(path.name)
        assert not path.name.startswith(".")
        assert cache.get(key) == key.encode("utf-8")
    assert len(list(tmp_path.iterdir())) == len(nasty)


def test_keys_that_sanitise_alike_do_not_collide(tmp_path: Path) -> None:
    cache = DiskCache(tmp_path, ttl_s=60)
    cache.put("a/b", b"slash")
    cache.put("a_b", b"underscore")
    assert cache.get("a/b") == b"slash"
    assert cache.get("a_b") == b"underscore"


def test_readable_prefix_is_kept_for_debugging(tmp_path: Path) -> None:
    assert DiskCache(tmp_path, ttl_s=1).path_for("easyeda-C2040").name.startswith("easyeda-C2040-")


# default_cache_dir


def test_env_override_wins_everywhere(tmp_path: Path) -> None:
    env = {CACHE_ENV_VAR: str(tmp_path / "custom"), "LOCALAPPDATA": "C:/ignored"}
    for platform in ("win32", "darwin", "linux"):
        assert default_cache_dir(env, platform, tmp_path) == tmp_path / "custom"


def test_windows_uses_localappdata(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "Local")}
    assert default_cache_dir(env, "win32", tmp_path) == tmp_path / "Local" / "lcsc2fusion" / "cache"


def test_windows_without_localappdata_falls_back_to_home(tmp_path: Path) -> None:
    expected = tmp_path / "AppData" / "Local" / "lcsc2fusion" / "cache"
    assert default_cache_dir({}, "win32", tmp_path) == expected


def test_macos_uses_library_caches(tmp_path: Path) -> None:
    expected = tmp_path / "Library" / "Caches" / "lcsc2fusion"
    assert default_cache_dir({}, "darwin", tmp_path) == expected


def test_other_platforms_use_dot_cache(tmp_path: Path) -> None:
    assert default_cache_dir({}, "linux", tmp_path) == tmp_path / ".cache" / "lcsc2fusion"


def test_empty_env_override_is_ignored(tmp_path: Path) -> None:
    env = {CACHE_ENV_VAR: ""}
    assert default_cache_dir(env, "linux", tmp_path) == tmp_path / ".cache" / "lcsc2fusion"
