# LCSC2Fusion add-in

Autodesk Fusion add-in with an HTML palette: search LCSC parts, press "Add to library", get an
Eagle `.lbr` (symbol and footprint) written by the `lcsc2fusion` core. Status: not yet verified
inside Fusion, see `docs/MANUAL_TEST.md`.

## Layout

```
addin/LCSC2Fusion/            folder name == .py name == .manifest name (Fusion requires it)
  LCSC2Fusion.manifest        add-in manifest
  LCSC2Fusion.py              run(context) / stop(context), sys.path setup, error reporting
  controller.py               palette logic and message protocol, no adsk (unit tested)
  core_service.py             PartService on top of lcsc2fusion.service (lazy import)
  fusion_adapter.py           the ONLY module importing adsk: palette, command, worker thread
  palette/                    index.html, app.js, style.css, mock-bridge.js
  resources/                  command button icons
```

All Fusion API calls run on the main thread. Network work runs on a worker thread; its result is
queued and handed back with `app.fireCustomEvent` and a custom event handler.

## Install

Build the package (stdlib only), which copies the `lcsc2fusion/` core into the add-in folder:

```
python scripts/build_addin.py
```

This creates `dist/LCSC2Fusion/` and `dist/LCSC2Fusion.zip` (the folder is the zip root). Then
either:

- extract the zip into `%APPDATA%\Autodesk\FusionAddins\` so that
  `%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest` exists, or
- in Fusion open Scripts and Add-Ins (Shift+S), Add-Ins tab, click the green plus and select the
  `dist/LCSC2Fusion` folder.

Windows may block a downloaded zip: right click, Properties, Unblock, before extracting. Then run
LCSC2Fusion from the Add-Ins tab. The button appears in the Design workspace under
Utilities > ADD-INS, and the palette docks on the right. If that panel cannot be found the palette
is still shown once at start (see the Text Commands window for the log line).

## Development mode

No build is needed. In Scripts and Add-Ins click the green plus and select `addin/LCSC2Fusion`
from the repository. When `lcsc2fusion/` is not inside the add-in folder, `LCSC2Fusion.py` adds the
repository root (two levels up) to `sys.path`. `stop` unloads the add-in modules, so stop and run
again to pick up edited code. Output goes to the Text Commands window (prefix `[LCSC2Fusion]`) and
to Fusion's log file.

To look at the palette without Fusion open `addin/LCSC2Fusion/palette/index.html` in a normal
browser. After about 1.5 seconds without `adsk` it switches to `mock-bridge.js` with fake data
(try `C2040`, `resistor`, `none`, `fail`; with the 3D box ticked `C2913202` shows the "no 3D
model" case, every other part gets a model). The mock never runs inside Fusion.

## Behaviour

- Libraries are written to `~/LCSC2Fusion/libraries/<id>.lbr` (created on demand).
- After saving, the palette shows an "Open in Fusion Electronics" button. Pressing it runs the
  undocumented text command `Document.newDesignFromLocal "<path>"` inside the palette message
  handler (the only context research confirms as safe for Electronics text commands, D-006
  variant A). Nothing is opened automatically. When the command fails, the palette shows the
  manual steps: File > Open > Open from my computer, then File > Save into a project so the
  library appears in the Library Manager.
- Optional 3D model: the checkbox "Include 3D model (STEP)" next to the search field is off by
  default and is remembered only while the palette page lives. With it ticked, "Add to library"
  sends `{lcsc_id, with_3d: true}`; the core then writes `<id>.step` (raw download) and
  `<id>.placed.step` (transformed into the package frame) next to the `.lbr`, and the `added`
  message carries `step_path`, `placed_step_path` and `with_3d`. The result card then shows the
  file names and the manual steps below. When 3D was requested but no model was produced (the
  part has none, the download was blocked) the card shows a warning instead and the library is
  still saved. The model data belongs to JLCEDA/EasyEDA: personal use only, no redistribution.
- Only one request runs at a time. Library errors (part not found, blocked by EasyEDA, network) are
  shown verbatim; anything unexpected is shown generically and logged with a traceback.
- Data source and terms: the footer of the palette repeats the attribution. Use is personal and you
  are responsible for complying with the EasyEDA and LCSC terms.

## Attaching the 3D model in Fusion (manual)

Fusion offers no documented API to attach a model to a library from an add-in, so this step is
manual (official path, Content Manager):

1. Open the library in Fusion Electronics ("Open in Fusion Electronics", then File > Save into a
   project).
2. In the Content Manager open the Footprints tab, right click the footprint and choose
   "Attach Existing 3D Model".
3. Click "Select from my computer..." and pick `<id>.placed.step` ("Open folder" shows it).
4. Name the package and press OK.
5. If the model sits wrong, edit the package to adjust the alignment, then File > Save.

Whether the placed model lines up without adjusting still has to be confirmed inside Fusion
(stage 3 gate).

## Tests

```
python -m pytest tests/test_addin_*.py
```

The controller, file and build tests need nothing from Fusion. `test_addin_adapter.py` uses the
official `adsk` API stubs, which are licensed CC BY-NC-SA and therefore not in this repository. Set
`ADSK_DEFS` to `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs` (the default location is
tried automatically); without the stubs those tests are skipped.
