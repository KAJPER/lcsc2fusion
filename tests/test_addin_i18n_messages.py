"""The Python texts of the add-in against the MESSAGES table of palette/i18n.js.

The controller, the library flow, the adapter and the core send English sentences to the window
(`status` text, `error` message, `added.warnings`, `added.instructions`, `fusion_done.message` and
`fusion_done.warnings`). The page translates them with the pattern table `MESSAGES`. Nothing but a
test notices when somebody rewords a Python sentence, so this module closes the gap twice:

* STATIC: the sources are parsed with `ast`. Every string that is handed to a user message sink
  (`_BadRequest`, `_status`, `_Failure`, `LibraryApiError`, ...) becomes a sample text, an f-string
  with plausible values. Each sample must be matched by a pattern.
* RUNTIME: the real `Controller`, `FusionLibraryFlow`, registry, `CorePartService` and HTTP
  transport run against fakes through the success and failure branches, and every text that
  reaches the window must be matched by a pattern.

The matching and the translation run in the real i18n.js under Node (one call for all texts), so
the table is tested the way the page uses it. Without Node those tests are skipped.

Texts that quote EasyEDA data (pad names, parser notes) have no pattern on purpose, and a test
checks that they stay as they are.
"""

from __future__ import annotations

import ast
import dataclasses
import http.client
import itertools
import json
import re
import shutil
import socket
import ssl
import subprocess
import unicodedata
import urllib.error
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import controller
import pytest
from controller import (
    AddedPart,
    Controller,
    ImportOutcome,
    JobResult,
    build_instructions,
    describe_open_outcome,
)
from core_service import CorePartService
from fusion_library_flow import (
    LIBRARY_NAME,
    FlowRequest,
    FlowResult,
    FusionLibraryFlow,
    LibraryRef,
)
from library_registry import LibraryState, PartRecord, StateStore, inspect_part_lbr

from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.client.http import UrllibTransport
from lcsc2fusion.client.ratelimit import RateLimiter
from lcsc2fusion.errors import LcscError, PartNotFoundError
from lcsc2fusion.lbr_writer import write_library
from lcsc2fusion.model import Part, SearchResult
from lcsc2fusion.parser.part import parse_part

ROOT = Path(__file__).resolve().parents[1]
ADDIN_DIR = ROOT / "addin" / "LCSC2Fusion"
I18N_JS = ADDIN_DIR / "palette" / "i18n.js"
SYNTH = Path(__file__).parent / "fixtures" / "synthetic"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

WIN_PATH = r"C:\Users\Kacper\LCSC2Fusion\libraries\C2040.lbr"
WIN_POSIX_PATH = "C:/Users/Kacper/LCSC2Fusion/libraries/C2040.lbr"

# Node: loads the real i18n.js (it needs only a `window`) and answers one JSON request on stdin.
NODE_SCRIPT = r"""
const fs = require('fs');
global.window = {};
require(process.argv[1]);
const i18n = window.LcscI18n;
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
let reply;
if (request.mode === 'dump') {
  reply = {
    entries: i18n.MESSAGES.map((entry) => ({
      source: entry.re instanceof RegExp ? entry.re.source : null,
      flags: entry.re instanceof RegExp ? entry.re.flags : null,
      keys: Object.keys(entry),
      pl: entry.pl === undefined ? null : entry.pl,
    })),
    en: i18n.DICTIONARIES.en,
    pl: i18n.DICTIONARIES.pl,
  };
} else {
  reply = request.texts.map((text) => {
    const subject = String(text).trim();
    const found = i18n.findMessage(text);
    const all = [];
    i18n.MESSAGES.forEach((entry, index) => {
      if (entry.re.test(subject)) all.push(index);
    });
    return {
      text: text,
      index: found ? found.index : null,
      all: all,
      pl: i18n.translateMessage(text, 'pl'),
      en: i18n.translateMessage(text, 'en'),
      xx: i18n.translateMessage(text, 'xx'),
    };
  });
}
process.stdout.write(JSON.stringify(reply));
"""


