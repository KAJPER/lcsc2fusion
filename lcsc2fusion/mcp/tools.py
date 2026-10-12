"""The six MCP tools: search, inspect and convert LCSC parts, and drive the Fusion add-in.

Each tool is a `Tool`: a name, a JSON schema, annotations and a handler that gets a `ToolContext`
and the (already shape checked) arguments and returns a `ToolResult`. Handlers validate their
own arguments with `validation` (a failure becomes a JSON-RPC "invalid params" error) and let
expected failures (`LcscError`, `OSError`) escape; `call_tool` turns those into tool results with
`isError` true and a message the model can act on. Stack traces go to the log (stderr), never to
the model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lcsc2fusion import service
from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.constants import ATTRIBUTION
from lcsc2fusion.errors import (
    LcscError,
    NetworkError,
    ParseError,
    PartNotFoundError,
    RateLimitedError,
)
from lcsc2fusion.links import lcsc_product_url
from lcsc2fusion.mcp import validation as check
from lcsc2fusion.mcp.fusion_client import (
    FusionBusyError,
    FusionClient,
    FusionError,
    FusionNoAnswerError,
    FusionNotRunningError,
    JobInfo,
    new_steps,
)
from lcsc2fusion.mcp.validation import InvalidArguments
from lcsc2fusion.model import Part, SearchResult
from lcsc2fusion.parser.model3d import MODEL_WARNING_PREFIX
from lcsc2fusion.service import ConversionResult

_log = logging.getLogger(__name__)

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 20
WAIT_TIMEOUT_DEFAULT_S = 240.0
WAIT_TIMEOUT_MIN_S = 10.0
WAIT_TIMEOUT_MAX_S = 600.0
MAX_JOB_NUMBER = 2_000_000_000

# The EasyEDA client used by the tools: shorter than the CLI defaults, so that a stuck network
# call ends in about a minute instead of several.
CLIENT_TIMEOUT_S = 20.0
CLIENT_RETRY_ATTEMPTS = 2

# Progress notifications while a job runs: one per new status line, else a heartbeat.
HEARTBEAT_S = 10.0

_TEXT_LIMITS = {"short": 120, "description": 300, "message": 1000}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_WARNINGS = 30


# Context and results


@dataclass(frozen=True)
class ToolResult:
    """What a tool returns: text for the model, the same facts as data, and the error flag."""

    text: str
    data: dict[str, Any]
    is_error: bool = False


class ConvertFn(Protocol):
    def __call__(
        self,
        lcsc_ids: Sequence[str],
        out_dir: Path,
        *,
        client: EasyEdaClient | None = None,
        library_name: str | None = None,
        with_3d: bool = False,
    ) -> ConversionResult: ...


SearchFn = Callable[[str, int, EasyEdaClient], list[SearchResult]]
LoadPartFn = Callable[[str, EasyEdaClient], Part]


def default_out_dir() -> Path:
    """Where libraries go by default, the same folder the Fusion add-in uses."""
    return Path.home() / "LCSC2Fusion" / "libraries"


def make_client() -> EasyEdaClient:
    """The EasyEDA client with cache, rate limit and retry, tuned for interactive use."""
    return EasyEdaClient(timeout_s=CLIENT_TIMEOUT_S, retry_attempts=CLIENT_RETRY_ATTEMPTS)


@dataclass
class Backend:
    """Everything the tools talk to. Tests replace the pieces; the defaults are the real ones.

    The EasyEDA client is created on first use and then shared, so its rate limiter, block guard
    and cache apply to all calls of one server process.
    """

    client_factory: Callable[[], EasyEdaClient] = make_client
    search: SearchFn = service.search
    load_part: LoadPartFn = service.load_part
    convert: ConvertFn = service.convert_parts
    fusion_factory: Callable[[], FusionClient] = FusionClient
    out_dir_default: Callable[[], Path] = default_out_dir
    _client: EasyEdaClient | None = field(default=None, init=False, repr=False)

    def client(self) -> EasyEdaClient:
        if self._client is None:
            self._client = self.client_factory()
        return self._client


@dataclass(frozen=True)
class ToolContext:
    backend: Backend
    progress: Callable[[str], None]  # a no-op unless the caller asked for progress


Handler = Callable[[ToolContext, Mapping[str, Any]], ToolResult]


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]
    handler: Handler

    def listing(self) -> dict[str, Any]:
        """The entry for `tools/list`."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, **self.annotations},
        }


