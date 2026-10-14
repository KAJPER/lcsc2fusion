"""Structure checks of the GitHub Actions workflows and of the release documentation.

No YAML parser is installed (the project has no dependency for it), so the workflow files are read
with small line based helpers that understand the subset of YAML these files use: block mappings
indented by two spaces, flow lists in square brackets, and `run: |` block scalars. The workflows
cannot be executed here, so these tests pin the properties that matter for safety and for
consistency with the rest of the repository.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
RELEASE = WORKFLOWS / "release.yml"
README_EN = ROOT / "README.md"
README_PL = ROOT / "README.pl.md"
RELEASING = ROOT / "RELEASING.md"
BUILD_RELEASE = ROOT / "scripts" / "build_release.py"

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)


# Small YAML helpers -----------------------------------------------------------------------------


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_code(line: str) -> bool:
    """A line with content that is not a full line comment."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if is_code(line)]


def top_level_block(text: str, key: str) -> list[str]:
    """The lines indented under a column 0 `key:` (empty when the key is absent)."""
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if re.fullmatch(rf"{re.escape(key)}:\s*(\S.*)?", line):
            body: list[str] = []
            for follower in lines[number + 1 :]:
                if (
                    follower.strip()
                    and indent_of(follower) == 0
                    and not follower.lstrip().startswith("#")
                ):
                    break
                body.append(follower)
            return body
    return []


def top_level_keys(text: str) -> list[str]:
    return [m.group(1) for line in code_lines(text) if (m := re.match(r"^([A-Za-z_-]+):", line))]


def jobs_of(text: str) -> dict[str, list[str]]:
    """Job id to the lines of that job."""
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in top_level_block(text, "jobs"):
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            result[current] = []
        elif current is not None:
            result[current].append(line)
    return result


def mapping_under(lines: list[str], key: str, indent: int) -> dict[str, str]:
    """`name: value` pairs directly under `key:` found at `indent` (scalars only)."""
    for number, line in enumerate(lines):
        if is_code(line) and indent_of(line) == indent and re.fullmatch(rf" *{key}:\s*", line):
            pairs: dict[str, str] = {}
            for follower in lines[number + 1 :]:
                if not is_code(follower):
                    continue
                if indent_of(follower) <= indent:
                    break
                match = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.*?)\s*$", follower)
                if match and indent_of(follower) == indent + 2:
                    pairs[match.group(1)] = match.group(2)
            return pairs
    return {}


def scalar(lines: list[str], key: str, indent: int) -> str | None:
    for line in lines:
        if is_code(line) and indent_of(line) == indent:
            match = re.match(rf"^ *{re.escape(key)}:\s*(.*?)\s*$", line)
            if match:
                return match.group(1)
    return None


def flow_list(value: str) -> list[str]:
    assert value.startswith("[") and value.endswith("]"), value
    return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]