def run_node(request: dict[str, Any]) -> Any:
    assert NODE is not None
    completed = subprocess.run(
        [NODE, "-e", NODE_SCRIPT, str(I18N_JS)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@dataclass(frozen=True)
class Result:
    """What the page makes of one text."""

    text: str
    index: int | None  # first matching entry, the one that wins
    all: tuple[int, ...]  # every entry whose pattern matches
    pl: str
    en: str
    xx: str


def translate(texts: Iterable[str]) -> dict[str, Result]:
    unique = list(dict.fromkeys(texts))
    rows = run_node({"mode": "translate", "texts": unique})
    return {
        row["text"]: Result(
            text=row["text"],
            index=row["index"],
            all=tuple(row["all"]),
            pl=row["pl"],
            en=row["en"],
            xx=row["xx"],
        )
        for row in rows
    }


# Reading a pattern -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RegexScan:
    named: tuple[str, ...]
    plain_groups: int  # capturing groups without a name: not allowed in the table
    top_level_alternation: bool  # `^a|b$` is not anchored the way it looks


def scan_regex(source: str) -> RegexScan:
    named: list[str] = []
    plain = 0
    depth = 0
    in_class = False
    top_level_alternation = False
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "(":
            if source.startswith("(?<", index) and source[index + 3 : index + 4] not in ("=", "!"):
                match = re.match(r"\(\?<([A-Za-z_]\w*)>", source[index:])
                assert match is not None, f"bad group in {source!r}"
                named.append(match.group(1))
            elif not source.startswith("(?", index):
                plain += 1
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            top_level_alternation = True
        index += 1
    return RegexScan(tuple(named), plain, top_level_alternation)


PLACEHOLDER = re.compile(r"\{(\w+)(:msg)?\}")


@dataclass(frozen=True)
class Entry:
    index: int
    source: str | None
    flags: str | None
    keys: tuple[str, ...]
    pl: Any

    @property
    def scan(self) -> RegexScan:
        assert self.source is not None
        return scan_regex(self.source)

    @property
    def templates(self) -> list[str]:
        if isinstance(self.pl, str):
            return [self.pl]
        if isinstance(self.pl, dict):
            return [value for value in self.pl.values() if isinstance(value, str)]
        return []

    def describe(self) -> str:
        return f"#{self.index} /{self.source}/"


@dataclass(frozen=True)
class Table:
    entries: tuple[Entry, ...]
    en: Mapping[str, Any]
    pl: Mapping[str, Any]


def load_table() -> Table:
    dump = run_node({"mode": "dump"})
    entries = tuple(
        Entry(index, row["source"], row["flags"], tuple(row["keys"]), row["pl"])
        for index, row in enumerate(dump["entries"])
    )
    return Table(entries, dump["en"], dump["pl"])


# The Python side, static: sample texts from the sources ------------------------------------------

# Where a sample has no better value. Matched on the source text of the expression, first one wins.
SAMPLE_RULES: tuple[tuple[str, str], ...] = (
    (r"^len\(|^response\.status$|^pause$|^left$|^timeout$|^limit$|^counter$", "5"),
    (r"lcsc_id$", "C2040"),
    (r"^_?label(\(|$)", "C2040 (RP2040)"),
    (r"^(ref|reopen)\.name$", LIBRARY_NAME),
    (r"(^|\.)name$|^stem$", "C2040.placed.step"),
    (
        r"(^|[._])(path|folder|step|step_path|source|lbr_path|parent)$|_out_dir$|^command_path\(",
        WIN_PATH,
    ),
    (r"^host$", "easyeda.com"),
    (r"^(exc|_describe\(exc\)|reason|outcome\.message)$", "boom"),
    (r"^key$", "with_3d"),
)
# Values that depend on the function the expression sits in: (function, expression) to samples.
PARAM_SAMPLES: dict[tuple[str, str], tuple[str, ...]] = {
    ("_check_token", "what"): ("device set name", "library file name"),
    ("command_path", "_QUOTE_NAMES[char]"): ("double quote", "single quote"),
    ("command_path", "text"): (WIN_POSIX_PATH,),
    ("add", "text"): ("boom",),
}

# Fragments that only make sense glued to another text. The static pass cannot build them, so the
# runtime scenarios below drive the real code through every variant and check those instead.
# Each template must still exist in the sources (a test checks it), so a stale entry is found.
ALLOWED_FRAGMENTS = frozenset(
    {
        # FusionLibraryFlow._call: "what" is one of four verb phrases, the hint exists for one.
        "Could not {what}: {_describe(exc)}.{extra}",
        # FusionLibraryFlow._three_d: "stage" is one of three phrases, the hint only for one.
        "3D: could not {stage}: {_describe(exc)}.{hint}",
    }
)
# Texts of the client that are about the URL we built ourselves: no user can do anything about it.
UNTRANSLATED_ON_PURPOSE = frozenset(
    {
        "Unsupported or malformed URL: {url!r}",
        "Malformed URL {url!r}: {exc}",
    }
)


def registry_reason_forms(reason: str) -> list[str]:
    return [f"The library registry {WIN_PATH} {reason}; starting with an empty registry."]


def registry_problem_forms(problem: str) -> list[str]:
    """A problem is either the reason of an unusable file or a warning behind "{path}: "."""
    return [*registry_reason_forms(problem), f"{WIN_PATH}: {problem}"]


@dataclass(frozen=True)
class SourceSpec:
    """Which calls of one source file carry user messages."""

    path: str
    calls: frozenset[str] = frozenset()
    keywords: frozenset[str] = frozenset({"message"})
    returns_in: frozenset[str] = frozenset()  # functions whose returned strings are messages
    dict_keys: frozenset[str] = frozenset()  # keys of dict literals that hold a message
    constants: frozenset[str] = frozenset()  # module constants that are messages
    wrappers: Mapping[str, Callable[[str], list[str]]] = field(default_factory=dict)


CLIENT_ERRORS = frozenset(
    {
        "NetworkError",
        "TransientNetworkError",
        "RateLimitedError",
        "PartNotFoundError",
        "ParseError",
        "ModelError",
    }
)
SPECS: tuple[SourceSpec, ...] = (
    SourceSpec(
        "addin/LCSC2Fusion/controller.py",
        calls=frozenset(
            {"_BadRequest", "_status", "_fusion_refused", "_start_job", "ImportOutcome"}
        ),
        returns_in=frozenset({"build_instructions", "describe_open_outcome"}),
        dict_keys=frozenset({"text", "message"}),
        constants=frozenset({"GENERIC_ERROR_TEXT", "MANUAL_STEPS", "FUSION_UNAVAILABLE_TEXT"}),
    ),
    SourceSpec(
        "addin/LCSC2Fusion/fusion_library_flow.py",
        calls=frozenset({"_Failure", "_say", "_warn", "_conclude"}),
        returns_in=frozenset({"_execute"}),
    ),
    SourceSpec(
        "addin/LCSC2Fusion/library_registry.py",
        calls=frozenset({"PartLbrError", "_unusable", "problems.append"}),
        wrappers={"_unusable": registry_reason_forms, "problems.append": registry_problem_forms},
    ),
    SourceSpec(
        "addin/LCSC2Fusion/fusion_adapter.py",
        calls=frozenset({"LibraryApiError", "ValueError", "ImportOutcome", "_failed"}),
    ),
    SourceSpec("lcsc2fusion/service.py", calls=frozenset({"problems.append"})),
    SourceSpec("lcsc2fusion/client/easyeda.py", calls=CLIENT_ERRORS),
    SourceSpec("lcsc2fusion/client/http.py", calls=CLIENT_ERRORS),
    SourceSpec("lcsc2fusion/client/search.py", calls=CLIENT_ERRORS),
    SourceSpec("lcsc2fusion/client/ratelimit.py", calls=frozenset({"RateLimitedError"})),
)
# Constants the sources import from each other, so that f-strings resolve without importing adsk.
IMPORTED_CONSTANTS: dict[str, str] = {
    "LIBRARY_NAME": LIBRARY_NAME,
    "BUTTON_ADD_TO_FUSION": controller.BUTTON_ADD_TO_FUSION,
    "BUTTON_OPEN_SEPARATE": controller.BUTTON_OPEN_SEPARATE,
    "MANUAL_STEPS": controller.MANUAL_STEPS,
    "MAX_QUERY_LENGTH": str(controller.MAX_QUERY_LENGTH),
    "STATE_VERSION": "1",
}
MAX_VARIANTS = 400


@dataclass(frozen=True)
class Context:
    function: ast.AST | None
    function_name: str
    constants: Mapping[str, str]
    message_constants: frozenset[str]


def sample_values(function_name: str, expression: str) -> list[str]:
    override = PARAM_SAMPLES.get((function_name, expression))
    if override is not None:
        return list(override)
    for pattern, value in SAMPLE_RULES:
        if re.search(pattern, expression):
            return [value]
    return ["X"]


def product(pieces: Sequence[Sequence[str]]) -> list[str]:
    joined = ("".join(parts) for parts in itertools.product(*pieces))
    return list(dict.fromkeys(itertools.islice(joined, MAX_VARIANTS)))


def local_values(context: Context, name: str, depth: int) -> list[str]:
    """Strings a local variable of the enclosing function can hold (all of its assignments)."""
    if context.function is None:
        return []
    found: list[str] = []
    for node in ast.walk(context.function):
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == name:
                value = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                value = node.value
        if value is not None:
            found.extend(expand(value, context, depth=depth + 1) or [])
    return list(dict.fromkeys(found))


def format_value(node: ast.FormattedValue, context: Context, depth: int) -> list[str]:
    expression = ast.unparse(node.value)
    options: list[str] = []
    if isinstance(node.value, ast.Name):
        options = local_values(context, node.value.id, depth)
        if not options and node.value.id in context.constants:
            options = [context.constants[node.value.id]]
    if not options:
        options = sample_values(context.function_name, expression)
    if node.conversion == ord("r"):
        options = [repr(option) for option in options]
    return options


def expand(
    node: ast.AST, context: Context, *, top: bool = False, depth: int = 0
) -> list[str] | None:
    """The texts `node` can evaluate to, or None when it is not a string we can know."""
    if depth > 8:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return [node.value]
        if isinstance(node.value, int) and not isinstance(node.value, bool) and not top:
            return [str(node.value)]
        return None
    if isinstance(node, ast.JoinedStr):
        pieces: list[list[str]] = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                pieces.append([str(part.value)])
            elif isinstance(part, ast.FormattedValue):
                pieces.append(format_value(part, context, depth))
            else:
                return None
        return product(pieces)
    if isinstance(node, ast.IfExp):
        branches = [
            expand(branch, context, top=top, depth=depth + 1) for branch in (node.body, node.orelse)
        ]
        merged = [text for branch in branches for text in branch or []]
        return list(dict.fromkeys(merged)) or None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = expand(node.left, context, top=top, depth=depth + 1)
        right = expand(node.right, context, top=top, depth=depth + 1)
        if left is None or right is None:
            return None
        return product([left, right])
    if isinstance(node, ast.Name):
        local = local_values(context, node.id, depth)
        if local:
            return local
        if node.id in context.constants and (not top or node.id in context.message_constants):
            return [context.constants[node.id]]
        return None if top else sample_values(context.function_name, node.id)
    return None


def raw_template(node: ast.AST) -> str | None:
    """The f-string as written, every value as {expression}; used to name allowed fragments."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                conversion = f"!{chr(part.conversion)}" if part.conversion > 0 else ""
                parts.append("{" + ast.unparse(part.value) + conversion + "}")
        return "".join(parts)
    return None


@dataclass(frozen=True)
class Candidate:
    """One place in the sources that sends a text to the window."""

    location: str
    callee: str
    template: str | None
    forms: tuple[tuple[str, ...], ...]  # per sample text: the texts the window can see for it

    @property
    def samples(self) -> list[str]:
        return [form[0] for form in self.forms]


@dataclass
class StaticScan:
    candidates: list[Candidate] = field(default_factory=list)
    allowed_seen: set[str] = field(default_factory=set)
    skipped_seen: set[str] = field(default_factory=set)

    def forms(self) -> list[str]:
        return [text for candidate in self.candidates for form in candidate.forms for text in form]

    def reasons(self) -> list[str]:
        """Texts of the Fusion adapter that arrive as the reason inside another sentence."""
        return list(
            dict.fromkeys(
                text
                for candidate in self.candidates
                if candidate.location.startswith("addin/LCSC2Fusion/fusion_adapter.py")
                and candidate.callee in ("LibraryApiError", "ValueError")
                for text in candidate.samples
            )
        )


class Collector(ast.NodeVisitor):
    def __init__(self, spec: SourceSpec, constants: Mapping[str, str], scan: StaticScan) -> None:
        self._spec = spec
        self._constants = constants
        self._scan = scan
        self._functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _context(self) -> Context:
        function = self._functions[-1] if self._functions else None
        return Context(
            function, function.name if function else "", self._constants, self._spec.constants
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        callee = ast.unparse(node.func)
        if callee in self._spec.calls or callee.rsplit(".", 1)[-1] in self._spec.calls:
            arguments = [
                *node.args,
                *(k.value for k in node.keywords if k.arg in self._spec.keywords),
            ]
            for argument in arguments:
                self._consider(argument, callee)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self._functions and self._functions[-1].name in self._spec.returns_in and node.value:
            values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
            for value in values:
                self._consider(value, "return")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value in self._spec.dict_keys:
                self._consider(value, "dict")
        self.generic_visit(node)

    def _consider(self, node: ast.expr, callee: str) -> None:
        location = f"{self._spec.path}:{node.lineno}"
        template = raw_template(node)
        if template in ALLOWED_FRAGMENTS:
            self._scan.allowed_seen.add(template)
            return
        if template in UNTRANSLATED_ON_PURPOSE:
            self._scan.skipped_seen.add(template)
            return
        texts = expand(node, self._context(), top=True)
        if not texts:
            return
        wrapper = self._spec.wrappers.get(callee) or self._spec.wrappers.get(
            callee.rsplit(".", 1)[-1]
        )
        forms = tuple(tuple(wrapper(text)) if wrapper else (text,) for text in texts)
        self._scan.candidates.append(Candidate(location, callee, template, forms))


def module_constants(tree: ast.Module) -> dict[str, str]:
    constants = dict(IMPORTED_CONSTANTS)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            values = expand(node.value, Context(None, "", constants, frozenset()))
            if values:
                constants[node.targets[0].id] = values[0]
    return constants


def scan_sources() -> StaticScan:
    scan = StaticScan()
    for spec in SPECS:
        tree = ast.parse((ROOT / spec.path).read_text(encoding="utf-8"))
        constants = module_constants(tree)
        Collector(spec, constants, scan).visit(tree)
        for node in tree.body:  # message constants such as GENERIC_ERROR_TEXT
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in spec.constants
            ):
                text = constants[node.targets[0].id]
                location = f"{spec.path}:{node.lineno}"
                scan.candidates.append(Candidate(location, node.targets[0].id, None, ((text,),)))
    return scan


# The Python side, at runtime: fakes and scenarios ------------------------------------------------


class Seen:
    """Every text that reached the window, with the places it was seen at."""

    def __init__(self) -> None:
        self.places: dict[str, set[str]] = {}

    def add(self, place: str, text: object) -> None:
        if isinstance(text, str) and text.strip():
            self.places.setdefault(text, set()).add(place)

    def harvest(self, place: str, sink: RecordingSink) -> None:
        for action, data in sink.messages:
            if action == "status":
                self.add(f"{place}:status", data["text"])
            elif action == "error":
                self.add(f"{place}:error", data["message"])
            elif action == "added":
                self.add(f"{place}:added.instructions", data["instructions"])
                for text in data["warnings"]:
                    self.add(f"{place}:added.warnings", text)
            elif action == "fusion_done":
                self.add(f"{place}:fusion_done.message", data["message"])
                for text in data["warnings"]:
                    self.add(f"{place}:fusion_done.warnings", text)


@dataclass(frozen=True)
class Nested:
    """A sentence that quotes the reason of a failed operation; the reason is translated too."""

    text: str
    reason: str


class RecordingSink:
    def __init__(self) -> None:
        self.raw: list[tuple[str, str]] = []

    def send(self, action: str, data: str) -> None:
        self.raw.append((action, data))

    @property
    def messages(self) -> list[tuple[str, dict[str, Any]]]:
        return [(action, json.loads(data)) for action, data in self.raw]


class SyncRunner:
    def run(self, fn: Callable[[], Any], on_done: Callable[[JobResult[Any]], None]) -> None:
        try:
            result: JobResult[Any] = JobResult(value=fn())
        except Exception as exc:
            result = JobResult(error=exc)
        on_done(result)


class HeldRunner:
    """Keeps the job until `finish()`, like a worker that has not completed yet."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def run(self, fn: Callable[[], Any], on_done: Callable[[JobResult[Any]], None]) -> None:
        self.pending.append(lambda: SyncRunner().run(fn, on_done))

    def finish(self) -> None:
        pending, self.pending = self.pending, []
        for complete in pending:
            complete()


class DeferQueue:
    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []
        self.error: Exception | None = None

    def __call__(self, fn: Callable[[], None]) -> None:
        if self.error is not None:
            raise self.error
        self.pending.append(fn)

    def run_all(self) -> None:
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()


_PART_CACHE: dict[str, Part] = {}


def synthetic_part(name: str = "r0603") -> Part:
    if name not in _PART_CACHE:
        document = json.loads((SYNTH / f"{name}.json").read_text(encoding="utf-8"))
        _PART_CACHE[name] = parse_part(document)
    return _PART_CACHE[name]


def write_part_lbr(directory: Path, lcsc_id: str, mpn: str) -> Path:
    part = dataclasses.replace(synthetic_part(), lcsc_id=lcsc_id, mpn=mpn)
    return write_library(directory / f"{lcsc_id}.lbr", [part])


class FakeApi:
    """The Fusion facade of the flow. A failure is an exception stored under the method name."""

    def __init__(self) -> None:
        self.libraries: dict[str, str] = {}
        self.failures: dict[str, BaseException] = {}
        self.hooks: dict[str, Callable[[], None]] = {}
        self._ids = itertools.count(1)

    def _enter(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook is not None:
            hook()
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def find_library(self, file_id: str | None, name: str) -> LibraryRef | None:
        self._enter("find_library")
        if file_id is not None and file_id in self.libraries:
            return LibraryRef(file_id, self.libraries[file_id])
        for known_id, known_name in self.libraries.items():
            if known_name == name:
                return LibraryRef(known_id, known_name)
        return None

    def create_library_from_part(self, lbr_path: Path, name: str) -> LibraryRef:
        self._enter("create_library_from_part")
        file_id = f"urn:lib:{next(self._ids)}"
        self.libraries[file_id] = name
        return LibraryRef(file_id, name)

    def open_library(self, ref: LibraryRef) -> None:
        self._enter("open_library")

    def merge_part(self, lbr_path: Path, deviceset: str) -> None:
        self._enter("merge_part")

    def save_active(self, description: str) -> None:
        self._enter("save_active")

    def create_3d_package(self, lbr_path: Path) -> None:
        self._enter("create_3d_package")

    def import_step(self, step_path: Path) -> None:
        self._enter("import_step")


class FlowAdder:
    """The `LibraryAdder` of the controller: a real flow over a fake Fusion."""

    def __init__(self, api: FakeApi, store: StateStore) -> None:
        self.api = api
        self.store = store

    def add(self, request: FlowRequest, status: Callable[[str, str], None]) -> FlowResult:
        return FusionLibraryFlow(self.api, self.store, status, lambda _text: None).run(request)


class FlowEnv:
    """A flow over a fake Fusion, a real registry file and real part libraries."""

    def __init__(self, root: Path, seen: Seen, place: str) -> None:
        self.root = root
        self.seen = seen
        self.place = place
        self.api = FakeApi()
        self.store = StateStore(root / "registry" / "state.json")
        self.flow = FusionLibraryFlow(self.api, self.store, self._status, lambda _text: None)

    def _status(self, level: str, text: str) -> None:
        self.seen.add(f"{self.place}:status", text)

    def request(
        self,
        lcsc_id: str = "C100",
        mpn: str = "MPN-100",
        *,
        step: bool = False,
        missing_step: bool = False,
        lbr_text: str | None = None,
    ) -> FlowRequest:
        out = self.root / "out"
        if lbr_text is None:
            lbr = write_part_lbr(out, lcsc_id, mpn)
        else:
            lbr = out / f"{lcsc_id}.lbr"
            lbr.parent.mkdir(parents=True, exist_ok=True)
            lbr.write_text(lbr_text, encoding="utf-8")
        step_path: Path | None = None
        if step or missing_step:
            step_path = out / f"{lcsc_id}.placed.step"
            if not missing_step:
                step_path.write_text("ISO-10303-21;", encoding="utf-8")
        return FlowRequest(lcsc_id, mpn, lbr, step_path)

    def run(self, request: FlowRequest) -> FlowResult:
        result = self.flow.run(request)
        self.seen.add(f"{self.place}:message", result.message)
        for text in result.warnings:
            self.seen.add(f"{self.place}:warning", text)
        return result


def flow_scenarios(root: Path, seen: Seen, reasons: Sequence[str]) -> list[Nested]:
    """Success, repeat, merge, 3D and every failure branch of the flow, with a real registry."""
    nested: list[Nested] = []

    def env(name: str) -> FlowEnv:
        return FlowEnv(root / name, seen, f"flow.{name}")

    # Success paths, with and without an MPN, with and without 3D.
    e = env("create")
    e.run(e.request("C100", ""))
    e.run(e.request("C100", ""))  # present now
    e = env("merge")
    e.run(e.request("C100", "MPN-100"))
    e.run(e.request("C200", "MPN-200"))
    e.run(e.request("C200", "MPN-200"))
    e = env("three_d")
    e.run(e.request("C100", "MPN-100", step=True))  # created, 3D started
    e.run(e.request("C200", "MPN-200", step=True))  # merged, 3D started
    e.run(e.request("C200", "MPN-200", step=True))  # already there, reopened, 3D started
    e.run(e.request("C300", "MPN-300", missing_step=True))  # merged, STEP file missing

    # The part file itself is wrong.
    e = env("bad_lbr")
    e.run(FlowRequest("C100", "M", e.root / "out" / "missing.lbr"))
    e.run(e.request("C101", "M", lbr_text="<eagle"))
    e.run(e.request("C102", "M", lbr_text="<eagle/>"))
    e.run(e.request("C103", "M", lbr_text="<eagle><drawing><library/></drawing></eagle>"))
    package = "<packages><package/></packages>"
    named = f'<devicesets><deviceset name="X"/></devicesets>{package}'
    e.run(
        e.request(
            "C104", "M", lbr_text=f"<eagle><drawing><library>{named}</library></drawing></eagle>"
        )
    )

    # The registry is unusable or says something the flow must warn about.
    broken_registries: dict[str, bytes | None] = {
        "not_json": b"{nope",
        "not_utf8": b"\xff\xfe\xfa",
        "list": b"[]",
        "version": b'{"version": 2}',
        "dropped": b'{"version": 1, "parts": {"C9": 1}, "packages": {"X": 5}}',
        "directory": None,
    }
    for name, content in broken_registries.items():
        e = env(f"registry_{name}")
        e.store.path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            e.store.path.mkdir()  # a directory where the file should be: it cannot be read
        else:
            e.store.path.write_bytes(content)
        e.run(e.request())
    e = env("registry_unwritable")
    e.store.path.parent.parent.mkdir(parents=True, exist_ok=True)
    e.store.path.parent.write_text("a file where the registry folder should be", encoding="utf-8")
    e.run(e.request())
    e = env("stale")  # the registry names a library that Fusion no longer has
    e.store.save(
        LibraryState("urn:lib:gone", LIBRARY_NAME).with_part(
            PartRecord("C9", "OLD", "PKG", "abc", 1.0), {"PKG": "abc"}
        )
    )
    e.run(e.request())
    e = env("conflicts")
    first = e.request("C100", "MPN-100")
    info = inspect_part_lbr(first.lbr_path)
    package_name = next(iter(info.packages))
    e.store.save(
        LibraryState("urn:lib:1", LIBRARY_NAME)
        .with_part(PartRecord("C1", info.deviceset, package_name, "other", 1.0), {})
        .with_part(PartRecord("C2", "ELSE", "ELSE", "else", 2.0), {package_name: "different"})
    )
    e.api.libraries["urn:lib:1"] = LIBRARY_NAME
    e.run(first)  # same footprint name, another shape
    e.store.save(
        LibraryState("urn:lib:1", LIBRARY_NAME).with_part(
            PartRecord("C1", info.deviceset, package_name, "x", 1.0), info.packages
        )
    )
    e.run(e.request("C101", "MPN-100"))  # the device set name is taken by C1

    # Busy: a second run while the first one is inside a Fusion call.
    e = env("busy")
    request = e.request()
    e.api.hooks["create_library_from_part"] = lambda: e.run(request)
    e.run(request)
    e = env("crash")
    e.store.load = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
    e.run(e.request())

    # A Fusion operation fails with every reason the adapter can give, and with a plain one.
    all_reasons = [*reasons, "boom", "RuntimeError"]
    for operation in ("find_library", "create_library_from_part"):
        e = env(f"fail_{operation}")
        request = e.request()
        for reason in all_reasons:
            e.api.failures[operation] = RuntimeError(reason)
            result = e.run(request)
            nested.append(Nested(result.message, reason))
    for operation in ("open_library", "merge_part", "save_active"):
        e = env(f"fail_{operation}")
        e.run(e.request("C100", "MPN-100"))
        request = e.request("C200", "MPN-200")
        for reason in all_reasons:
            e.api.failures[operation] = RuntimeError(reason)
            result = e.run(request)
            nested.append(Nested(result.message, reason))
    for operation in ("create_3d_package", "import_step"):
        e = env(f"fail_{operation}")
        request = e.request("C100", "MPN-100", step=True)
        for reason in all_reasons:
            e.api.failures[operation] = RuntimeError(reason)
            result = e.run(request)
            nested.append(Nested(result.warnings[-1], reason))
    e = env("fail_reopen")  # the part is there already, so the 3D step reopens the library
    request = e.request("C100", "MPN-100", step=True)
    e.run(request)
    for reason in all_reasons:
        e.api.failures["open_library"] = RuntimeError(reason)
        result = e.run(request)
        nested.append(Nested(result.warnings[-1], reason))
    return nested


class FakeService:
    """`PartService` that writes real part libraries."""

    def __init__(self) -> None:
        self.search_results: list[SearchResult] = []
        self.search_error: Exception | None = None
        self.add_error: Exception | None = None
        self.warnings: tuple[str, ...] = ()
        self.produce_models = True
        self.outside: Path | None = None  # write the .lbr here instead of the output folder

    def search(self, query: str, limit: int) -> list[SearchResult]:
        if self.search_error is not None:
            raise self.search_error
        return list(self.search_results)

    def add_part(self, lcsc_id: str, out_dir: Path, with_3d: bool = False) -> AddedPart:
        if self.add_error is not None:
            raise self.add_error
        directory = self.outside if self.outside is not None else out_dir
        lbr = write_part_lbr(directory, lcsc_id, "RP2040")
        step: Path | None = None
        placed: Path | None = None
        if with_3d and self.produce_models:
            step = directory / f"{lcsc_id}.step"
            placed = directory / f"{lcsc_id}.placed.step"
            step.write_text("ISO-10303-21;", encoding="utf-8")
            placed.write_text("ISO-10303-21;", encoding="utf-8")
        return AddedPart(lbr, "RP2040", self.warnings, step, placed)


class FakeImporter:
    def __init__(self) -> None:
        self.outcome = ImportOutcome(opened=True, message="sent")
        self.error: Exception | None = None

    def open_library(self, path: Path) -> ImportOutcome:
        if self.error is not None:
            raise self.error
        return self.outcome


@dataclass
class CtlEnv:
    ctl: Controller
    service: FakeService
    importer: FakeImporter
    sink: RecordingSink
    deferred: DeferQueue
    api: FakeApi
    out_dir: Path
    opened: list[Path]

    def call(self, action: str, payload: Any = None) -> str:
        data = payload if isinstance(payload, str) else json.dumps(payload or {})
        return self.ctl.handle(action, data)


def make_ctl(root: Path, *, fusion: bool = False, runner: Any = None) -> CtlEnv:
    service = FakeService()
    importer = FakeImporter()
    sink = RecordingSink()
    deferred = DeferQueue()
    api = FakeApi()
    opened: list[Path] = []
    adder = FlowAdder(api, StateStore(root / "registry" / "state.json"))
    ctl = Controller(
        service,
        importer,
        sink,
        runner if runner is not None else SyncRunner(),
        lambda _text: None,
        out_dir=root / "libs",
        folder_opener=opened.append,
        library=adder if fusion else None,
        defer=deferred if fusion else None,
    )
    return CtlEnv(ctl, service, importer, sink, deferred, api, root / "libs", opened)


def hit(lcsc_id: str) -> SearchResult:
    return SearchResult(lcsc_id=lcsc_id, mpn="MPN", manufacturer="M", package="P", description="D")


def controller_scenarios(root: Path, seen: Seen, reasons: Sequence[str]) -> None:
    """Every request the page can send, answered the good way and the bad way."""

    def done(env: CtlEnv, place: str) -> None:
        seen.harvest(place, env.sink)

    # Without the hand-off to Fusion.
    env = make_ctl(root / "plain")
    env.call("ready")
    for count in (1, 2, 5, 0):
        env.service.search_results = [hit(f"C{n}") for n in range(1, count + 1)]
        env.call("search", {"query": "0603 100nF"})
    env.call("search", {"query": "c2040"})
    for payload in ({}, {"query": ""}, {"query": "x" * 101}, {"query": "a\x07b"}, "{nope", "[]"):
        env.call("search", payload)
    env.call("bogus", {})
    for payload in ({}, {"lcsc_id": "x"}, {"lcsc_id": "C1", "with_3d": "yes"}):
        env.call("add", payload)
    env.call("add_to_fusion", {"lcsc_id": "C9"})
    env.call("add_to_fusion", {"lcsc_id": "nope"})
    for payload in ({}, {"tag": 5}, {"tag": "v01.2.3"}, {"tag": "1.2.3"}):
        env.call("dismiss_update", payload)  # the one Python sentence of the update notice
    for payload in ({}, {"enabled": "yes"}):
        env.call("set_update_check", payload)
    for error in (
        PartNotFoundError("EasyEDA has no data for C1 (HTTP 404)."),
        OSError("[WinError 5] Access is denied"),
        RuntimeError("secret internals"),
    ):
        env.service.search_error = error
        env.call("search", {"query": "x"})
        env.service.search_error = None
        env.service.add_error = error
        env.call("add", {"lcsc_id": "C1"})
        env.service.add_error = None
    env.call("add", {"lcsc_id": "C100"})  # saved to a path, no hand-off available
    env.call("add", {"lcsc_id": "C101", "fusion": True})  # asked for, but not available
    env.service.warnings = ("C102: pad 41 is shared by 9 pads",)
    env.call("add", {"lcsc_id": "C102"})
    env.service.warnings = tuple(f"C103: note {n}" for n in range(3))
    env.call("add", {"lcsc_id": "C103"})
    env.service.warnings = ()
    env.call("add", {"lcsc_id": "C104", "with_3d": True})
    env.service.produce_models = False
    env.call("add", {"lcsc_id": "C105", "with_3d": True})
    env.service.produce_models = True
    env.call("add_to_fusion", {"lcsc_id": "C100"})  # the feature is off in this session
    folder, library = env.out_dir, env.out_dir / "C100.lbr"
    for payload in (
        {},
        {"path": str(root)},
        {"path": str(folder / "missing" / "file.txt")},
        {"path": str(library)},
    ):
        env.call("open_folder", payload)
    env.ctl._folder_opener = lambda _path: (_ for _ in ()).throw(OSError("no explorer"))
    env.call("open_folder", {"path": str(folder)})
    for payload in ({}, {"path": str(root / "x.lbr")}, {"path": str(folder / "none.lbr")}):
        env.call("open_library", payload)
    outcomes = [
        ImportOutcome(True, "sent"),
        ImportOutcome(False, ""),
        ImportOutcome(False, "Fusion rejected the open command (boom)"),
        *(ImportOutcome(False, reason) for reason in reasons),
    ]
    for outcome in outcomes:
        env.importer.outcome = outcome
        env.call("open_library", {"path": str(library)})
    env.importer.error = RuntimeError("importer broke")
    env.call("open_library", {"path": str(library)})
    done(env, "ctl.plain")

    # With the hand-off: create, merge, 3D, repeat and the things that go wrong around it.
    env = make_ctl(root / "fusion", fusion=True)
    env.call("add", {"lcsc_id": "C200", "fusion": True})
    env.deferred.run_all()
    env.call("add", {"lcsc_id": "C201", "fusion": True, "with_3d": True})
    env.deferred.run_all()
    env.call("add_to_fusion", {"lcsc_id": "C201"})  # again: already there, 3D reopened
    env.deferred.run_all()
    env.call("add_to_fusion", {"lcsc_id": "C200"})
    env.deferred.run_all()
    env.api.failures["merge_part"] = RuntimeError("boom")
    env.call("add", {"lcsc_id": "C202", "fusion": True})
    env.deferred.run_all()
    env.api.failures.clear()
    env.call("add", {"lcsc_id": "C203"})
    env.call("add_to_fusion", {"lcsc_id": "C203"})
    env.call("add_to_fusion", {"lcsc_id": "C203"})  # queued already: busy
    env.deferred.run_all()
    env.deferred.error = RuntimeError("queue is gone")
    env.call("add_to_fusion", {"lcsc_id": "C200"})
    env.deferred.error = None
    env.ctl._library = type("Crashing", (), {"add": lambda self, r, s: 1 / 0})()  # type: ignore[assignment]
    env.call("add_to_fusion", {"lcsc_id": "C200"})
    env.deferred.run_all()
    done(env, "ctl.fusion")

    env = make_ctl(root / "outside", fusion=True)
    env.service.outside = root / "elsewhere"
    env.call("add", {"lcsc_id": "C300", "fusion": True})
    env.call("add_to_fusion", {"lcsc_id": "C300"})
    done(env, "ctl.outside")

    held = HeldRunner()
    env = make_ctl(root / "busy", runner=held)
    env.call("search", {"query": "x"})
    env.call("search", {"query": "y"})  # busy
    held.finish()
    done(env, "ctl.busy")


# The core and the HTTP client, against scripted answers ------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.headers = {"content-type": "application/json"}
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


Outcome = tuple[int, bytes | str] | BaseException


class ScriptedOpener:
    """Stands in for urllib's opener, so the real transport code produces its real messages."""

    def __init__(self, handler: Callable[[str, str], Outcome]) -> None:
        self._handler = handler

    def open(self, request: Any, data: bytes | None = None, timeout: float = 0.0) -> Any:
        outcome = self._handler(request.get_method(), request.full_url)
        if isinstance(outcome, BaseException):
            raise outcome
        status, body = outcome
        return _FakeHttpResponse(status, body if isinstance(body, bytes) else body.encode())


def make_client(handler: Callable[[str, str], Outcome], root: Path) -> EasyEdaClient:
    return EasyEdaClient(
        transport=UrllibTransport(opener=ScriptedOpener(handler)),
        cache=DiskCache(root / "cache", 0.0),
        limiter=RateLimiter(0.0),
        sleep=lambda _seconds: None,
        retry_attempts=1,
        retry_base_delay_s=0.0,
    )


def client_scenarios(root: Path, seen: Seen) -> list[str]:
    """Network failures and bad answers through `CorePartService`; returns EasyEDA data notes."""
    notes: list[str] = []
    ok_search = (200, json.dumps({"success": True, "result": [{"uuid": "abc"}]}))
    nothing = (200, json.dumps({"success": True, "result": []}))
    doc = (SYNTH / "box3d_rot90.json").read_text(encoding="utf-8")
    sot23 = (SYNTH / "sot23.json").read_text(encoding="utf-8")
    model_uuid = json.loads((SYNTH / "box3d_rot90.expected.json").read_text(encoding="utf-8"))[
        "model3d"
    ]["uuid"]

    def everything(outcome: Outcome) -> Callable[[str, str], Outcome]:
        return lambda method, url: outcome

    def part_with_model(model: Outcome, document: str = doc) -> Callable[[str, str], Outcome]:
        def handler(method: str, url: str) -> Outcome:
            if "searchByNumbers" in url:
                return ok_search
            if url.endswith("/api/components/abc"):
                return 200, document
            if "modules.easyeda.com" in url:
                assert model_uuid in url
                return model
            return 404, b""  # the datasheet lookup

        return handler

    def products(status: int, body: str) -> Callable[[str, str], Outcome]:
        return lambda method, url: nothing if "searchByNumbers" in url else (status, body)

    def jlcpcb(status: int, body: str) -> Callable[[str, str], Outcome]:
        return lambda method, url: (status, body)

    def lookup(service: CorePartService) -> object:
        return service.search("C2040", 20)

    def phrase(service: CorePartService) -> object:
        return service.search("0603 100nF", 20)

    def add_3d(service: CorePartService) -> object:
        part = service.add_part("C9000016", root / "out", with_3d=True)
        notes.extend(part.warnings)
        return part

    def add_plain(service: CorePartService) -> object:
        part = service.add_part("C9000004", root / "out", with_3d=True)
        notes.extend(part.warnings)
        return part

    dns = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
    cases: list[
        tuple[str, Callable[[str, str], Outcome], Callable[[CorePartService], object], int]
    ] = [
        ("dns", everything(dns), lookup, 1),
        ("timeout", everything(TimeoutError()), lookup, 1),
        ("tls", everything(ssl.SSLError("certificate verify failed")), lookup, 1),
        ("drop", everything(http.client.RemoteDisconnected("closed")), lookup, 1),
        ("503", everything((503, b"")), lookup, 1),
        ("blocked", everything((403, b"")), lookup, 2),
        ("404", products(404, ""), lookup, 1),
        ("no_data", everything((200, '{"success": false}')), lookup, 1),
        ("not_json", everything((200, "<html>")), lookup, 1),
        ("418", products(418, ""), lookup, 1),
        ("jlc_503", jlcpcb(503, ""), phrase, 1),
        ("jlc_404", jlcpcb(404, ""), phrase, 1),
        ("jlc_list", jlcpcb(200, "[]"), phrase, 1),
        ("jlc_nodata", jlcpcb(200, '{"code": 5, "message": "bad", "data": null}'), phrase, 1),
        ("jlc_info", jlcpcb(200, '{"data": {"componentPageInfo": 5}}'), phrase, 1),
        ("jlc_items", jlcpcb(200, '{"data": {"componentPageInfo": {"list": 5}}}'), phrase, 1),
        ("model_404", part_with_model((404, b"")), add_3d, 1),
        ("model_html", part_with_model((200, "<html>sorry</html>")), add_3d, 1),
        ("model_503", part_with_model((503, b"")), add_3d, 1),
        ("model_blocked", part_with_model((403, b"")), add_3d, 2),
        ("model_dns", part_with_model(dns), add_3d, 1),
        ("no_model", part_with_model((404, b""), sot23), add_plain, 1),
    ]
    for name, handler, action, repeat in cases:
        service = CorePartService(
            client=make_client(handler, root / name), state_path=root / name / "state.json"
        )
        for _ in range(repeat):
            try:
                action(service)
            except LcscError as exc:
                seen.add(f"client.{name}", str(exc))
    for text in notes:
        seen.add("client.warnings", text)
    return notes


# The corpus -------------------------------------------------------------------------------------

# English input and the exact Polish text the table must give. Written by hand and reviewed.
INSTRUCTIONS_TAIL_PL = (
    "wybierz File > Open > Open from my computer, wskaż plik .lbr, a potem zapisz go w projekcie "
    "przez File > Save, aby pojawił się w Library Manager."
)
PAIRS: tuple[tuple[str, str], ...] = (
    ("Found 1 part(s) for 'C2040'.", "Znaleziono 1 część dla 'C2040'."),
    ("Found 2 part(s) for '0603 100nF'.", "Znaleziono 2 części dla '0603 100nF'."),
    ('Found 4 part(s) for "it\'s".', 'Znaleziono 4 części dla "it\'s".'),
    ("Found 5 part(s) for 'x'.", "Znaleziono 5 części dla 'x'."),
    ("Found 22 part(s) for 'x'.", "Znaleziono 22 części dla 'x'."),
    ("Searching for 'C2040'...", "Wyszukiwanie 'C2040'..."),
    (
        "Invalid request: tag must be a release tag such as v1.2.3.",
        "Nieprawidłowe żądanie: tag musi być tagiem wydania, na przykład v1.2.3.",
    ),
    (
        "Invalid request: enabled must be true or false.",
        "Nieprawidłowe żądanie: enabled musi mieć wartość true albo false.",
    ),
    (
        "No parts found for 'zzz'. Try an LCSC number such as C2040 or fewer words.",
        "Nie znaleziono części dla 'zzz'. Spróbuj podać numer LCSC, np. C2040, albo użyj "
        "mniejszej liczby słów.",
    ),
    (
        "Downloading C2040 and building the library and the 3D model...",
        "Pobieranie C2040 i budowanie biblioteki oraz modelu 3D...",
    ),
    (
        r"Saved C2040 (RP2040) to C:\Users\K\libs\C2040.lbr",
        r"Zapisano C2040 (RP2040) do pliku C:\Users\K\libs\C2040.lbr",
    ),
    (
        r"Saved C2040 and its 3D model to C:\libs",
        r"Zapisano C2040 wraz z modelem 3D do folderu C:\libs",
    ),
    ("Saved C2040 (RP2040) with 1 warning(s).", "Zapisano C2040 (RP2040) z 1 ostrzeżeniem."),
    ("Saved C2040 (RP2040) with 3 warning(s).", "Zapisano C2040 (RP2040) z 3 ostrzeżeniami."),
    ("Saved C2040 (RP2040) with 7 warning(s).", "Zapisano C2040 (RP2040) z 7 ostrzeżeniami."),
    (
        "Saved C2040 without a 3D model: none was produced for this part. "
        "See the warnings on the result.",
        "Zapisano C2040 bez modelu 3D: dla tej części nie powstał żaden. "
        "Zobacz ostrzeżenia przy wyniku.",
    ),
    (
        build_instructions(True),
        'Naciśnij "Dodaj do biblioteki Fusion", aby dołączyć część do biblioteki '
        "LCSC2Fusion Library. Aby użyć tylko tej części osobno, naciśnij "
        '"Otwórz jako osobną bibliotekę", a potem w edytorze użyj File > Save i wybierz '
        "projekt, aby biblioteka pojawiła się w Library Manager. Jeśli to nie zadziała, "
        f"zrób to ręcznie: {INSTRUCTIONS_TAIL_PL}",
    ),
    (
        build_instructions(False),
        'Naciśnij "Otwórz jako osobną bibliotekę", a potem w edytorze użyj File > Save i '
        "wybierz projekt, aby biblioteka pojawiła się w Library Manager. Jeśli to nie "
        f"zadziała, zrób to ręcznie: {INSTRUCTIONS_TAIL_PL}",
    ),
    (
        controller.FUSION_UNAVAILABLE_TEXT,
        "Dodawanie do biblioteki Fusion jest niedostępne w tej sesji. Plik biblioteki "
        'został zapisany; użyj zamiast tego "Otwórz jako osobną bibliotekę".',
    ),
    (
        "C25804 was not added in this session. Press Add to library for it first.",
        'Część C25804 nie została dodana w tej sesji. Najpierw naciśnij "Dodaj do biblioteki" '
        "przy tej części.",
    ),
    (
        describe_open_outcome(
            ImportOutcome(False, "Library file not found: C:\\x\\C2040.lbr"), "C2040.lbr"
        )[1],
        "Fusion nie otworzył C2040.lbr (Nie znaleziono pliku biblioteki: C:\\x\\C2040.lbr). "
        f"Otwórz go ręcznie: {INSTRUCTIONS_TAIL_PL}",
    ),
    (
        "Added C2040 (RP2040) to the LCSC2Fusion Library. The 3D package editor is open: "
        "check the position of the model, then click Finish, Save, OK.",
        "Dodano część C2040 (RP2040) do biblioteki LCSC2Fusion Library. Edytor pakietu 3D "
        "jest otwarty: sprawdź położenie modelu, a potem kliknij Finish, Save, OK.",
    ),
    (
        "Created the LCSC2Fusion Library with C2040 (RP2040). The 3D package could not be "
        "created, see the warnings.",
        "Utworzono bibliotekę LCSC2Fusion Library z częścią C2040 (RP2040). "
        "Nie udało się utworzyć pakietu 3D, zobacz ostrzeżenia.",
    ),
    (
        "C2040 (RP2040) is already in the LCSC2Fusion Library, so it was not added again. "
        "The 3D package editor is open: check the position of the model, then click Finish, "
        "Save, OK.",
        "Część C2040 (RP2040) jest już w bibliotece LCSC2Fusion Library, więc nie została "
        "dodana ponownie. Edytor pakietu 3D jest otwarty: sprawdź położenie modelu, a potem "
        "kliknij Finish, Save, OK.",
    ),
    (
        "3D: could not create the 3D package: Fusion could not save the new 3D package design..",
        "3D: nie udało się utworzyć pakietu 3D: Fusion nie mógł zapisać nowego projektu "
        "pakietu 3D.",
    ),
    (
        "3D: could not open the library: RuntimeError.",
        "3D: nie udało się otworzyć biblioteki: RuntimeError.",
    ),
    (
        r"3D: could not import C2040.placed.step: Fusion imported nothing from "
        r"C2040.placed.step.. You can import C:\x\C2040.placed.step by hand.",
        r"3D: nie udało się zaimportować C2040.placed.step: Fusion nic nie zaimportował z "
        r"C2040.placed.step. Możesz zaimportować C:\x\C2040.placed.step ręcznie.",
    ),
    (
        "Could not merge the part: Fusion cannot find the library 'LCSC2Fusion Library' any "
        "more.. The library may be open with unsaved changes: close it without saving before "
        "you try again.",
        "Nie udało się dołączyć części do biblioteki: Fusion nie może już znaleźć biblioteki "
        "'LCSC2Fusion Library'. Biblioteka może być otwarta z niezapisanymi zmianami: zamknij "
        "ją bez zapisywania, zanim spróbujesz ponownie.",
    ),
    (
        "Could not create the library: Fusion has no project to keep the library in. "
        "Sign in to a hub..",
        "Nie udało się utworzyć biblioteki: Fusion nie ma projektu, w którym można "
        "przechowywać bibliotekę. Zaloguj się do huba.",
    ),
    (
        "easyeda.com blocked the request (HTTP 403). Such blocks usually clear on their own; "
        "try again in about 300 s.",
        "easyeda.com zablokował żądanie (HTTP 403). Takie blokady zwykle ustępują same; "
        "spróbuj ponownie za około 300 s.",
    ),
    (
        "Requests to easyeda.com are paused after it blocked us. Try again in about 120 s.",
        "Żądania do easyeda.com są wstrzymane, bo serwer nas zablokował. Spróbuj ponownie "
        "za około 120 s.",
    ),
    (
        "Cannot reach easyeda.com: [Errno 11001] getaddrinfo failed",
        "Nie można połączyć się z easyeda.com: [Errno 11001] getaddrinfo failed",
    ),
    (
        "TLS error while talking to easyeda.com: certificate verify failed",
        "Błąd TLS podczas komunikacji z easyeda.com: certificate verify failed",
    ),
    ("EasyEDA has no data for C2040 (HTTP 404).", "EasyEDA nie ma danych dla C2040 (HTTP 404)."),
    (
        "easyeda.com returned unexpected HTTP status 418.",
        "easyeda.com zwrócił nieoczekiwany status HTTP 418.",
    ),
    ("easyeda.com returned HTTP 503", "easyeda.com zwrócił kod HTTP 503"),
    (
        "C2040: 3D model not written: 3D model abc not found on EasyEDA (HTTP 404).",
        "C2040: nie zapisano modelu 3D: Model 3D abc nie został znaleziony w EasyEDA (HTTP 404).",
    ),
    (
        "File system error: [WinError 5] Access is denied",
        "Błąd systemu plików: [WinError 5] Access is denied",
    ),
    (
        "The library registry C:\\x\\state.json is not valid JSON (Expecting value: line 1 "
        "column 1 (char 0)); starting with an empty registry.",
        "Rejestr biblioteki C:\\x\\state.json nie jest poprawnym plikiem JSON (Expecting value: "
        "line 1 column 1 (char 0)); zaczynam od pustego rejestru.",
    ),
    (
        "Ready. Libraries are saved to C:\\Users\\K\\LCSC2Fusion\\libraries",
        "Gotowe. Biblioteki są zapisywane w C:\\Users\\K\\LCSC2Fusion\\libraries",
    ),
    (
        "The search text is too long (limit 100 characters).",
        "Tekst wyszukiwania jest za długi (limit: 100 znaków).",
    ),
    (
        "The request to Fusion failed. Check that Fusion is running, then try again.",
        "Żądanie do Fusion nie powiodło się. Sprawdź, czy Fusion jest uruchomiony, i spróbuj "
        "ponownie.",
    ),
    (
        "Another request is still running. Please wait for it.",
        "Inne żądanie jest jeszcze w toku. Poczekaj, aż się zakończy.",
    ),
    ("Fusion did not report back. Try again.", "Fusion nie odpowiedział. Spróbuj ponownie."),
)
# Texts that quote EasyEDA data or other data of ours: no pattern, they stay English.
DATA_TEXTS: tuple[str, ...] = (
    "C1: pad 41 is shared by 9 pads",
    "C2040: pad 'A1' has no number, skipped",
    "C2040: 3D model: footprint has 2 SVGNODE shapes, using the first",
    "C2040: Cannot place STEP: the 3D placement contains non-finite numbers",
    "Something we never wrote",
    "",
)
BUTTON_TEXTS = {
    "Add to Fusion library": "Dodaj do biblioteki Fusion",
    "Open as a separate library": "Otwórz jako osobną bibliotekę",
    "Add to library": "Dodaj do biblioteki",
}


@dataclass(frozen=True)
class Corpus:
    table: Table
    static: StaticScan
    seen: Seen
    nested: list[Nested]
    notes: list[str]
    results: dict[str, Result]

    def unmatched(self, texts: Iterable[str]) -> list[str]:
        return sorted({text for text in texts if self.results[text].index is None})


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    if NODE is None:
        pytest.skip("node is not installed")
    root = tmp_path_factory.mktemp("i18n")
    table = load_table()
    static = scan_sources()
    reasons = static.reasons()
    seen = Seen()
    nested = flow_scenarios(root / "flow", seen, reasons)
    controller_scenarios(root / "ctl", seen, reasons)
    notes = client_scenarios(root / "client", seen)
    texts: list[str] = [*static.forms(), *seen.places, *(pair[0] for pair in PAIRS), *DATA_TEXTS]
    texts.extend(item.text for item in nested)
    texts.extend(item.reason for item in nested)
    results = translate(texts)
    return Corpus(table, static, seen, nested, notes, results)


@pytest.fixture(scope="module")
def table() -> Table:
    if NODE is None:
        pytest.skip("node is not installed")
    return load_table()


# Pattern hygiene ---------------------------------------------------------------------------------


@needs_node
def test_the_table_is_filled(table: Table) -> None:
    assert len(table.entries) >= 100


@needs_node
def test_every_entry_has_a_pattern_and_a_polish_template(table: Table) -> None:
    for entry in table.entries:
        assert entry.source is not None, f"#{entry.index} has no RegExp in `re`"
        assert set(entry.keys) <= {"re", "pl"}, f"{entry.describe()} has unknown keys {entry.keys}"
        assert "pl" in entry.keys, f"{entry.describe()} has no `pl` template"
        assert isinstance(entry.pl, str | dict), f"{entry.describe()} template is {entry.pl!r}"
        assert entry.templates, f"{entry.describe()} has no template text"


@needs_node
def test_patterns_are_anchored_named_and_not_global(table: Table) -> None:
    for entry in table.entries:
        assert entry.source is not None and entry.flags is not None
        assert "g" not in entry.flags and "y" not in entry.flags, entry.describe()
        assert entry.source.startswith("^") and entry.source.endswith("$"), entry.describe()
        scan = entry.scan
        assert not scan.top_level_alternation, f"{entry.describe()} has a top level |"
        assert scan.plain_groups == 0, f"{entry.describe()} has a group without a name"
        assert len(set(scan.named)) == len(scan.named), f"{entry.describe()} repeats a group"


@needs_node
def test_no_pattern_is_listed_twice(table: Table) -> None:
    sources = [entry.source for entry in table.entries]
    duplicates = sorted({source for source in sources if sources.count(source) > 1}, key=str)
    assert duplicates == []


@needs_node
def test_templates_only_name_groups_of_their_pattern(table: Table) -> None:
    for entry in table.entries:
        groups = set(entry.scan.named)
        for template in entry.templates:
            if template.startswith("@"):
                continue
            used = {name for name, _modifier in PLACEHOLDER.findall(template)}
            assert used <= groups, f"{entry.describe()} uses {used - groups} in {template!r}"
            leftovers = PLACEHOLDER.sub("", template)
            assert "{" not in leftovers and "}" not in leftovers, (
                f"{entry.describe()}: {template!r}"
            )


@needs_node
def test_templates_keep_every_captured_value(table: Table) -> None:
    """A group that no template shows would drop information (a path, a reason, a count)."""
    for entry in table.entries:
        if isinstance(entry.pl, str) and entry.pl.startswith("@"):
            continue
        for template in entry.templates:
            used = {name for name, _modifier in PLACEHOLDER.findall(template)}
            assert set(entry.scan.named) <= used, (
                f"{entry.describe()} drops {set(entry.scan.named) - used} in {template!r}"
            )


@needs_node
def test_plural_templates_belong_to_a_count_group(table: Table) -> None:
    for entry in table.entries:
        has_count = "n" in entry.scan.named
        if isinstance(entry.pl, dict):
            assert has_count, f"{entry.describe()} has plural forms but no group `n`"
            assert "other" in entry.pl, f"{entry.describe()} has no `other` form"
            assert set(entry.pl) <= {"zero", "one", "two", "few", "many", "other"}
            forms = [form for form in entry.pl.values()]
            assert all("{n}" in form for form in forms), f"{entry.describe()} hides the number"
        else:
            assert not has_count, f"{entry.describe()} has a group `n` but no plural forms"


@needs_node
def test_shared_texts_exist_in_the_dictionary_and_match_their_pattern(table: Table) -> None:
    keyed = [entry for entry in table.entries if isinstance(entry.pl, str) and entry.pl[:1] == "@"]
    assert len(keyed) >= 3
    results = translate(table.en[entry.pl[1:]] for entry in keyed)
    for entry in keyed:
        key = entry.pl[1:]
        assert key in table.pl, f"{entry.describe()} points at {key!r}, missing in pl"
        assert isinstance(table.en[key], str)
        found = results[table.en[key]]
        assert found.index == entry.index, f"{entry.describe()} does not match en[{key!r}]"
        assert found.pl == table.pl[key]


@needs_node
def test_no_dashes_and_no_empty_templates(table: Table) -> None:
    for entry in table.entries:
        for template in entry.templates:
            assert template.strip(), f"{entry.describe()} has an empty template"
            for dash in ("\u2014", "\u2013", "--"):
                assert dash not in template, f"{entry.describe()} has {dash!r} in {template!r}"
        assert "--" not in (entry.source or "")


@needs_node
def test_the_three_d_prefix_is_kept(table: Table) -> None:
    """The page drops the leading "3D: " of a warning, in both languages."""
    for entry in table.entries:
        english = (entry.source or "").startswith("^3D: ")
        for template in entry.templates:
            assert template.startswith("3D: ") == english, f"{entry.describe()}: {template!r}"


@needs_node
def test_templates_are_clean_polish_text(table: Table) -> None:
    letters = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
    used: set[str] = set()
    for entry in table.entries:
        for template in entry.templates:
            assert unicodedata.normalize("NFC", template) == template, entry.describe()
            assert not re.search("[\u00c3\u00c4\u00c5\ufffd]", template), f"mojibake {template!r}"
            assert template == template.strip(), f"{entry.describe()} has outer spaces"
            assert "  " not in template, f"{entry.describe()} has a double space"
            used |= letters & set(template)
    assert set("ąćęłóśż") <= used, f"diacritics never used: {sorted(set('ąćęłóśż') - used)}"


# The texts of the sources, static ----------------------------------------------------------------


def test_the_static_scan_finds_the_message_sinks() -> None:
    scan = scan_sources()
    where = {candidate.location.split(":")[0] for candidate in scan.candidates}
    assert where == {spec.path for spec in SPECS}, "a source file has no message sink at all"
    assert len(scan.candidates) >= 100
    samples = {text for candidate in scan.candidates for text in candidate.samples}
    assert controller.GENERIC_ERROR_TEXT in samples
    assert controller.MANUAL_STEPS in samples
    assert controller.FUSION_UNAVAILABLE_TEXT in samples
    assert build_instructions(True) in samples and build_instructions(False) in samples
    assert "Found 5 part(s) for 'X'." in samples


def test_allowed_fragments_and_skips_still_exist_in_the_sources() -> None:
    scan = scan_sources()
    assert scan.allowed_seen == ALLOWED_FRAGMENTS, "stale or missing ALLOWED_FRAGMENTS entry"
    assert scan.skipped_seen == UNTRANSLATED_ON_PURPOSE, "stale UNTRANSLATED_ON_PURPOSE entry"


@needs_node
def test_every_static_python_text_has_a_pattern(corpus: Corpus) -> None:
    missing: list[str] = []
    for candidate in corpus.static.candidates:
        for forms in candidate.forms:
            if all(corpus.results[text].index is None for text in forms):
                missing.append(f"{candidate.location}: {forms[0]!r}")
    assert not missing, "no pattern in MESSAGES (i18n.js) for:\n" + "\n".join(missing)


# The texts that reach the window at runtime ------------------------------------------------------


@needs_node
def test_the_scenarios_reach_every_kind_of_text(corpus: Corpus) -> None:
    places = {place for found in corpus.seen.places.values() for place in found}
    for kind in (
        "status",
        "error",
        "added.instructions",
        "added.warnings",
        "fusion_done.message",
        "fusion_done.warnings",
        "message",
        "warning",
    ):
        assert any(place.endswith(f":{kind}") for place in places), f"no {kind} text was seen"
    assert len(corpus.seen.places) >= 150
    assert len(corpus.nested) >= 100
    assert len(corpus.static.reasons()) >= 15


# Pieces of sentences the scenarios must produce, so that a scenario that silently stops reaching
# a branch is noticed (the texts of the sources are checked against the table anyway).
RUNTIME_ANCHORS: tuple[str, ...] = (
    "Ready. Libraries are saved to ",
    "Searching for ",
    "Downloading C104 and building the library and the 3D model...",
    "Downloading C100 and building the library...",
    "Found 5 part(s) for ",
    "No parts found for ",
    "Saved C100 (RP2040) to ",
    "Saved C103 (RP2040) with 3 warning(s).",
    "Saved C104 (RP2040) and its 3D model to ",
    "Saved C105 (RP2040) without a 3D model",
    "File system error: ",
    "Unexpected error. The details were written",
    "Adding to the Fusion library is not available",
    'Press "Add to Fusion library" to merge',
    'Press "Open as a separate library", then',
    "Sent C100.lbr to Fusion for opening.",
    "Fusion did not open C100.lbr (",
    "Fusion did not open C100.lbr. Open it manually",
    "Sending C200 to Fusion...",
    "Could not start the Fusion request.",
    "was not added in this session.",
    "Refusing to use a file outside",
    "Refusing to open a folder outside",
    "Refusing to open a file outside",
    "Folder does not exist: ",
    "Could not open the folder: ",
    "No library file given.",
    "Library file does not exist: ",
    "Malformed request from the palette (invalid JSON).",
    "Malformed request from the palette (expected a JSON object).",
    "Enter an LCSC part number",
    "The search text is too long",
    "The search text contains unsupported characters.",
    "Invalid request: with_3d must be true or false.",
    "Invalid request: tag must be a release tag such as v1.2.3.",
    "Invalid request: enabled must be true or false.",
    "is not a valid LCSC part number.",
    "Unknown palette action ",
    "Another request is still running. Please wait for it.",
    "Creating the LCSC2Fusion Library from C100...",
    "Opening the LCSC2Fusion Library...",
    "Merging C200 (MPN-200) into the library...",
    "Saving the library...",
    "Creating the 3D package for C100 (MPN-100)...",
    "Importing the 3D model...",
    "Created the LCSC2Fusion Library with C100.",
    "Added C200 (MPN-200) to the LCSC2Fusion Library.",
    "C200 (MPN-200) is already in the LCSC2Fusion Library, so it was not added again.",
    " The 3D package editor is open: ",
    " The 3D package could not be created, see the warnings.",
    "The 3D model file does not exist (",
    "3D: could not open the library: ",
    "3D: could not create the 3D package: ",
    "3D: could not import C100.placed.step: ",
    " You can import ",
    "Could not look for the library: ",
    "Could not create the library: ",
    "Could not open the library: ",
    "Could not merge the part: ",
    " The library may be open with unsaved changes",
    "was merged into the open library but saving it failed (",
    "Another library operation is still running. Wait for it to finish.",
    "Unexpected error while adding C100 (MPN-100): boom",
    "The part library file does not exist: ",
    "The part library C101.lbr cannot be read: ",
    "C102.lbr is not an Eagle library (no <library> element)",
    "C103.lbr has no named <deviceset>",
    "C104.lbr has a <package> without a name",
    "no longer exists (deleted or moved?)",
    "is already in the LCSC2Fusion Library with a different shape",
    "is already in the library (part C1).",
    "could not be written (",
    " cannot be read (",
    " is not valid JSON (",
    " does not contain a JSON object; ",
    " has version 2, this add-in understands 1; ",
    ": dropped the malformed record for 'C9'",
    ": dropped the malformed package entry 'X'",
    "easyeda.com blocked the request (HTTP 403).",
    "Requests to easyeda.com are paused after it blocked us.",
    "Cannot reach easyeda.com: ",
    "Request to easyeda.com timed out after 30 s",
    "TLS error while talking to easyeda.com: ",
    "Connection to easyeda.com failed: ",
    "easyeda.com returned HTTP 503",
    "easyeda.com returned unexpected HTTP status 418.",
    "EasyEDA has no data for C2040 (HTTP 404).",
    "EasyEDA has no symbol/footprint data for C2040.",
    "easyeda.com returned a response that is not valid JSON: ",
    "jlcpcb.com returned HTTP 503",
    "jlcpcb.com returned unexpected HTTP status 404.",
    "jlcpcb.com returned JSON that is not an object.",
    "jlcpcb.com search response has no data (code 5, message 'bad').",
    "jlcpcb.com search response has an unexpected componentPageInfo.",
    "jlcpcb.com search response has an unexpected result list.",
    ": 3D model not written: 3D model ",
    ": 3D model not written: EasyEDA returned an unusable 3D model ",
    ": 3D model not written: modules.easyeda.com returned HTTP 503",
    ": no 3D model available",
)


@needs_node
def test_the_scenarios_reach_the_branches_they_are_meant_to(corpus: Corpus) -> None:
    texts = list(corpus.seen.places)
    missing = [anchor for anchor in RUNTIME_ANCHORS if not any(anchor in text for text in texts)]
    assert not missing, "no scenario produced:\n" + "\n".join(missing)


# Runtime texts that are data, not our sentences: EasyEDA notes and the like.
def is_data_note(text: str) -> bool:
    return bool(re.match(r"^C\d+: (?!no 3D model available$|3D model not written: )", text))


@needs_node
def test_every_text_that_reaches_the_window_has_a_pattern(corpus: Corpus) -> None:
    missing = [
        f"{text!r}  (seen in {sorted(corpus.seen.places[text])[0]})"
        for text in corpus.unmatched(corpus.seen.places)
        if not is_data_note(text)
    ]
    assert not missing, "no pattern in MESSAGES (i18n.js) for:\n" + "\n".join(missing)


@needs_node
def test_data_notes_of_the_core_are_not_translated(corpus: Corpus) -> None:
    assert corpus.notes, "the core scenario produced no notes at all"
    for text in corpus.seen.places:
        if is_data_note(text) and "3D model not written" not in text:
            result = corpus.results[text]
            assert result.index is None, f"{text!r} is matched by #{result.index}"
            assert result.pl == text


@needs_node
def test_reasons_are_translated_inside_their_sentence(corpus: Corpus) -> None:
    translated = 0
    for item in corpus.nested:
        sentence = corpus.results[item.text].pl
        reason = corpus.results[item.reason].pl
        assert reason in sentence, f"{sentence!r} does not hold {reason!r}"
        translated += reason != item.reason
    assert translated >= 15, "no Fusion reason was translated inside a sentence"


@needs_node
def test_translations_have_no_open_placeholder_and_differ_from_the_english(corpus: Corpus) -> None:
    for text, result in corpus.results.items():
        if result.index is None:
            continue
        assert result.pl != text, f"{text!r} is not translated"
        assert not PLACEHOLDER.search(result.pl), f"open placeholder in {result.pl!r}"
        assert result.pl.startswith("3D: ") == text.strip().startswith("3D: "), text


# Reachability: every entry is used, none hides behind an earlier one -----------------------------

# Entries no sentence of ours reaches in the scenarios above, so a sample is given here.
EXTRA_EXAMPLES: tuple[str, ...] = (
    "Could not look for the library: RuntimeError.",
    "The library registry C:\\x\\state.json has an unexpected structure; "
    "starting with an empty registry.",
)


@needs_node
def test_every_entry_is_reached_by_some_text_and_none_is_shadowed(corpus: Corpus) -> None:
    extra = translate(EXTRA_EXAMPLES)
    results = [*corpus.results.values(), *extra.values()]
    first: dict[int, int] = {}
    anywhere: dict[int, int] = {}
    for result in results:
        for index in result.all:
            anywhere[index] = anywhere.get(index, 0) + 1
        if result.index is not None:
            first[result.index] = first.get(result.index, 0) + 1
    entries = corpus.table.entries
    unused = [entries[i].describe() for i in range(len(entries)) if i not in anywhere]
    shadowed = [entries[i].describe() for i in anywhere if i not in first]
    assert not unused, "no text reaches these entries (dead pattern?):\n" + "\n".join(unused)
    assert not shadowed, "an earlier entry always wins over these:\n" + "\n".join(shadowed)


# What the table gives -----------------------------------------------------------------------------


@needs_node
def test_there_are_enough_explicit_pairs() -> None:
    assert len(PAIRS) >= 25
    assert len({english for english, _polish in PAIRS}) == len(PAIRS)


@needs_node
@pytest.mark.parametrize("number", range(len(PAIRS)))
def test_exact_polish_for_important_messages(corpus: Corpus, number: int) -> None:
    english, polish = PAIRS[number]
    assert corpus.results[english].pl == polish


@needs_node
def test_plural_forms_follow_polish_rules() -> None:
    counts = (1, 2, 4, 5, 12, 22, 25)
    found = translate(f"Found {count} part(s) for 'x'." for count in counts)
    forms = {count: found[f"Found {count} part(s) for 'x'."].pl for count in counts}
    assert forms[1] == "Znaleziono 1 część dla 'x'."
    for count in (2, 4, 22):
        assert forms[count] == f"Znaleziono {count} części dla 'x'."
    for count in (5, 12, 25):
        assert forms[count] == f"Znaleziono {count} części dla 'x'."


@needs_node
def test_button_names_are_quoted_with_the_polish_caption(corpus: Corpus) -> None:
    quoted_in_english = 0
    for text, result in corpus.results.items():
        if result.index is None:
            continue
        for english, polish in BUTTON_TEXTS.items():
            if english in text:
                quoted_in_english += 1
                assert f'"{polish}"' in result.pl, f"{polish!r} missing in {result.pl!r}"
                assert english not in result.pl, f"{english!r} left in {result.pl!r}"
    assert quoted_in_english >= 5
    for english, polish in BUTTON_TEXTS.items():
        assert polish in {
            corpus.table.pl.get("button.addToFusion"),
            corpus.table.pl.get("button.openSeparate"),
            corpus.table.pl.get("button.add"),
        }, f"{polish!r} is not a caption of the page"
        assert english in corpus.table.en.values()


@needs_node
def test_polish_text_keeps_its_diacritics(corpus: Corpus) -> None:
    spot_checks = {
        "Found 2 part(s) for 'x'.": "części",
        "Saved C2040 (RP2040) with 3 warning(s).": "ostrzeżeniami",
        "Ready. Libraries are saved to C:\\x": "są zapisywane",
        controller.GENERIC_ERROR_TEXT: "Szczegóły zapisano",
        "No folder given.": "Nie podano folderu.",
        "Refusing to open a folder outside the library output directory.": "wyjściowego bibliotek",
        "EasyEDA has no data for C2040 (HTTP 404).": "nie ma danych",
        "Fusion could not open the library 'X'.": "Fusion nie mógł otworzyć biblioteki",
    }
    found = translate(spot_checks)
    for english, expected in spot_checks.items():
        assert expected in found[english].pl, f"{expected!r} not in {found[english].pl!r}"
    everything = " ".join(result.pl for result in corpus.results.values())
    for letter in "ąćęłóśż":
        assert letter in everything, f"no translation uses {letter!r}"


@needs_node
def test_texts_without_a_pattern_stay_as_they_are(corpus: Corpus) -> None:
    for text in DATA_TEXTS:
        result = corpus.results[text]
        assert result.index is None and result.all == (), f"{text!r} matches #{result.all}"
        assert result.pl == text and result.xx == text


@needs_node
def test_english_and_unknown_languages_get_the_original(corpus: Corpus) -> None:
    translated = 0
    for text, result in corpus.results.items():
        assert result.en == text, f"{text!r} changed in English"
        assert result.xx == text, f"{text!r} changed in an unknown language"
        translated += result.index is not None
    assert translated >= 100


@needs_node
def test_a_pattern_does_not_swallow_longer_or_shorter_texts() -> None:
    """The whole text must match: a prefix or a suffix around a known sentence is another text."""
    base = "No folder given."
    variants = [base, f"{base} More", f"Oh. {base}", f" {base} "]
    found = translate(variants)
    assert found[base].index is not None
    assert found[variants[1]].index is None
    assert found[variants[2]].index is None
    assert found[variants[3]].index == found[base].index  # the page trims the text first


# The Fusion adapter, with the real adsk stubs when the machine has them --------------------------


@needs_node
def test_texts_of_the_fusion_adapter_have_patterns(
    adsk_stubs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fusion_adapter

    texts: list[str] = []

    def reason_of(call: Callable[[], object]) -> None:
        with pytest.raises(ValueError) as caught:
            call()
        texts.append(str(caught.value))

    reason_of(lambda: fusion_adapter.command_path(Path("relative/x.lbr")))
    reason_of(lambda: fusion_adapter.build_open_command(Path('C:/odd"name.lbr')))
    reason_of(lambda: fusion_adapter.build_create_3d_command(Path('C:/odd/"x.lbr')))
    reason_of(lambda: fusion_adapter.build_merge_commands(Path("C:/odd'name.lbr"), "DS"))
    reason_of(lambda: fusion_adapter.build_merge_commands(Path("C:/x/ok.lbr"), "  "))
    reason_of(lambda: fusion_adapter.build_merge_commands(Path("C:/x/ok.lbr"), 'a"b'))
    reason_of(lambda: fusion_adapter.build_merge_commands(Path("C:/x/a@b.lbr"), "DS"))

    class App:
        def executeTextCommand(self, command: str) -> str:
            raise RuntimeError("Fusion said no")

    importer = fusion_adapter.FusionImporter(App(), lambda _text: None)  # type: ignore[arg-type]
    real = tmp_path / "C2040.lbr"
    real.write_text("<eagle/>", encoding="utf-8")
    for path in (Path("relative.lbr"), tmp_path / "missing.lbr", real):
        outcome = importer.open_library(path)
        assert not outcome.opened
        texts.append(outcome.message)
        texts.append(describe_open_outcome(outcome, path.name)[1])

    env = FlowEnv(tmp_path / "adder", Seen(), "adder")
    adder = fusion_adapter.FlowLibraryAdder(env.api, env.store, lambda _text: None)
    statuses: list[tuple[str, str]] = []
    request = env.request()
    adder._running = True
    texts.append(adder.add(request, lambda level, text: statuses.append((level, text))).message)

    def explode(*_args: object) -> object:
        raise RuntimeError("flow broke")

    monkeypatch.setattr(fusion_adapter, "FusionLibraryFlow", explode)
    adder._running = False
    texts.append(adder.add(request, lambda level, text: statuses.append((level, text))).message)
    texts.extend(text for _level, text in statuses)

    found = translate(texts)
    missing = [text for text in texts if found[text].index is None]
    assert not missing, "no pattern for:\n" + "\n".join(missing)
    assert len(texts) >= 15


# Nothing outside the table is needed to read it --------------------------------------------------


def test_the_markers_of_the_table_are_in_the_file() -> None:
    source = I18N_JS.read_text(encoding="utf-8")
    assert source.count("MESSAGES:BEGIN") == 1 and source.count("MESSAGES:END") == 1
    assert source.index("MESSAGES:BEGIN") < source.index("MESSAGES:END")
