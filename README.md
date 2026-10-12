# lcsc2fusion

Open source tool that turns an LCSC part number (for example `C2040`) or a search phrase into an
Eagle `.lbr` library (symbol + footprint) ready to use in Autodesk Fusion Electronics. Data comes
from the unofficial EasyEDA/LCSC endpoints. It works as a command line tool and as a Fusion add-in
with a search palette, in the spirit of SnapEDA.

Status: the converter core is covered by offline tests (2000+). Confirmed by hand in Fusion
Electronics: libraries generated from synthetic parts import cleanly (pads, slots, arcs, symbols,
multi gate parts) and the placed 3D model lines up with the footprint for three real parts. The add-in
palette itself still needs its manual run, see [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

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
in Scripts and Add-Ins), start the add-in, and open the LCSC2Fusion palette. Search, press
"Add to library", and follow the status message. See [addin/README.md](addin/README.md) and
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md).

## How it works

```
EasyEDA/LCSC JSON  ->  parser (clean room)  ->  internal model (mm, Y up)  ->  .lbr writer + lint
```

- `lcsc2fusion/client`: HTTP, disk cache, rate limiting, retry, search
- `lcsc2fusion/parser`: EasyEDA shape strings to the internal model (one place for units and axes)
- `lcsc2fusion/lbr_writer`: model to Eagle XML, plus a linter for Fusion compatibility
- `lcsc2fusion/render.py`: footprint/symbol rendering to PNG/SVG for visual checks
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