def uses_of(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^\s*(?:- )?uses:\s*(\S+)", text, flags=re.MULTILINE)]


def run_blocks(text: str) -> list[str]:
    """The text of every `run:` step: a one line command or the body of a `|` block scalar."""
    lines = text.splitlines()
    blocks: list[str] = []
    for number, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not match:
            continue
        key_indent = len(match.group(1)) + (2 if line.lstrip().startswith("- ") else 0)
        value = match.group(2).strip()
        if value in ("|", ">", "|-", ">-"):
            body = []
            for follower in lines[number + 1 :]:
                if follower.strip() and indent_of(follower) <= key_indent:
                    break
                body.append(follower)
            blocks.append("\n".join(body))
        else:
            blocks.append(value)
    return blocks


def prose(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


@pytest.fixture(scope="module")
def release_text() -> str:
    return read(RELEASE)


@pytest.fixture(scope="module")
def ci_text() -> str:
    return read(CI)


# Every workflow ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_workflow_files_have_the_basic_shape(path: Path) -> None:
    text = read(path)
    assert "\t" not in text, "tabs are not allowed in YAML"
    assert "\r" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert re.search(r"^name: \S", text, flags=re.MULTILINE)
    assert {"name", "on", "permissions", "jobs"} <= set(top_level_keys(text))
    for number, line in enumerate(text.splitlines(), start=1):
        assert indent_of(line) % 2 == 0 or line.lstrip().startswith("#"), (
            f"line {number} odd indent"
        )
        assert line == line.rstrip(), f"line {number} has trailing spaces"


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_every_action_is_first_party_and_pinned_to_a_major_version(path: Path) -> None:
    used = uses_of(read(path))
    assert used, "no actions used at all?"
    for reference in used:
        assert re.fullmatch(r"actions/[a-z-]+@v\d+", reference), reference


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_no_secrets_other_than_the_builtin_token(path: Path) -> None:
    text = read(path)
    assert "secrets." not in text
    assert "pull_request_target" not in text
    for match in re.finditer(r"\$\{\{\s*(.*?)\s*\}\}", text):
        assert not match.group(1).startswith("secrets"), match.group(0)


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_run_steps_do_not_interpolate_github_expressions(path: Path) -> None:
    # `${{ ... }}` inside a script is pasted into it before the shell runs (script injection).
    # Values reach scripts through env variables instead.
    for block in run_blocks(read(path)):
        assert "${{" not in block, block


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_every_job_has_a_timeout(path: Path) -> None:
    jobs = jobs_of(read(path))
    assert jobs
    for name, lines in jobs.items():
        value = scalar(lines, "timeout-minutes", 4)
        assert value is not None and 5 <= int(value) <= 60, name


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_top_level_permissions_are_read_only(path: Path) -> None:
    assert mapping_under(read(path).splitlines(), "permissions", 0) == {"contents": "read"}


@pytest.mark.parametrize("path", [CI, RELEASE], ids=lambda p: p.name)
def test_no_workflow_ignores_failures(path: Path) -> None:
    text = read(path)
    assert "continue-on-error" not in text
    assert "|| true" not in text


# CI ---------------------------------------------------------------------------------------------


def test_ci_runs_on_pushes_to_main_and_on_pull_requests(ci_text: str) -> None:
    block = top_level_block(ci_text, "on")
    keys = [m.group(1) for line in block if (m := re.match(r"^  ([a-z_]+):", line))]
    assert sorted(keys) == ["pull_request", "push"]
    push = [line for line in block if is_code(line)]
    branches = [line for line in push if "branches:" in line]
    assert len(branches) == 1 and flow_list(branches[0].split(":", 1)[1].strip()) == ["main"]
    assert not any("tags:" in line for line in block)


def test_ci_cancels_superseded_runs(ci_text: str) -> None:
    concurrency = mapping_under(ci_text.splitlines(), "concurrency", 0)
    assert concurrency["cancel-in-progress"] == "true"
    assert "github.ref" in concurrency["group"]


def test_ci_has_the_lint_type_and_test_jobs(ci_text: str) -> None:
    jobs = jobs_of(ci_text)
    assert set(jobs) == {"ruff", "mypy", "pytest"}
    commands = {name: "\n".join(run_blocks("\n".join(lines))) for name, lines in jobs.items()}
    assert "ruff check ." in commands["ruff"]
    assert "ruff format --check ." in commands["ruff"]
    assert "mypy lcsc2fusion" in commands["mypy"]
    assert "python -m pytest" in commands["pytest"]
    for name, text in commands.items():
        assert "pip install -r requirements-dev.txt" in text, name


def test_ci_tests_run_on_linux_and_windows_with_old_and_new_python(ci_text: str) -> None:
    lines = jobs_of(ci_text)["pytest"]
    strategy = "\n".join(lines)
    assert "fail-fast: false" in strategy
    match = re.search(r"os: (\[.*\])", strategy)
    assert match and set(flow_list(match.group(1))) == {"ubuntu-latest", "windows-latest"}
    match = re.search(r"python: (\[.*\])", strategy)
    assert match and {"3.11", "3.13"} <= set(flow_list(match.group(1)))
    assert "runs-on: ${{ matrix.os }}" in strategy


def test_ci_caches_pip_in_every_python_setup(ci_text: str) -> None:
    assert ci_text.count("actions/setup-python@") == ci_text.count("cache: pip")


def test_ci_installs_no_browser(ci_text: str) -> None:
    # The Playwright test skips itself when no Python has Playwright, so the run stays green and
    # fast. If a browser job is added later, update this test on purpose.
    assert not re.search(r"playwright\s+install|install\s+playwright", ci_text.lower())


# Release ----------------------------------------------------------------------------------------


def test_release_triggers_only_on_version_tags_and_manual_runs(release_text: str) -> None:
    block = top_level_block(release_text, "on")
    keys = [m.group(1) for line in block if (m := re.match(r"^  ([a-z_]+):", line))]
    assert sorted(keys) == ["push", "workflow_dispatch"]
    tags = [line for line in block if "tags:" in line]
    assert len(tags) == 1 and flow_list(tags[0].split(":", 1)[1].strip()) == ["v*"]
    assert not any("branches" in line for line in block)
    # a manual run takes no inputs, so nothing can switch publishing on
    assert not any("inputs:" in line for line in block)


def test_release_writes_only_in_the_publish_job(release_text: str) -> None:
    jobs = jobs_of(release_text)
    assert set(jobs) == {"build", "publish"}
    writes = [
        line.strip()
        for line in code_lines(release_text)
        if re.search(r":\s*(write|write-all|read-all)\s*$", line)
    ]
    assert writes == ["contents: write"]
    assert mapping_under(jobs["publish"], "permissions", 4) == {"contents": "write"}
    assert mapping_under(jobs["build"], "permissions", 4) == {}  # inherits read only


def test_release_publish_job_is_tag_only_and_has_no_repository_code(release_text: str) -> None:
    publish = jobs_of(release_text)["publish"]
    text = "\n".join(publish)
    condition = scalar(publish, "if", 4)
    assert condition is not None
    assert "github.event_name == 'push'" in condition and "github.ref_type == 'tag'" in condition
    assert scalar(publish, "needs", 4) == "build"
    assert "actions/checkout" not in text, (
        "the job with the write token must not run repository code"
    )
    assert "setup-python" not in text and "python" not in "\n".join(run_blocks(text))
    assert "GH_TOKEN: ${{ github.token }}" in text


def test_release_publishes_only_through_gh_release_create_with_verify_tag(
    release_text: str,
) -> None:
    assert "\n".join(code_lines(release_text)).count("gh release create") == 1
    publish = "\n".join(jobs_of(release_text)["publish"])
    command = "\n".join(run_blocks(publish))
    assert "gh release create" in command
    assert "--verify-tag" in command
    assert "--notes-file" in command and "--title" in command
    assert "--prerelease" in command
    # never overwrite, never edit or delete an existing release, no draft left behind
    for forbidden in ("--clobber", "gh release upload", "gh release edit", "gh release delete"):
        assert forbidden not in release_text, forbidden
    assert "gh release view" in command  # the readable "already exists" check
    # nothing in the build job talks to GitHub Releases
    build = "\n".join(jobs_of(release_text)["build"])
    assert not re.search(r"\bgh\s", "\n".join(run_blocks(build)))


def test_release_marks_hyphen_versions_as_prereleases(release_text: str) -> None:
    assert "$tag.Contains('-')" in release_text
    assert 'if [ "$PRERELEASE" = "true" ]; then args+=(--prerelease); fi' in release_text


def test_release_build_job_checks_tests_builds_verifies_and_writes_notes(release_text: str) -> None:
    build = jobs_of(release_text)["build"]
    assert scalar(build, "runs-on", 4) == "windows-latest"  # IExpress exists only on Windows
    steps = run_blocks("\n".join(build))
    joined = "\n".join(steps)
    positions = [
        joined.index(needle)
        for needle in (
            "pip install -r requirements-dev.txt",
            "scripts/build_release.py --check-tag",
            "python -m pytest",
            "scripts/build_release.py --out dist/release",
            "scripts/verify_checksums.py dist/release",
            "scripts/release_notes.py",
        )
    ]
    assert positions == sorted(positions), "the steps must run in this order"
    assert "actions/checkout" in "\n".join(build)
    assert "persist-credentials: false" in "\n".join(build)


def test_release_tag_comes_from_the_ref_through_env_variables_only(release_text: str) -> None:
    build = "\n".join(run_blocks("\n".join(jobs_of(release_text)["build"])))
    assert "$env:GITHUB_REF_NAME" in build
    assert "$env:TAG" in build
    assert r"'^v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$'" in build  # the shape is checked before use


def test_release_dry_run_uploads_artifacts_and_never_publishes(release_text: str) -> None:
    assert release_text.count("actions/upload-artifact@") == 2
    assert "name: release-files" in release_text and "name: release-notes" in release_text
    assert release_text.count("if-no-files-found: error") == 2
    publish = "\n".join(jobs_of(release_text)["publish"])
    assert publish.count("actions/download-artifact@") == 2
    # the only job that can publish is guarded by the push event; a dispatch run skips it
    assert "workflow_dispatch" not in publish


def test_release_never_cancels_a_running_release(release_text: str) -> None:
    concurrency = mapping_under(release_text.splitlines(), "concurrency", 0)
    assert concurrency["cancel-in-progress"] == "false"
    assert "github.ref" in concurrency["group"]


# Scripts and files that the workflows name ------------------------------------------------------


def referenced_scripts() -> list[str]:
    names: set[str] = set()
    for path in (CI, RELEASE):
        names.update(re.findall(r"\bscripts/[A-Za-z0-9_./-]+\.py\b", read(path)))
    return sorted(names)


@pytest.mark.parametrize("script", referenced_scripts())
def test_scripts_named_by_the_workflows_exist(script: str) -> None:
    path = ROOT / script
    if script == "scripts/build_release.py" and not path.exists():
        pytest.skip("scripts/build_release.py is written by another change that has not landed yet")
    assert path.is_file(), script


def test_the_workflows_name_exactly_the_expected_scripts() -> None:
    assert referenced_scripts() == [
        "scripts/build_release.py",
        "scripts/release_notes.py",
        "scripts/verify_checksums.py",
    ]


def test_build_release_script_offers_the_flags_the_workflow_uses() -> None:
    if not BUILD_RELEASE.exists():
        pytest.skip("scripts/build_release.py is written by another change that has not landed yet")
    source = read(BUILD_RELEASE)
    for flag in ("--check-tag", "--out", "--no-exe"):
        assert flag in source, flag


@pytest.mark.parametrize("name", ["requirements-dev.txt", "pyproject.toml"])
def test_files_named_by_the_workflows_exist(name: str) -> None:
    assert (ROOT / name).is_file()
    assert name in read(CI) and name in read(RELEASE)


# Release documentation --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def release_notes() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location(
        "release_notes_for_workflow_tests", ROOT / "scripts" / "release_notes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


ASSET_PATTERNS = [
    "LCSC2Fusion-Setup-<version>.exe",
    "LCSC2Fusion-<version>-installer.zip",
    "LCSC2Fusion-<version>-addin.zip",
    "Install-LCSC2Fusion.bat",
    "Uninstall-LCSC2Fusion.bat",
    "SHA256SUMS.txt",
]


@pytest.mark.parametrize("path", [README_EN, README_PL, RELEASING], ids=lambda p: p.name)
def test_documents_use_the_asset_names_of_the_release(path: Path) -> None:
    text = read(path)
    for name in ASSET_PATTERNS:
        assert name in text, f"{path.name} does not mention {name}"
    assert "Get-FileHash" in text


@pytest.mark.parametrize("path", [README_EN, README_PL], ids=lambda p: p.name)
def test_readmes_link_the_latest_release_and_describe_the_install_target(path: Path) -> None:
    text = read(path)
    assert "https://github.com/KAJPER/lcsc2fusion/releases/latest" in text
    assert r"%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion" in text
    assert "SmartScreen" in text
    assert "(RELEASING.md)" in text and "(CHANGELOG.md)" in text


def test_both_readmes_name_exactly_the_same_downloads() -> None:
    pattern = re.compile(r"LCSC2Fusion-[A-Za-z0-9<>.-]*\.(?:exe|zip)|[A-Za-z]*-LCSC2Fusion\.bat")
    found_en = set(pattern.findall(read(README_EN)))
    found_pl = set(pattern.findall(read(README_PL)))
    assert found_en == found_pl
    assert {"LCSC2Fusion-Setup-<version>.exe", "LCSC2Fusion-<version>-installer.zip"} <= found_en


def test_readme_asset_names_equal_the_names_the_release_is_built_with(
    release_notes: ModuleType,
) -> None:
    footer = release_notes.footer("1.2.3")
    for pattern in ASSET_PATTERNS[:3]:
        assert pattern.replace("<version>", "1.2.3") in footer
    # the check script of the workflow uses the same names
    spec = importlib.util.spec_from_file_location(
        "verify_checksums_for_workflow_tests", ROOT / "scripts" / "verify_checksums.py"
    )
    assert spec is not None and spec.loader is not None
    verify = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = verify
    try:
        spec.loader.exec_module(verify)
    finally:
        sys.modules.pop(spec.name, None)
    expected = {pattern.replace("<version>", "1.2.3") for pattern in ASSET_PATTERNS[:3]}
    assert set(verify.expected_assets("1.2.3")) == expected


def test_readmes_keep_the_same_headings_and_links() -> None:
    def headings(path: Path) -> list[int]:
        return [
            len(m.group(1))
            for line in prose(read(path)).splitlines()
            if (m := re.match(r"^(#{1,6})\s+\S", line))
        ]

    assert headings(README_EN) == headings(README_PL)
    for path in (README_EN, README_PL):
        text = read(path)
        for anchor in re.findall(r"\]\(#([^)]+)\)|href=\"#([^\"]+)\"", prose(text)):
            target = next(part for part in anchor if part)
            titles = [
                re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-")
                for line in prose(text).splitlines()
                if (m := re.match(r"^#{1,6}\s+(.*\S)\s*$", line))
            ]
            assert target in titles, f"{path.name}: #{target} has no heading"


def test_releasing_describes_the_steps_a_maintainer_runs() -> None:
    text = read(RELEASING)
    for needle in (
        "--check-tag",
        "git tag v",
        "git push origin",
        "workflow_dispatch",
        "Run workflow",
        "gh release delete",
        "SignPath",
        "pyproject.toml",
        "lcsc2fusion/__init__.py",
        "LCSC2Fusion.manifest",
        "CHANGELOG.md",
        "pre-release",
    ):
        assert needle in text, needle


@pytest.mark.parametrize(
    "path",
    [
        CI,
        RELEASE,
        ROOT / "CHANGELOG.md",
        RELEASING,
        README_EN,
        README_PL,
        ROOT / "addin" / "README.md",
        ROOT / "scripts" / "release_notes.py",
        ROOT / "scripts" / "verify_checksums.py",
        ROOT / "tests" / "test_github_workflows.py",
        ROOT / "tests" / "test_release_notes.py",
        ROOT / "tests" / "test_verify_checksums.py",
    ],
    ids=lambda p: p.name,
)
def test_no_long_dashes_and_no_double_hyphen_prose(path: Path) -> None:
    text = read(path)
    assert EM_DASH not in text and EN_DASH not in text
    if path.suffix == ".md":
        assert " -- " not in prose(text)
    elif "tests" not in path.parts:  # the tests spell the pattern out themselves
        assert " -- " not in text
