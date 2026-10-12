"""CLI tests with a stub client serving synthetic fixtures (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from step_samples import make_box_step

from lcsc2fusion.cli import app as cli_app
from lcsc2fusion.client.easyeda import DEFAULT_MODEL_TTL_S
from lcsc2fusion.errors import ModelError, PartNotFoundError, RateLimitedError
from lcsc2fusion.lbr_writer import lint_library
from lcsc2fusion.model import SearchResult

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
DOCS = {
    "C9000001": SYNTH / "r0603.json",
    "C9000004": SYNTH / "sot23.json",
    "C9000016": SYNTH / "box3d_rot90.json",
    "C9000017": SYNTH / "box3d_z.json",
}
# Model uuid -> sidecar with the model size, for the synthetic parts that have an SVGNODE.
MODEL_UUIDS = {
    "cc9537b400bd60d0fa1f049537c4bf44": SYNTH / "box3d_rot90.expected.json",
    "86bcd63db8c66b1af2659db97a969b57": SYNTH / "box3d_z.expected.json",
    "8ce2a36307c68382e5e789edc6b90caf": SYNTH / "r0603.expected.json",
}


class StubClient:
    """Serves synthetic documents and, for `--3d`, STEP boxes matching the sidecar sizes."""

    model_failure: ClassVar[Exception | None] = None
    model_calls: ClassVar[list[str]] = []

    def fetch_model_step(self, uuid: str) -> bytes:
        StubClient.model_calls.append(uuid)
        if StubClient.model_failure is not None:
            raise StubClient.model_failure
        sidecar = MODEL_UUIDS.get(uuid)
        if sidecar is None:
            raise ModelError(f"3D model {uuid} not found on EasyEDA (HTTP 404).")
        native = json.loads(sidecar.read_text(encoding="utf-8"))["model3d"]["native_size_mm"]
        return make_box_step(size=(native[0], native[1], 0.9), center=(10.0, 5.0), zmin=-0.2)

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


_REAL_MAKE_CLIENT = cli_app._make_client


@pytest.fixture(autouse=True)
def stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    StubClient.model_failure = None
    StubClient.model_calls = []
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


# --3d


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_3d_flag_writes_the_step_files_and_prints_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C9000016", "--3d", "-o", str(tmp_path)]) == 0
    assert _files(tmp_path) == [
        "C9000016.3d.json",
        "C9000016.lbr",
        "C9000016.placed.step",
        "C9000016.step",
    ]
    out = capsys.readouterr().out.splitlines()
    assert f"Wrote {tmp_path / 'C9000016.lbr'}" in out
    for name in ("C9000016.step", "C9000016.placed.step", "C9000016.3d.json"):
        assert f"Wrote {tmp_path / name}" in out
    attach = [line for line in out if "attach this file in Fusion" in line]
    assert attach == [
        f"3D model for C9000016, attach this file in Fusion: {tmp_path / 'C9000016.placed.step'}"
    ]
    hint = " ".join(out)
    assert "Content Manager > Footprints" in hint
    assert "Attach Existing 3D Model" in hint
    assert "docs/MANUAL_TEST.md" in hint
    assert "Gate 3" in hint
    assert _errors(tmp_path / "C9000016.lbr") == []
    lbr = (tmp_path / "C9000016.lbr").read_text(encoding="utf-8")
    assert 'name="MODEL3D_STEP" value="C9000016.placed.step"' in lbr


def test_without_the_flag_there_is_no_3d_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C9000016", "-o", str(tmp_path)]) == 0
    assert _files(tmp_path) == ["C9000016.lbr"]
    captured = capsys.readouterr()
    assert "3D" not in captured.out
    assert "STEP" not in captured.out
    assert "3D" not in captured.err  # parser notes about models stay hidden without --3d
    assert StubClient.model_calls == []


def test_3d_with_several_ids_writes_files_for_each(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = ["C9000016", "C9000017", "C9000004"]
    assert cli_app.main([*ids, "--3d", "-n", "pair", "-o", str(tmp_path)]) == 0
    assert _files(tmp_path) == sorted(
        [
            "pair.lbr",
            *(f"{i}{s}" for i in ids[:2] for s in (".step", ".placed.step", ".3d.json")),
        ]
    )
    captured = capsys.readouterr()
    assert captured.out.count("attach this file in Fusion") == 2
    assert captured.out.count("Attach Existing 3D Model") == 1  # the hint is printed once
    assert "warning: C9000004: no 3D model available" in captured.err


def test_3d_with_pick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    picked = SearchResult("C9000017", "SYN-BOX3D-Z", "SYN-Maker", "box", "box with z")
    monkeypatch.setattr(cli_app, "search", lambda query, limit, client: [RESULTS[1], picked])
    assert cli_app.main(["box", "--pick", "2", "--3d", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "C9000017.placed.step").is_file()
    assert "attach this file in Fusion" in capsys.readouterr().out


def test_3d_pick_of_a_part_without_a_model_still_writes_the_library(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["10k", "resistor", "--pick", "2", "--3d", "-o", str(tmp_path)]) == 0
    assert _files(tmp_path) == ["C9000004.lbr"]
    captured = capsys.readouterr()
    assert "warning: C9000004: no 3D model available" in captured.err
    assert "Attach Existing 3D Model" not in captured.out  # nothing to attach


def test_3d_with_render(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    assert cli_app.main(["C9000016", "--3d", "--render", "-o", str(tmp_path)]) == 0
    assert (tmp_path / "C9000016.placed.step").is_file()
    assert list(tmp_path.glob("*_footprint.png"))
    assert list(tmp_path.glob("*_symbol_*.png"))


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        (ModelError("3D model abc not found on EasyEDA (HTTP 404)."), "not found"),
        (RateLimitedError("modules.easyeda.com blocked the request (HTTP 403)."), "blocked"),
    ],
    ids=["model-error", "rate-limited"],
)
def test_a_failed_3d_download_is_a_warning_and_the_exit_code_stays_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failure: Exception, fragment: str
) -> None:
    StubClient.model_failure = failure
    assert cli_app.main(["C9000016", "--3d", "-o", str(tmp_path)]) == 0
    assert _files(tmp_path) == ["C9000016.lbr"]
    assert _errors(tmp_path / "C9000016.lbr") == []
    captured = capsys.readouterr()
    assert "Wrote" in captured.out
    assert "Attach Existing 3D Model" not in captured.out
    (warning,) = [line for line in captured.err.splitlines() if "3D model not written" in line]
    assert warning.startswith("warning: C9000016: 3D model not written: ")
    assert fragment in warning
    assert "error:" not in captured.err


def test_a_part_that_fails_still_fails_the_run_with_3d(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_app.main(["C1", "--3d", "-o", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_parser_notes_about_models_show_up_only_with_3d(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(DOCS, "C9000018", SYNTH / "box3d_stale.json")
    monkeypatch.setitem(
        MODEL_UUIDS, "fde83ba10fd012b748e71d12f7ab2e6a", SYNTH / "box3d_stale.expected.json"
    )
    assert cli_app.main(["C9000018", "-o", str(tmp_path / "off")]) == 0
    assert "stale" not in capsys.readouterr().err
    assert cli_app.main(["C9000018", "--3d", "-o", str(tmp_path / "on")]) == 0
    assert "warning: C9000018: 3D model: c_origin is stale" in capsys.readouterr().err


def test_help_mentions_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli_app.main(["--help"])
    assert info.value.code == 0
    assert "--3d" in capsys.readouterr().out


def test_the_flag_defaults_to_off() -> None:
    parser = cli_app._build_parser()
    assert parser.parse_args(["C1"]).with_3d is False
    assert parser.parse_args(["C1", "--3d"]).with_3d is True


@pytest.mark.parametrize(("extra", "model_ttl"), [([], DEFAULT_MODEL_TTL_S), (["--no-cache"], 0.0)])
def test_real_client_gets_a_model_lifetime_and_no_cache_switches_it_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str], model_ttl: float
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_app, "EasyEdaClient", lambda **kwargs: seen.append(kwargs))
    for cache_args in ([], ["--cache-dir", str(tmp_path)]):
        args = cli_app._build_parser().parse_args(["C1", *extra, *cache_args])
        _REAL_MAKE_CLIENT(args)
    assert [kwargs["model_ttl_s"] for kwargs in seen] == [model_ttl, model_ttl]