# Running a tool


def call_tool(tool: Tool, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
    """Run a handler. `InvalidArguments` propagates, expected failures become error results."""
    try:
        return tool.handler(context, arguments)
    except InvalidArguments:
        raise
    except LcscError as exc:
        return failure_result(exc)
    except OSError as exc:
        _log.warning("Tool %s failed with an OS error: %s", tool.name, exc)
        reason = exc.strerror or type(exc).__name__
        return _error(f"A file operation failed: {reason}.", "filesystem")
    except Exception:  # last line of defence: a bug must not kill the server loop
        _log.exception("Tool %s crashed", tool.name)
        return _error(
            f"Internal error in {tool.name}. The details are in the server log (stderr).",
            "internal",
        )


def failure_result(exc: LcscError) -> ToolResult:
    kind, hint = _classify(exc)
    message = one_line(str(exc), _TEXT_LIMITS["message"])
    text = f"{message} {hint}" if hint else message
    return _error(text, kind)


def _classify(exc: LcscError) -> tuple[str, str]:
    """Machine readable kind and a short hint on what to do next."""
    if isinstance(exc, RateLimitedError):
        return "rate_limited", "Do not retry right away and do not loop; wait before the next try."
    if isinstance(exc, NetworkError):
        return "network", "Check the internet connection and try again in a moment."
    if isinstance(exc, PartNotFoundError):
        return "not_found", "Use lcsc_search to find a part that has EasyEDA data."
    if isinstance(exc, ParseError):
        return "parse_error", ""
    if isinstance(exc, FusionNotRunningError):
        return "fusion_not_running", ""
    if isinstance(exc, FusionBusyError):
        return "fusion_busy", ""
    if isinstance(exc, FusionNoAnswerError):
        return "fusion_no_answer", ""
    if isinstance(exc, FusionError):
        return "fusion_error", ""
    if isinstance(exc, check.OutDirError):
        return "out_dir", ""
    return "error", ""


def _error(text: str, kind: str) -> ToolResult:
    return ToolResult(text=text, data={"error": text, "error_kind": kind}, is_error=True)


# Text helpers


def one_line(text: str, limit: int) -> str:
    """Whitespace and control characters collapsed to single spaces, cut at `limit`."""
    cleaned = " ".join(_CONTROL_RE.sub(" ", text).split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _http_url(value: str) -> str:
    """The url when it is plain http(s) text, else an empty string (data is third party)."""
    url = one_line(value, 500)
    return url if url.lower().startswith(("https://", "http://")) and " " not in url else ""


def _product_url(lcsc_id: str) -> str:
    try:
        return lcsc_product_url(lcsc_id)
    except ValueError:
        return ""


def _unique(lines: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:_MAX_WARNINGS]


# 1. lcsc_search


def _search_item(result: SearchResult) -> dict[str, Any]:
    return {
        "lcsc_id": result.lcsc_id,
        "mpn": one_line(result.mpn, _TEXT_LIMITS["short"]),
        "manufacturer": one_line(result.manufacturer, _TEXT_LIMITS["short"]),
        "package": one_line(result.package, _TEXT_LIMITS["short"]),
        "description": one_line(result.description, _TEXT_LIMITS["description"]),
        "library_type": one_line(result.library_type, _TEXT_LIMITS["short"]),
        "stock": result.stock,
        "lcsc_url": _product_url(result.lcsc_id),
        "datasheet_url": _http_url(result.datasheet_url),
    }


def _lcsc_search(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ("query", "limit"))
    query = check.as_query(check.require(arguments, "query"))
    limit = check.as_int(arguments.get("limit", SEARCH_LIMIT_DEFAULT), "limit", 1, SEARCH_LIMIT_MAX)
    backend = context.backend
    found = backend.search(query, limit, backend.client())[:limit]
    items = [_search_item(result) for result in found]
    data: dict[str, Any] = {"query": query, "count": len(items), "results": items}
    if not items:
        text = (
            f"No parts found for {query!r}. Try fewer or different words, the manufacturer part "
            "number, or an LCSC number like C2040."
        )
        return ToolResult(text=text, data=data)
    lines = [f"{len(items)} result(s) for {query!r}:"]
    for index, item in enumerate(items, start=1):
        extras = [item["package"], item["library_type"]]
        if item["stock"] is not None:
            extras.append(f"stock {item['stock']}")
        lines.append(
            f"{index}. {item['lcsc_id']}  {item['mpn']}  {item['manufacturer']}  "
            + " | ".join(extra for extra in extras if extra)
        )
        if item["description"]:
            lines.append(f"   {item['description']}")
        if item["lcsc_url"]:
            lines.append(f"   {item['lcsc_url']}")
    lines.append(
        "Next: lcsc_part_info to check a part, then lcsc_convert (files) or fusion_add_part "
        "(Fusion)."
    )
    return ToolResult(text="\n".join(lines), data=data)


# 2. lcsc_part_info


def _lcsc_part_info(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ("lcsc_id",))
    lcsc_id = check.as_lcsc_id(check.require(arguments, "lcsc_id"))
    backend = context.backend
    part = backend.load_part(lcsc_id, backend.client())
    pins = sum(len(symbol.pins) for symbol in part.symbols)
    plain = [w for w in part.warnings if not w.startswith(MODEL_WARNING_PREFIX)]
    model_notes = [w for w in part.warnings if w.startswith(MODEL_WARNING_PREFIX)]
    has_3d = part.model3d is not None
    data: dict[str, Any] = {
        "lcsc_id": lcsc_id,
        "mpn": one_line(part.mpn, _TEXT_LIMITS["short"]),
        "manufacturer": one_line(part.manufacturer, _TEXT_LIMITS["short"]),
        "description": one_line(part.description, _TEXT_LIMITS["description"]),
        "package": one_line(part.package.name, _TEXT_LIMITS["short"]),
        "pad_count": len(part.package.pads),
        "hole_count": len(part.package.holes),
        "pin_count": pins,
        "symbol_count": len(part.symbols),
        "has_3d_model": has_3d,
        "warnings": _unique([one_line(w, _TEXT_LIMITS["message"]) for w in plain]),
        "model3d_warnings": _unique([one_line(w, _TEXT_LIMITS["message"]) for w in model_notes]),
        "datasheet_url": _http_url(part.datasheet),
        "lcsc_url": _product_url(lcsc_id),
        "attribution": ATTRIBUTION,
    }
    title = f"{lcsc_id}: {data['mpn']}" if data["mpn"] else lcsc_id
    lines = [
        f"{title} ({data['manufacturer']})" if data["manufacturer"] else title,
        f"Package {data['package']}: {data['pad_count']} pad(s), {data['hole_count']} hole(s); "
        f"symbol: {data['pin_count']} pin(s) in {data['symbol_count']} unit(s).",
        "3D model: "
        + (
            "available (lcsc_convert or fusion_add_part with with_3d=true)."
            if has_3d
            else "none on EasyEDA for this part."
        ),
    ]
    if data["description"]:
        lines.append(data["description"])
    lines.extend(f"Warning: {w}" for w in data["warnings"])
    lines.extend(f"3D note: {w}" for w in data["model3d_warnings"])
    if data["datasheet_url"]:
        lines.append(f"Datasheet: {data['datasheet_url']}")
    lines.append(
        "Check the footprint and pinout against the datasheet before ordering boards. Nothing "
        "was written to disk."
    )
    return ToolResult(text="\n".join(lines), data=data)


# 3. lcsc_convert


def _lcsc_convert(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ("lcsc_id", "with_3d", "out_dir"))
    lcsc_id = check.as_lcsc_id(check.require(arguments, "lcsc_id"))
    with_3d = check.as_bool(arguments.get("with_3d", False), "with_3d")
    backend = context.backend
    default_dir = backend.out_dir_default()
    out_dir = (
        check.resolve_out_dir(arguments["out_dir"], default_dir)
        if "out_dir" in arguments
        else default_dir
    )
    check.prepare_out_dir(out_dir)
    context.progress(f"Downloading and converting {lcsc_id}...")
    # The file name is the validated id, never the number inside the downloaded document.
    result = backend.convert(
        [lcsc_id], out_dir, client=backend.client(), library_name=lcsc_id, with_3d=with_3d
    )
    part = result.parts[0]
    models = [
        {
            "lcsc_id": files.lcsc_id,
            "step_path": str(files.step_path),
            "placed_step_path": str(files.placed_step_path),
            "info_path": str(files.info_path),
        }
        for files in result.models
    ]
    warnings = _unique([one_line(w, _TEXT_LIMITS["message"]) for w in result.warnings])
    data: dict[str, Any] = {
        "lcsc_id": lcsc_id,
        "mpn": one_line(part.mpn, _TEXT_LIMITS["short"]),
        "out_dir": str(out_dir),
        "lbr_path": str(result.path),
        "with_3d": with_3d,
        "model_files": models,
        "warnings": warnings,
        "attribution": ATTRIBUTION,
    }
    lines = [f"Wrote {result.path}" + (f" ({data['mpn']})" if data["mpn"] else "")]
    for model in models:
        lines.append(
            f"3D model for {model['lcsc_id']}, the file to attach: {model['placed_step_path']}"
        )
        lines.append(f"Also written: {model['step_path']} and {model['info_path']}")
    lines.extend(f"Warning: {w}" for w in warnings)
    lines.append(
        "Open the .lbr in Fusion Electronics, or use fusion_add_part to let the Fusion add-in "
        "merge the part into the LCSC2Fusion Library."
    )
    lines.append(ATTRIBUTION)
    return ToolResult(text="\n".join(lines), data=data)


# 4. fusion_status


def _fusion_status(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ())
    fusion = context.backend.fusion_factory()
    try:
        ping = fusion.ping()
    except FusionNotRunningError as exc:
        return _status_result(False, False, False, str(exc), None)
    except FusionNoAnswerError as exc:
        return _status_result(True, True, False, str(exc), None)
    except FusionError as exc:
        return _status_result(True, False, False, str(exc), None)
    if ping.busy:
        message = (
            "The Fusion add-in is running but busy with a part. Wait a little (check "
            "fusion_status again) before adding another one."
        )
    else:
        message = "The Fusion add-in is running and ready. fusion_add_part can add a part."
    return _status_result(True, ping.busy, True, message, ping.version)


def _status_result(
    running: bool, busy: bool, responsive: bool, message: str, api_version: int | None
) -> ToolResult:
    text = one_line(message, _TEXT_LIMITS["message"])
    data = {
        "running": running,
        "busy": busy,
        "responsive": responsive,
        "message": text,
        "api_version": api_version,
    }
    return ToolResult(text=text, data=data)


# 5. fusion_add_part and 6. fusion_job


class _ProgressTracker:
    """Turns polls of a job into progress messages: new status lines, else a heartbeat."""

    def __init__(self, progress: Callable[[str], None]) -> None:
        self._progress = progress
        self._seen: list[str] = []
        self._last_message_at = 0.0

    def on_poll(self, info: JobInfo | None, elapsed_s: float) -> None:
        if info is not None:
            fresh = new_steps(self._seen, list(info.steps))
            self._seen = list(info.steps)
            for step in fresh:
                self._progress(one_line(step, _TEXT_LIMITS["message"]))
            if fresh:
                self._last_message_at = elapsed_s
                return
        if elapsed_s - self._last_message_at >= HEARTBEAT_S:
            self._last_message_at = elapsed_s
            state = "still working" if info is not None else "not answering (busy?)"
            self._progress(f"Fusion is {state}, waited {elapsed_s:.0f} s so far.")


def job_data(info: JobInfo) -> dict[str, Any]:
    """The job as tool data: the add-in's document flattened to the fields a model needs."""
    added, fusion = info.added, info.fusion
    return {
        "job": info.job,
        "lcsc_id": info.lcsc_id,
        "state": info.state,
        "finished": info.terminal,
        "ok": info.ok,
        "status": info.status,
        "message": info.message,
        "steps": list(info.steps),
        "mpn": added.mpn if added else None,
        "library_name": fusion.library_name if fusion else None,
        "created_library": fusion.created_library if fusion else None,
        "merged": fusion.merged if fusion else None,
        "already_present": fusion.already_present if fusion else None,
        "three_d": fusion.three_d if fusion else None,
        "needs_human_confirmation": bool(fusion and fusion.three_d == "started"),
        "warnings": _unique(
            [*(added.warnings if added else ()), *(fusion.warnings if fusion else ())]
        ),
        "lbr_path": added.path if added else None,
        "step_path": added.step_path if added else None,
        "placed_step_path": added.placed_step_path if added else None,
    }


def job_text(info: JobInfo, waited_s: float | None = None) -> str:
    """A short human readable account of a job in any state."""
    data = job_data(info)
    subject = info.lcsc_id or f"job {info.job}"
    head = f"{subject} ({data['mpn']})" if data["mpn"] else subject
    lines: list[str] = []
    if not info.terminal:
        lines.append(
            f"Job {info.job} ({subject}) is still running in Fusion: {info.status or 'working'}."
        )
        if waited_s is not None:
            lines.append(
                f"Stopped waiting after {waited_s:.0f} s; the job keeps running in Fusion."
            )
        lines.append(f"Call fusion_job with job={info.job} to check again.")
    elif not info.succeeded:
        reason = info.message or info.status or "no reason given"
        lines.append(f"Adding {head} failed in Fusion (job {info.job}): {reason}")
    else:
        lines.append(_success_line(head, info))
        outcome_message = info.fusion.message if info.fusion else ""
        extra = outcome_message or info.message
        if extra and extra not in lines[0]:
            lines.append(extra)
    three_d = data["three_d"]
    if three_d == "started":
        lines.append(
            "A 3D package editor is open in Fusion with the model in place. A person must check "
            "it and press Finish, then Save, then OK; the part is not complete until they do. "
            "Fusion may be slow to respond while it works."
        )
    elif three_d == "failed":
        lines.append(
            "The 3D package could not be created in Fusion (the part itself is in the library)."
        )
    elif three_d == "skipped":
        lines.append("The 3D step was skipped, for example because the part has no 3D model.")
    lines.extend(f"Warning: {w}" for w in data["warnings"])
    return "\n".join(lines)


def _success_line(head: str, info: JobInfo) -> str:
    fusion = info.fusion
    if fusion is None:
        return f"Job {info.job} finished: {head} was processed."
    place = (
        f"the Fusion library '{fusion.library_name}'"
        if fusion.library_name
        else "the Fusion library"
    )
    if fusion.already_present:
        return f"{head} was already in {place}; nothing new was added."
    if fusion.created_library:
        return f"{head} is the first part of the new {place}."
    if fusion.merged:
        return f"{head} was merged into {place}."
    return f"{head} was added to {place}."


def job_result(info: JobInfo, waited_s: float | None = None) -> ToolResult:
    failed = info.terminal and not info.succeeded
    data = job_data(info)
    if failed:
        data["error_kind"] = "fusion_job_failed"
    return ToolResult(
        text=job_text(info, waited_s if not info.terminal else None), data=data, is_error=failed
    )


def _fusion_add_part(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ("lcsc_id", "with_3d", "wait", "timeout_s"))
    lcsc_id = check.as_lcsc_id(check.require(arguments, "lcsc_id"))
    with_3d = check.as_bool(arguments.get("with_3d", False), "with_3d")
    wait = check.as_bool(arguments.get("wait", True), "wait")
    timeout_s = check.as_number(
        arguments.get("timeout_s", WAIT_TIMEOUT_DEFAULT_S),
        "timeout_s",
        WAIT_TIMEOUT_MIN_S,
        WAIT_TIMEOUT_MAX_S,
    )
    fusion = context.backend.fusion_factory()
    context.progress(f"Asking the Fusion add-in to add {lcsc_id}...")
    job = fusion.add(lcsc_id, with_3d)
    if not wait:
        text = (
            f"Fusion add-in accepted {lcsc_id} as job {job} and works on it now. Call fusion_job "
            f"with job={job} to follow it."
        )
        data = {
            "job": job,
            "lcsc_id": lcsc_id,
            "state": "running",
            "finished": False,
            "with_3d": with_3d,
        }
        return ToolResult(text=text, data=data)
    tracker = _ProgressTracker(context.progress)
    info = fusion.wait_for_job(job, timeout_s, tracker.on_poll)
    return job_result(info, waited_s=timeout_s)


def _fusion_job(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    check.reject_unknown(arguments, ("job",))
    job = check.as_int(check.require(arguments, "job"), "job", 0, MAX_JOB_NUMBER)
    info = context.backend.fusion_factory().job(job)
    return job_result(info)


# Registry

_READ_ONLY_OPEN = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}
_READ_ONLY_LOCAL = {**_READ_ONLY_OPEN, "openWorldHint": False}
_WRITES = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

_LCSC_ID_PROPERTY = {
    "type": "string",
    "pattern": "^[Cc][0-9]{1,12}$",
    "description": "LCSC part number: the letter C and digits, for example C2040.",
}
_WITH_3D_PROPERTY = {
    "type": "boolean",
    "default": False,
    "description": "Also fetch the 3D model (STEP). Costs an extra download.",
}


def build_tools() -> tuple[Tool, ...]:
    return (
        Tool(
            name="lcsc_search",
            title="Search LCSC parts",
            description=(
                "Search the LCSC / EasyEDA parts catalogue. Give an LCSC part number (C2040) to "
                "look up that one part, or free text (a value and package like '100nF 0603', or a "
                "manufacturer part number) for up to `limit` hits. Returns lcsc_id, mpn, "
                "manufacturer, package, description, Basic or Extended library type, stock and "
                "links. Only parts that have EasyEDA symbol and footprint data can be converted. "
                "Read only. Requests are cached and rate limited, so do not call it in loops."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": check.MAX_QUERY_CHARS,
                        "description": "LCSC number like C2040, or free text. Max 100 characters.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": SEARCH_LIMIT_MAX,
                        "default": SEARCH_LIMIT_DEFAULT,
                        "description": "Maximum number of results (1 to 20).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            annotations=_READ_ONLY_OPEN,
            handler=_lcsc_search,
        ),
        Tool(
            name="lcsc_part_info",
            title="Inspect an LCSC part",
            description=(
                "Show what converting one LCSC part would produce, without writing any file: "
                "MPN, package name, pad and pin counts, whether a 3D model exists, warnings from "
                "the source data, datasheet and LCSC links. Use it to check a part before "
                "lcsc_convert or fusion_add_part. Read only."
            ),
            input_schema={
                "type": "object",
                "properties": {"lcsc_id": _LCSC_ID_PROPERTY},
                "required": ["lcsc_id"],
                "additionalProperties": False,
            },
            annotations=_READ_ONLY_OPEN,
            handler=_lcsc_part_info,
        ),
        Tool(
            name="lcsc_convert",
            title="Convert an LCSC part to an .lbr file",
            description=(
                "Download one LCSC part from EasyEDA and write an Eagle .lbr library (symbol and "
                "footprint) to disk; with with_3d also the 3D model as STEP files. Returns the "
                "absolute paths, warnings and the attribution note. Does not touch Fusion: to put "
                "the part into Fusion use fusion_add_part. Default folder: "
                "~/LCSC2Fusion/libraries. The data belongs to JLCEDA/EasyEDA and is for personal "
                "use."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "lcsc_id": _LCSC_ID_PROPERTY,
                    "with_3d": _WITH_3D_PROPERTY,
                    "out_dir": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": check.MAX_OUT_DIR_CHARS,
                        "description": (
                            "Folder for the files: an absolute local path or one starting with ~. "
                            "Created when missing. Default: ~/LCSC2Fusion/libraries."
                        ),
                    },
                },
                "required": ["lcsc_id"],
                "additionalProperties": False,
            },
            annotations=_WRITES,
            handler=_lcsc_convert,
        ),
        Tool(
            name="fusion_status",
            title="Check the Fusion add-in",
            description=(
                "Check whether the LCSC2Fusion add-in is running inside Fusion and ready for "
                "fusion_add_part. Returns running, busy, responsive and a message that says how "
                "to start the add-in when it is not running. Never fails when the add-in is "
                "absent. Local only."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=_READ_ONLY_LOCAL,
            handler=_fusion_status,
        ),
        Tool(
            name="fusion_add_part",
            title="Add a part to Fusion",
            description=(
                "Ask the running Fusion add-in to download an LCSC part and merge it into the "
                "'LCSC2Fusion Library' in the user's Fusion Hub project (created on first use). "
                "Needs Fusion running with the LCSC2Fusion add-in (see fusion_status). With "
                "wait=true (default) it waits until Fusion is done or timeout_s passes and "
                "returns the steps, library name, 3D state and warnings. With wait=false it "
                "returns a job number at once; follow it with fusion_job. With with_3d=true "
                "Fusion also opens a 3D package editor with the model in place that the human "
                "must confirm (Finish, Save, OK); tell the user. Fusion may be unresponsive for "
                "up to about a minute while it works, and only one part can be processed at a "
                "time. Do not call it for many parts in a row."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "lcsc_id": _LCSC_ID_PROPERTY,
                    "with_3d": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Also build the 3D package in Fusion. Leaves an editor open that a "
                            "person must confirm (Finish, Save, OK)."
                        ),
                    },
                    "wait": {
                        "type": "boolean",
                        "default": True,
                        "description": "Wait for the result (true) or return the job number now.",
                    },
                    "timeout_s": {
                        "type": "number",
                        "minimum": WAIT_TIMEOUT_MIN_S,
                        "maximum": WAIT_TIMEOUT_MAX_S,
                        "default": WAIT_TIMEOUT_DEFAULT_S,
                        "description": "Longest wait in seconds (10 to 600) when wait is true.",
                    },
                },
                "required": ["lcsc_id"],
                "additionalProperties": False,
            },
            annotations=_WRITES,
            handler=_fusion_add_part,
        ),
        Tool(
            name="fusion_job",
            title="Check a Fusion job",
            description=(
                "Return the current state of a job started by fusion_add_part: running, done or "
                "failed, with the steps so far and the final result. Use it after wait=false or "
                "after fusion_add_part stopped waiting. Local only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "job": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "The job number returned by fusion_add_part.",
                    }
                },
                "required": ["job"],
                "additionalProperties": False,
            },
            annotations=_READ_ONLY_LOCAL,
            handler=_fusion_job,
        ),
    )
