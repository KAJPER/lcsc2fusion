# LCSC2Fusion add-in

Autodesk Fusion add-in: search LCSC parts, press "Add to library", get an Eagle `.lbr` (symbol and
footprint, optionally a 3D model) written by the `lcsc2fusion` core. The UI is a separate browser
window, like SnapEDA's own window, served by a small local HTTP server inside the add-in. Fusion
deletes every API palette whenever the user switches workspace (for example when a library opens in
the Electronics editor), so a palette cannot survive that; the separate window does (D-022).
Status: the window architecture still has to be confirmed inside Fusion, see `docs/MANUAL_TEST.md`
(gate 4).

## Layout

```
addin/LCSC2Fusion/            folder name == .py name == .manifest name (Fusion requires it)
  LCSC2Fusion.manifest        add-in manifest
  LCSC2Fusion.py              run(context) / stop(context), sys.path setup, error reporting
  controller.py               UI logic and message protocol, no adsk (unit tested)
  core_service.py             PartService on top of lcsc2fusion.service (lazy import)
  http_transport.py           local HTTP server: page, POST /api/act, SSE /api/events, no adsk
  browser_window.py           finds Edge/Chrome, opens the app mode window, tracks and closes it
  fusion_adapter.py           the ONLY module importing adsk: main thread dispatcher, buttons,
                              command, worker thread, show/stop of the window
  palette/                    index.html, app.js, style.css, mock-bridge.js, http-bridge.js
  resources/                  command button icons
```

All Fusion API calls run on the main thread. Network work runs on a worker thread; its result is
queued and handed back with `app.fireCustomEvent` and a custom event handler. The same mechanism
carries requests from the HTTP server threads to the controller (`MainThreadDispatcher`).

## How the window works

```
Fusion button -> add-in (main thread) -> BrowserWindow.launch(http://127.0.0.1:PORT/?t=TOKEN)
Edge or Chrome --app (own profile)  <--HTTP-->  HttpTransport (thread in the add-in)
  page -> POST /api/act -> dispatcher -> custom event -> Controller.handle (main thread)
  Controller -> HubSink -> Hub -> SSE /api/events -> page
```

- The server binds 127.0.0.1 on a random port. A one time launch token is exchanged for an
  `HttpOnly; SameSite=Strict` session cookie. Every request checks Host, cookie and Origin, POSTs
  need JSON and an `X-LCSC` header, there is no CORS and the CSP is `default-src 'self'`.
- The window is Edge (Windows, always present) or Chrome in app mode with a private profile in
  `%LOCALAPPDATA%\LCSC2Fusion\browser-profile`. Without either browser the page opens in the default
  browser tab instead (it cannot be closed from Python then).
- The button "LCSC2Fusion" is registered in the Design, Schematic, Board and Library
  environments (panels listed in `fusion_adapter.TARGETS`), so the window can be reopened from
  anywhere. The log lines `button: <workspace> / <panel> -> added|exists|missing|failed` show
  which panels exist on your Fusion build. A second click focuses the existing window.
- `runOnStartup` is true: the add-in only registers buttons and the server at Fusion start, the
  window is shown on the first click. Run manually from Scripts and Add-Ins and the window opens
  at once. Stop closes the window.
- `LCSC2FUSION_UI=palette` (environment variable) starts the old docked palette instead. Temporary,
  to compare until gate 4 passed (D-022).

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
LCSC2Fusion from the Add-Ins tab. The window opens at once; afterwards the LCSC2Fusion button is in
Utilities > ADD-INS of the Design, Schematic, Board and Library environments (see the Text Commands
window and Fusion's log file for the `button:` lines if one is missing).

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

## The page over HTTP (http-bridge.js)

`palette/http-bridge.js` lets the same page (`index.html` and `app.js`, unchanged) run in a normal
browser window that the add-in serves from a local HTTP server. It is loaded before `app.js` and
installs itself only when the page is served over `http:` and no `adsk` object exists, so the
Fusion palette and the `file://` mock mode behave as before.

- Page to Python: `window.adsk.fusionSendData(action, data)` becomes `POST /api/act` (JSON
  `{action, data}`, header `X-LCSC: 1`, session cookie) and resolves with the reply string. It
  never rejects and never hangs: a network error, HTTP 403 or 5xx, a body that is not JSON, or 35 s
  without an answer give `'ERROR'`, and the page also receives an `error` message so that the
  failure is visible.
- Python to page: one `EventSource` on `/api/events`; each message `{action, data}` goes to
  `window.fusionJavaScriptHandler.handle`. Messages that arrive before `app.js` has defined the
  handler are queued (200 at most) and delivered in order. The `overflow` marker of the server is
  ignored (the stream ends and the browser reconnects with Last-Event-ID).
- A connection without Last-Event-ID starts at the head of the stream, so nothing published before
  it subscribed is replayed. Therefore the first request waits (3 s at most) until the stream is
  open, and a stream that failed and came back makes the page send `ready` once more, so that
  Python sends its state (busy flag, status line) again.
- Action `shutdown` (published when the add-in stops): the page shows "LCSC2Fusion was stopped in
  Fusion. You can close this window." and closes itself.
- Connection loss: the browser reconnects by itself. After 15 s without a successful open the
  banner "Lost the connection to Fusion..." appears and a fresh `EventSource` is created every
  3 s, together with a `GET /api/ping` probe. The banner hides when the stream is back. After
  90 s without any success the window tries to close itself and keeps the banner.
- All code lives in files (the server sends CSP `default-src 'self'`). The state can be read in
  tests with `window.__lcscHttpBridge.state()` (`connecting`, `open`, `broken`, `stopped`).
- Tests: `tests/test_addin_http_bridge.py` runs the bridge under Node with a fake DOM and clock,
  and, when Playwright is installed in some Python on PATH, in a real browser against a small
  fixture server (`LCSC_BRIDGE_SHOTS=<folder>` keeps the screenshots).

## Behaviour

- Libraries are written to `~/LCSC2Fusion/libraries/<id>.lbr` (created on demand).
- After saving, the window shows an "Open in Fusion Electronics" button. Pressing it runs the
  undocumented text command `Document.newDesignFromLocal "<path>"` on Fusion's main thread, from
  the custom event handler (D-006 variant A; confirmed for a palette handler, still to be confirmed
  for this context in gate 4, fallback: the `fusion360://` protocol, D-022). Nothing is opened
  automatically. When the command fails, the palette shows the
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

The placed model lined up with the footprint in Fusion for C25804, C2040 and C165948 (D-021).

## Tests

```
python -m pytest tests/test_addin_*.py
```

The controller, file and build tests need nothing from Fusion. `test_addin_adapter.py` uses the
official `adsk` API stubs, which are licensed CC BY-NC-SA and therefore not in this repository. Set
`ADSK_DEFS` to `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs` (the default location is
tried automatically); without the stubs those tests are skipped.
