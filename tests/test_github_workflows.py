"""Structure checks of the GitHub Actions workflows and of the release documentation.

No YAML parser is installed (the project has no dependency for it), so the workflow files are read
with small line based helpers that understand the subset of YAML these files use: block mappings
indented by two spaces, flow lists in square brackets, `run: |` block scalars and `if: >-` folded
scalars. The workflows cannot be executed here, and neither can the SignPath service be reached, so
these tests pin the properties that matter for safety and for consistency with the rest of the
repository. The job conditions are evaluated by a small interpreter of the GitHub expression subset
that the release workflow uses, over every combination of trigger, repository variable and job
outcome, with GitHub's scheduling rule for `needs` (a skipped need skips the job unless its `if`
has a status function) built into the simulation.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
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


def folded_scalar(lines: list[str], key: str, indent: int) -> str | None:
    """The value of `key:` at `indent`: a one line scalar, or a `>-` block joined with spaces.

    Only `>-` is accepted for a block: `>` keeps a trailing newline, and a condition of the form
    `${{ ... }}` followed by a newline is a non empty string that GitHub treats as true."""
    for number, line in enumerate(lines):
        if not (is_code(line) and indent_of(line) == indent):
            continue
        match = re.match(rf"^ *{re.escape(key)}:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1)
        if value not in (">", ">+", ">-", "|", "|-", "|+"):
            return value
        assert value == ">-", f"{key}: must use >- for a block, found {value}"
        body: list[str] = []
        for follower in lines[number + 1 :]:
            if follower.strip() and indent_of(follower) <= indent:
                break
            body.append(follower.strip())
        return " ".join(part for part in body if part)
    return None


def needs_of(lines: list[str]) -> list[str]:
    value = scalar(lines, "needs", 4)
    if value is None:
        return []
    return flow_list(value) if value.startswith("[") else [value]


def steps_of(lines: list[str]) -> list[str]:
    """The text of every step of a job: the `- ` items at indent 6 under `steps:`."""
    start = next(number for number, line in enumerate(lines) if line == "    steps:")
    steps: list[list[str]] = []
    for line in lines[start + 1 :]:
        if re.match(r"^      - ", line):
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    return ["\n".join(step) for step in steps]


def step_named(lines: list[str], name: str) -> str:
    found = [step for step in steps_of(lines) if f"name: {name}" in step]
    assert len(found) == 1, name
    return found[0]


# GitHub expression subset -----------------------------------------------------------------------


class _Text(str):
    """A string that compares without regard to case, like GitHub's == and !=."""

    def __eq__(self, other: object) -> bool:
        return str.lower(self) == str(other).lower()

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = str.__hash__


_TOKEN = re.compile(
    r"\s*(?:(?P<string>'[^']*')|(?P<op>&&|\|\||==|!=|!|\(|\))"
    r"|(?P<call>[a-z]+\(\))|(?P<name>[A-Za-z_][A-Za-z0-9_.]*))"
)
_OPERATORS = {
    "&&": " and ",
    "||": " or ",
    "!": " not ",
    "==": " == ",
    "!=": " != ",
    "(": "(",
    ")": ")",
}
STATUS_FUNCTION = re.compile(r"\b(?:always|cancelled|failure|success)\(\)")


def expression_of(condition: str) -> str:
    """The expression inside `${{ ... }}`, which must be the whole value with nothing around it:
    leading or trailing text makes GitHub read the condition as a string, and a non empty
    string is true."""
    match = re.fullmatch(r"\$\{\{ (.+) \}\}", condition)
    assert match, f"not exactly one ${{{{ ... }}}} expression: {condition!r}"
    return match.group(1)


def evaluate(expression: str, context: dict[str, str], *, cancelled: bool = False) -> bool:
    """Evaluate the subset the release workflow uses: string literals, context names (an unset
    name is the empty string), == != && || ! parentheses and cancelled()."""
    parts: list[str] = []
    position = 0
    text = expression.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match, f"cannot read the expression at {text[position:]!r}"
        position = match.end()
        if match.group("string") is not None:
            parts.append(f"_Text({match.group('string')})")
        elif match.group("op"):
            parts.append(_OPERATORS[match.group("op")])
        elif match.group("call"):
            assert match.group("call") == "cancelled()", match.group("call")
            parts.append("_cancelled")
        else:
            parts.append(f"_lookup({match.group('name')!r})")
    namespace = {
        "_Text": _Text,
        "_cancelled": cancelled,
        "_lookup": lambda name: _Text(context.get(name, "")),
    }
    return bool(eval("".join(parts), {"__builtins__": {}}, namespace))  # noqa: S307


def simulate(
    release: str,
    *,
    event: str,
    ref_type: str,
    variables: dict[str, str],
    outcomes: dict[str, str],
) -> dict[str, str]:
    """The result of every job of the workflow, following GitHub's scheduling rules.

    A job whose `if` has no status function only runs when all jobs it needs succeeded (so a
    skipped need skips it); one with a status function (cancelled() and the like) is decided by its
    expression alone. A job that runs ends as outcomes[job], default success. If any job is
    cancelled, the whole run counts as cancelled for cancelled()."""
    jobs = jobs_of(release)
    run_is_cancelled = "cancelled" in outcomes.values()
    results: dict[str, str] = {}
    while len(results) < len(jobs):
        progressed = False
        for name, lines in jobs.items():
            needs = needs_of(lines)
            if name in results or any(need not in results for need in needs):
                continue
            progressed = True
            condition = folded_scalar(lines, "if", 4)
            needs_succeeded = all(results[need] == "success" for need in needs)
            if condition is None:
                runs = needs_succeeded
            else:
                context = {"github.event_name": event, "github.ref_type": ref_type}
                context.update({f"vars.{key}": value for key, value in variables.items()})
                context.update({f"needs.{need}.result": results[need] for need in needs})
                expression = expression_of(condition) if condition.startswith("${{") else condition
                wanted = evaluate(expression, context, cancelled=run_is_cancelled)
                has_status_function = bool(STATUS_FUNCTION.search(condition))
                runs = wanted if has_status_function else (needs_succeeded and wanted)
            results[name] = outcomes.get(name, "success") if runs else "skipped"
        assert progressed, "the needs of the jobs form a cycle or name a missing job"
    return results


def find_bash() -> str | None:
    """A real bash for the shell steps: not the WSL launcher of Windows, which needs a distro."""
    candidates = [shutil.which("bash")]
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            if base:
                candidates.append(str(Path(base) / "Git" / "bin" / "bash.exe"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if any(part in candidate.lower() for part in ("system32", "windowsapps")):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "echo ok; command -v awk sha256sum"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and probe.stdout.startswith("ok") and "sha256sum" in probe.stdout:
            return candidate
    return None


def run_bash_step(
    step: str, env: dict[str, str], cwd: Path, bash: str
) -> subprocess.CompletedProcess[str]:
    """Run the `run:` block of a workflow step with exactly the given extra environment."""
    block = run_blocks(step)
    assert len(block) == 1
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in block[0].splitlines())
    return subprocess.run(
        [bash, "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, **env},
        timeout=120,
        check=False,
    )


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
