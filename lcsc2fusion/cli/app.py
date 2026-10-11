"""argparse based CLI: `python -m lcsc2fusion C2040 -o out/`."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from lcsc2fusion import __version__
from lcsc2fusion.client.cache import DiskCache
from lcsc2fusion.client.easyeda import EasyEdaClient
from lcsc2fusion.constants import ATTRIBUTION
from lcsc2fusion.errors import LcscError
from lcsc2fusion.service import convert_parts, search

_ID_RE = re.compile(r"^[Cc]\d+$")
_DEFAULT_TTL_S = 7 * 24 * 3600.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcsc2fusion",
        description="Convert LCSC/EasyEDA parts into an Eagle .lbr library for Fusion Electronics.",
        epilog=ATTRIBUTION,
    )
    parser.add_argument(
        "query", nargs="+", help="LCSC part number(s) like C2040, or a search phrase"
    )
    parser.add_argument("-o", "--output", type=Path, default=Path("out"), help="output directory")
    parser.add_argument("-n", "--name", help="library file name (without .lbr) for many parts")
    parser.add_argument("--search", action="store_true", help="only list search results")
    parser.add_argument("--pick", type=int, metavar="N", help="convert the Nth search result")
    parser.add_argument("--limit", type=int, default=10, help="max search results (default 10)")
    parser.add_argument("--render", action="store_true", help="also write footprint/symbol PNGs")
    parser.add_argument("--cache-dir", type=Path, help="override the cache directory")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached responses")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _make_client(args: argparse.Namespace) -> EasyEdaClient:
    ttl = 0.0 if args.no_cache else _DEFAULT_TTL_S
    if args.cache_dir:
        return EasyEdaClient(cache=DiskCache(args.cache_dir, ttl), ttl_s=ttl)
    return EasyEdaClient(ttl_s=ttl)


def _print_results(query: str, limit: int, client: EasyEdaClient) -> list[str]:
    results = search(query, limit, client)
    if not results:
        print(f"No results for {query!r}", file=sys.stderr)
        return []
    for index, r in enumerate(results, start=1):
        extra = f" [{r.library_type}]" if r.library_type else ""
        print(f"{index:>2}. {r.lcsc_id:<10} {r.mpn} | {r.manufacturer} | {r.package}{extra}")
        if r.description:
            print(f"      {r.description}")
    return [r.lcsc_id for r in results]


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        client = _make_client(args)
        queries: list[str] = list(args.query)
        if all(_ID_RE.match(q) for q in queries) and not args.search:
            ids = [q.upper() for q in queries]
        else:
            found = _print_results(" ".join(queries), args.limit, client)
            if args.search or not found:
                return 0 if found else 1
            if args.pick is None:
                print("Use --pick N to convert one of the results.", file=sys.stderr)
                return 0
            if not 1 <= args.pick <= len(found):
                print(f"--pick must be between 1 and {len(found)}", file=sys.stderr)
                return 2
            ids = [found[args.pick - 1]]
        result = convert_parts(ids, args.output, client=client, library_name=args.name)
        print(f"Wrote {result.path}")
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.render:
            from lcsc2fusion.render import render_part

            for part in result.parts:
                for image in render_part(part, args.output):
                    print(f"Wrote {image}")
        return 0
    except LcscError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
