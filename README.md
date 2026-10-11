# lcsc2fusion

Open source tool that turns an LCSC part number (for example `C2040`) or a search phrase into an
Eagle `.lbr` library (symbol + footprint) ready to use in Autodesk Fusion Electronics. Data comes
from the unofficial EasyEDA/LCSC endpoints. It works as a command line tool and as a Fusion add-in
with a search palette, in the spirit of SnapEDA.

Status: early. The converter core is covered by offline tests. The import of a generated `.lbr`
into Fusion and the add-in itself have not been verified inside Fusion yet, see
[docs/MANUAL_TEST.md](docs/MANUAL_TEST.md) for the checklist.

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
```

Responses are cached on disk (7 days) and requests are rate limited. Use `--no-cache` to refetch.

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
  through-hole pads follow the DRC elongation, and 3D models are not included yet.
- Rotation and arc direction conventions were derived from sample data. Check the generated
  footprint visually (`--render`) against the EasyEDA preview.

## Development

```
python -m pytest              # offline tests (synthetic fixtures; real ones are optional, local only)
ruff check . && ruff format .
mypy lcsc2fusion
```

Tests marked `real` use `tests/fixtures/real/*.json` (EasyEDA responses downloaded with
`python scripts/fetch_fixtures.py`). That directory is gitignored because the content belongs to
EasyEDA.

## License

MIT, see [LICENSE](LICENSE). EasyEDA/LCSC data is not covered by this license.
