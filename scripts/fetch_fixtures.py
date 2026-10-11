"""Dev helper: download EasyEDA part documents into tests/fixtures/real/<id>.json.

Real fixtures stay local (the directory is git-ignored, see DECISIONS.md D-013). Existing files
are skipped, requests are rate limited, and the run stops at the first block from the server.

Usage: python scripts/fetch_fixtures.py [C2040 C25804 ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from lcsc2fusion.client.easyeda import EasyEdaClient, normalize_lcsc_id
from lcsc2fusion.client.ratelimit import RateLimiter
from lcsc2fusion.errors import LcscError, PartNotFoundError, RateLimitedError

REFERENCE_IDS: tuple[str, ...] = (
    "C25804",
    "C14663",
    "C81598",
    "C8678",
    "C20526",
    "C6186",
    "C7593",
    "C6961",
    "C2040",
    "C454188",
    "C15483",
    "C14877",
    "C8734",
    "C2895846",
    "C46749",
    "C165948",
    "C2913202",
    "C12674",
    "C13738",
    "C1035",
    "C476261",
    "C2286",
    "C318884",
    "C7943",
)

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real"


def _lcsc_id_arg(text: str) -> str:
    try:
        return normalize_lcsc_id(text)
    except PartNotFoundError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "ids",
        nargs="*",
        type=_lcsc_id_arg,
        help=f"LCSC ids to download (default: the {len(REFERENCE_IDS)} reference parts)",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="target directory")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="minimum seconds between HTTP requests (default: 1.5)",
    )
    return parser


def fetch_fixtures(
    ids: Sequence[str],
    out_dir: Path,
    client: EasyEdaClient,
) -> tuple[list[str], list[str], list[str]]:
    """Download missing fixtures. Returns (written, skipped, failed) id lists.

    A `RateLimitedError` aborts the run (the remaining ids are reported as failed) because
    pushing on while blocked only prolongs the block.
    """
    written: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = [normalize_lcsc_id(raw) for raw in ids]  # ids from code, already validated by CLI
    for index, part_id in enumerate(pending):
        target = out_dir / f"{part_id}.json"
        if target.exists():
            skipped.append(part_id)
            continue
        try:
            document = client.fetch_part(part_id)
        except RateLimitedError as exc:
            print(f"{part_id}: blocked, stopping ({exc})", file=sys.stderr)
            failed.extend(pending[index:])
            break
        except LcscError as exc:
            print(f"{part_id}: failed ({exc})", file=sys.stderr)
            failed.append(part_id)
            continue
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        written.append(part_id)
        print(f"{part_id}: saved {target}")
    return written, skipped, failed


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ids = args.ids or list(REFERENCE_IDS)
    client = EasyEdaClient(limiter=RateLimiter(args.interval))
    written, skipped, failed = fetch_fixtures(ids, args.out_dir, client)
    print(f"written {len(written)}, skipped {len(skipped)}, failed {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
