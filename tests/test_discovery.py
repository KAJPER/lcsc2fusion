import json
from pathlib import Path

import pytest

from lcsc2fusion.discovery import (
    API_ENV_VAR,
    ApiEndpoint,
    DiscoveryError,
    default_api_path,
    read_api_file,
    remove_api_file,
    write_api_file,
)

ENDPOINT = ApiEndpoint(url="http://127.0.0.1:51234", token="t" * 32, pid=4242)


def test_default_path_on_each_platform(tmp_path: Path) -> None:
    win = default_api_path({"LOCALAPPDATA": str(tmp_path)}, "win32", tmp_path)
    assert win == tmp_path / "LCSC2Fusion" / "api.json"
    mac = default_api_path({}, "darwin", tmp_path)
    assert mac == tmp_path / "Library" / "Application Support" / "LCSC2Fusion" / "api.json"
    linux = default_api_path({}, "linux", tmp_path)
    assert linux == tmp_path / ".local" / "state" / "lcsc2fusion" / "api.json"


def test_default_path_override(tmp_path: Path) -> None:
    target = tmp_path / "x" / "my.json"
    assert default_api_path({API_ENV_VAR: str(target)}, "win32", tmp_path) == target


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "api.json"
    write_api_file(path, ENDPOINT)
    assert read_api_file(path) == ENDPOINT
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind


def test_missing_file_says_to_start_the_add_in(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="not running"):
        read_api_file(tmp_path / "api.json")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"version": 2, "url": "http://127.0.0.1:1", "token": "t" * 32, "pid": 1}),
        json.dumps({"version": 1, "url": "http://example.com:1", "token": "t" * 32, "pid": 1}),
        json.dumps({"version": 1, "url": "http://127.0.0.1:abc", "token": "t" * 32, "pid": 1}),
        json.dumps({"version": 1, "url": "http://127.0.0.1:1", "token": "short", "pid": 1}),
        json.dumps({"version": 1, "url": "http://127.0.0.1:1", "token": "t" * 32, "pid": True}),
    ],
)
def test_unusable_files_are_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "api.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DiscoveryError):
        read_api_file(path)


def test_remove_only_deletes_our_own_token(tmp_path: Path) -> None:
    path = tmp_path / "api.json"
    write_api_file(path, ENDPOINT)
    remove_api_file(path, "someone else's token")
    assert path.exists()
    remove_api_file(path, ENDPOINT.token)
    assert not path.exists()
    remove_api_file(path, ENDPOINT.token)  # already gone: no error
