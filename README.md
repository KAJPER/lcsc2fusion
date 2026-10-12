# lcsc2fusion

Open source tool that turns an LCSC part number (for example `C2040`) or a search phrase into an
Eagle `.lbr` library (symbol + footprint) ready to use in Autodesk Fusion Electronics. Data comes
from the unofficial EasyEDA/LCSC endpoints. It works as a command line tool and as a Fusion add-in
with a search palette, in the spirit of SnapEDA.

Status: the converter core is covered by offline tests (3000+) and the whole chain is confirmed by hand in
Fusion Electronics: generated libraries import cleanly, the add-in window (search, accumulating
"LCSC2Fusion Library" in the hub, 3D package editor opened with the model in place) works, and the placed 3D
model lines up with the footprint. Open points: the Windows taskbar icon and a rare Fusion "Can't rename"
error right after a library is first created, see [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md) and DECISIONS.md.

## Install (development)

```
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

The runtime has no third party dependencies (Python 3.11+ standard library only). matplotlib is
needed only for the footprint renderer.

## Command line

```
python -m lcsc2fusion C2040 -o out/               # writes out/C2040.lbr
python -m lcsc2fusion C2040 C14663 -n mylib -o out/   # several parts in one library
python -m lcsc2fusion --search 100nF 0603          # list search results
python -m lcsc2fusion 100nF 0603 --pick 1 -o out/  # convert the first search result
python -m lcsc2fusion C2040 --render -o out/       # also write footprint/symbol PNGs
python -m lcsc2fusion C25804 --3d -o out/          # also download the 3D model (STEP), see below
```

Responses are cached on disk (7 days) and requests are rate limited. Use `--no-cache` to refetch.

## 3D models (opt-in)

`--3d` (or the "Include 3D model (STEP)" checkbox in the add-in) downloads the EasyEDA 3D model and
writes next to the `.lbr`: `<id>.step` (as downloaded), `<id>.placed.step` (the same model moved into
the package frame: millimetres, Z up, origin at the footprint origin, bottom on the board) and
`<id>.3d.json` (placement, checksums, attribution). Fusion keeps 3D models as separate designs in its
Hub and cannot be given one through the `.lbr`, so you attach the model yourself: in the library
open Content Manager > Footprints, right click the footprint, "Attach Existing 3D Model", "Select
from my computer...", pick `<id>.placed.step`. Use is personal only (the data belongs to
JLCEDA/EasyEDA), do not redistribute the STEP files.

## Fusion add-in

Build the add-in folder and zip, then install it:

```
python scripts/build_addin.py          # creates dist/LCSC2Fusion and dist/LCSC2Fusion.zip
```

Unpack `LCSC2Fusion` into `%APPDATA%\Autodesk\FusionAddins\` (or add the folder with the green plus
in Scripts and Add-Ins) and run it. The UI opens in a separate browser window (Edge or Chrome in app
mode, served locally by the add-in), so it stays open when Fusion switches environment, for example
when a library opens in the Electronics editor. The LCSC2Fusion button is in Utilities > ADD-INS of
the Design, Schematic, Board and Library environments. Search, press "Add to library", and follow
the status message. With "Add to the Fusion library right away" (default) the part goes into one
accumulating library, "LCSC2Fusion Library", in your active Fusion hub project, and with the 3D box ticked the
3D package editor opens with the model already in place, so you only check it and press Finish (unverified in
Fusion until gate 5, see [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md)). See [addin/README.md](addin/README.md) and
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## Use from another Claude (MCP)

`python -m lcsc2fusion.mcp` starts an MCP server (stdio, standard library only) so that Claude Code
or Claude Desktop can search LCSC, inspect a part, convert it to an `.lbr` and, through the running
Fusion add-in, add it to the "LCSC2Fusion Library". The repository ships a project scope
`.mcp.json`, so `claude` started in this folder offers the server `lcsc2fusion` right away. Tools:
`lcsc_search`, `lcsc_part_info`, `lcsc_convert`, `fusion_status`, `fusion_add_part`, `fusion_job`.
Install options, the local add-in API, security notes and an example conversation are in
[docs/MCP.md](docs/MCP.md). The same terms of use as below apply; do not use it for bulk scraping.

## How it works

```
EasyEDA/LCSC JSON  ->  parser (clean room)  ->  internal model (mm, Y up)  ->  .lbr writer + lint
```

- `lcsc2fusion/client`: HTTP, disk cache, rate limiting, retry, search
- `lcsc2fusion/parser`: EasyEDA shape strings to the internal model (one place for units and axes)
- `lcsc2fusion/lbr_writer`: model to Eagle XML, plus a linter for Fusion compatibility
- `lcsc2fusion/render.py`: footprint/symbol rendering to PNG/SVG for visual checks
- `lcsc2fusion/mcp`: MCP server for other Claudes (tools, JSON-RPC loop, client of the add-in API)
- `addin/`: Fusion add-in (manifest, palette, thin adapter around the core)

Research notes and decisions: [docs/RESEARCH.md](docs/RESEARCH.md), [DECISIONS.md](DECISIONS.md).

## Known limitations and risks

- Terms of service. EasyEDA's terms forbid bulk or systematic automated access and the use of its
  content for machine learning, and its `robots.txt` disallows `/api/`. This tool fetches single
  parts on user request with caching and rate limiting, but you are responsible for complying with
  the EasyEDA/JLCEDA and LCSC terms. The library data stays the property of JLCEDA/EasyEDA, and
  generated libraries carry the required attribution ("JLCEDA/EasyEDA Official Library",
  https://lceda.cn/, https://easyeda.com). Commercial use of the data is granted by EasyEDA only when
  used in EasyEDA. This is not legal advice.
- The endpoints are unofficial and can change or start blocking without notice.
- Fusion's Electronics API is read only. Opening the generated library uses undocumented text
  commands (with a manual fallback), which may change between Fusion builds.
- Lossy conversions: polygon pads become rectangles, through-hole rectangles become square pads,
  per pad mask/paste expansion is not representable, thermal pads are plain SMD pads, oval
  through-hole pads follow the DRC elongation, and 3D models are only downloaded as STEP files, you attach them by hand.
- Non-plated pads (Eagle has none) are written as plated pads. Symbol pins keep the EasyEDA
  1.27 mm grid, so Fusion may report "off grid" pins for some symbols. Multi-unit symbols become
  several gates. Datasheet links are best effort.
- Rotation and arc direction conventions were derived from sample data and checked against
  EasyEDA previews, not yet inside Fusion. Check a generated footprint visually (`--render`)
  against the EasyEDA preview before trusting it for a board.

## Development

```
python -m pytest              # offline tests (synthetic fixtures; real ones are optional, local only)
ruff check . && ruff format .
mypy lcsc2fusion
```

The committed fixtures in `tests/fixtures/synthetic/` are generated by
`tests/fixtures/synthetic/generate.py` together with independently computed expected values (pad
positions, sizes, pin positions in mm). Tests marked `real` additionally use
`tests/fixtures/real/*.json` (EasyEDA responses downloaded with `python scripts/fetch_fixtures.py`).
That directory is gitignored because the content belongs to EasyEDA.

`python scripts/make_manual_test_assets.py` builds the library and renders used by the manual
Fusion checklist in [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## License

MIT, see [LICENSE](LICENSE). EasyEDA/LCSC data is not covered by this license.
