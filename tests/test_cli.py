"""CLI tests with a stub client serving synthetic fixtures (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lcsc2fusion.cli import app as cli_app
from lcsc2fusion.errors import PartNotFoundError
from lcsc2fusion.lbr_writer import lint_library
from lcsc2fusion.model import SearchResult

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
DOCS = {
    "C9000001": SYNTH / "r0603.json",
    "C9000004": SYNTH / "sot23.json",
}


class StubClient:
    def fetch_part(self, lcsc_id: str) -> dict[str, Any]:
        path = DOCS.get(lcsc_id)
        if path is None:
            raise PartNotFoundError(f"Part {lcsc_id} not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def fetch_datasheet_url(self, lcsc_id: str) -> str:
        return ""


RESULTS = [
    SearchResult(
        "C9000001", "SYN-R0603-10K", "SYN-Maker", "0603", "10k resistor", library_type="Basic"
    ),
    SearchResult("C9000004", "SYN-SOT23", "SYN-Maker", "SOT-23", "transistor"),
]


@pytest.fixture(autouse=True)
def stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_app, "_make_client", lambda args: StubClient())
    monkeypatch.setattr(cli_app, "search", lambda query, limit, client: list(RESULTS))


def _errors(path: Path) -> list[str]:
    issues = lint_library(path.read_text(encoding="utf-8"))
    return [i.message for i in issues if i.severity == "error"]


def test_single_id_writes_lint_clean_library(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C9000001", "-o", str(tmp_path)]) == 0
    target = tmp_path / "C9000001.lbr"
    assert target.exists()
    assert _errors(target) == []
    assert "Wrote" in capsys.readouterr().out


def test_lowercase_id_is_normalised(tmp_path: Path) -> None:
    assert cli_app.main(["c9000001", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "C9000001.lbr").exists()


def test_several_ids_go_into_one_named_library(tmp_path: Path) -> None:
    assert cli_app.main(["C9000001", "C9000004", "-n", "mylib", "-o", str(tmp_path)]) == 0
    target = tmp_path / "mylib.lbr"
    text = target.read_text(encoding="utf-8")
    assert text.count("<deviceset ") == 2
    assert _errors(target) == []


def test_several_ids_without_name_use_default_library_name(tmp_path: Path) -> None:
    assert cli_app.main(["C9000001", "C9000004", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "lcsc2fusion.lbr").exists()


def test_unknown_part_reports_error_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C1", "-o", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_phrase_lists_results_and_does_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["10k", "resistor", "-o", str(tmp_path)]) == 0
    out = capsys.readouterr()
    assert "SYN-R0603-10K" in out.out
    assert "--pick" in out.err
    assert list(tmp_path.iterdir()) == []


def test_pick_converts_the_chosen_result(tmp_path: Path) -> None:
    assert cli_app.main(["10k", "resistor", "--pick", "2", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "C9000004.lbr").exists()


def test_pick_out_of_range_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["10k", "--pick", "9", "-o", str(tmp_path)]) == 2
    assert "between 1 and 2" in capsys.readouterr().err


def test_search_flag_only_lists_even_for_an_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C9000001", "--search", "-o", str(tmp_path)]) == 0
    assert "SYN-R0603-10K" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_no_search_results_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_app, "search", lambda query, limit, client: [])
    assert cli_app.main(["nothing", "-o", str(tmp_path)]) == 1
    assert "No results" in capsys.readouterr().err


def test_render_flag_writes_images(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    assert cli_app.main(["C9000001", "--render", "-o", str(tmp_path)]) == 0
    assert list(tmp_path.glob("*_footprint.png"))
    assert list(tmp_path.glob("*_symbol_*.png"))
