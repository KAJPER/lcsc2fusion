# LCSC2Fusion add-in

Autodesk Fusion add-in: search LCSC parts, press "Add to library", get an Eagle `.lbr` (symbol and
footprint, optionally a 3D model) written by the `lcsc2fusion` core. The UI is a separate browser
window, like SnapEDA's own window, served by a small local HTTP server inside the add-in. Fusion
deletes every API palette whenever the user switches workspace (for example when a library opens in
the Electronics editor), so a palette cannot survive that; the separate window does (D-022).
Status: the window architecture and the accumulating library were confirmed inside Fusion (gates 4 and 5,
DECISIONS.md D-027). The restyled window, the settings, the LCSC links and the local API for the MCP server
are covered by automated tests and a real browser, and still wait for gate 6 of `docs/MANUAL_TEST.md`.

## Layout

```
addin/LCSC2Fusion/            folder name == .py name == .manifest name (Fusion requires it)
  LCSC2Fusion.manifest        add-in manifest
  LCSC2Fusion.py              run(context) / stop(context), sys.path setup, error reporting
  controller.py               UI logic and message protocol, no adsk (unit tested)
  core_service.py             PartService on top of lcsc2fusion.service (lazy import)
  http_transport.py           local HTTP server: page, POST /api/act, SSE /api/events, no adsk
  local_api.py                job tracker and bearer token API for the MCP server, no adsk
  browser_window.py           finds Edge/Chrome, opens the app mode window, tracks and closes it
  library_registry.py         state file (library id, added parts, package fingerprints), no adsk
  fusion_library_flow.py      accumulating library + 3D package sequence on a fake-able API, no adsk
  fusion_adapter.py           the ONLY module importing adsk: main thread dispatcher, buttons,
                              command, worker thread, show/stop of the window
  palette/                    index.html, app.js, style.css, theme-init.js, i18n.js,
                              mock-bridge.js, http-bridge.js, window icon files
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
- `LCSC2FUSION_UI=palette` (environment variable) starts the old docked palette instead. Temporary;
  gate 4 has passed, so the mode is not needed any more (D-027) and may be removed.
- Look and links of the page: one blue accent, cool greys, light and dark (System by default, or
  forced in the settings, see "Settings" below);
  the scales (spacing, radius, type) and the contrast rules are documented at the top of
  `palette/style.css` and checked by `tests/test_addin_files.py`. A result row and a saved card show
  the LCSC id as a link to its product page (`lcsc_url` of the `results` message, built by
  `lcsc2fusion.links.lcsc_product_url`), plus a "Datasheet" link when the datasheet is https on
  lcsc.com, jlcpcb.com or easyeda.com. They are plain anchors with `target="_blank"` and
  `rel="noopener noreferrer"`: Edge or Chrome opens a new tab and the window stays. `app.js`
  checks every URL again before it sets an `href`. Whether links open from the old docked
  palette is not verified.

## Settings: theme and language

The "Settings" button in the header opens a small panel (a disclosure: `aria-expanded`, Escape
closes it and returns the focus to the button) with two radio groups.

- Theme: System, Light, Dark. System follows the operating system (`prefers-color-scheme`). A forced
  choice puts `data-theme="light"` or `"dark"` on `<html>`; no attribute means System. `style.css`
  keeps the dark tokens twice, under `@media (prefers-color-scheme: dark)` for System and under
  `:root[data-theme="dark"]` for Dark (CSS cannot share one list between a media query and an
  attribute selector, and `light-dark()` would need Chrome 123); `tests/test_addin_i18n.py` fails
  when the two lists differ. `color-scheme` and the two `theme-color` metas follow the choice.
- Language: English or Polski. The first run takes the browser language (`navigator.language`
  starting with `pl` gives Polish, anything else English); a saved choice wins.

Both choices are kept in `localStorage` of the window's own browser profile
(`%LOCALAPPDATA%\LCSC2Fusion\browser-profile`), under `lcsc2fusion.theme` (`system`, `light`,
`dark`) and `lcsc2fusion.lang` (`en`, `pl`). Nothing else is stored. Every access is wrapped in
try/catch (blocked storage means a choice lasts until the window closes), and a stored value that
is not in the allowed set is ignored. `palette/theme-init.js` owns the keys and is a classic script
in `<head>` (the CSP forbids inline scripts), so the saved theme and language are on `<html>`
before the first paint. `app.js` and the other scripts never touch the storage.

How the texts work (`palette/i18n.js`, loaded before `http-bridge.js` and `app.js`):

- Interface texts are `DICTIONARIES[language][key]`. `index.html` marks them with `data-i18n="key"`
  (text) and `data-i18n-attr="aria-label:key;title:key2"` (attributes), the English text in the
  HTML is the default and equals the English dictionary (a test compares them). `app.js` uses
  `t(key, params)`; `{name}` placeholders and plural forms (`{one, few, many, other}` with
  `params.n`) are supported. Whatever a node shows keeps its key (or the raw message) in a
  `data-*` attribute or in `state`, so a language switch rewrites every text without losing state:
  the results, the saved cards, open `<details>`, the typed query and the status line stay.
- Texts that Python sends (status lines, errors, warnings, the saved card "instructions") are
  English and the protocol is unchanged. The page translates them with `MESSAGES` in `i18n.js`:
  an ordered list of `{ re, pl }` entries (anchored regex with named groups, template per
  language). A message without an entry is shown as it came, so nothing breaks, and warnings that
  quote EasyEDA data (pad names and the like) have no entry on purpose.
  `tests/test_addin_i18n_messages.py` collects the messages from the Python sources and from runs of
  the controller and the flow, and fails when one has no entry, so a changed or new Python text
  needs a new pattern.
- The captions that Python texts quote ("Add to Fusion library", "Open as a separate library") are
  `button.addToFusion` and `button.openSeparate`; the Polish texts quote the Polish captions.

To add a language (say German, `de`): add `{ code: 'de', name: 'Deutsch' }` to `LANGUAGES` in
`palette/theme-init.js`; add `de: { ... }` with every key of `en` to `DICTIONARIES` in `i18n.js`;
add a `de:` template to every entry of `MESSAGES`. `tests/test_addin_i18n.py` checks that the key
sets, the placeholders and the message entries are complete for every registered language and that
no text contains a dash character (the project rule: use a comma, colon or a word).

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
- After saving, the card offers "Open as a separate library". Pressing it runs the
  undocumented text command `Document.newDesignFromLocal "<path>"` on Fusion's main thread, from
  the custom event handler (D-006 variant A; it worked from this context in the Fusion log of gate 5, D-026;
  fallback: the `fusion360://` protocol, D-022). Nothing is opened
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

`test_addin_i18n.py` (settings, dictionaries, theme tokens) and `tests/js/i18n_harness.js` need Node;
`test_addin_http_bridge.py` also drives a real Chromium through Playwright when some Python on the
machine has it (the browser runs with the locale `en-US`, so the machine language does not matter).
The controller, file and build tests need nothing from Fusion. `test_addin_adapter.py` uses the
official `adsk` API stubs, which are licensed CC BY-NC-SA and therefore not in this repository. Set
`ADSK_DEFS` to `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs` (the default location is
tried automatically); without the stubs those tests are skipped.

## Accumulating library and 3D package (D-024)

With "Add to the Fusion library right away" ticked (default) the part is written as before and then handed
to Fusion on the main thread, in the background (about 45 s, progress shown in the window):

1. First part: `Document.newDesignFromLocal` of the part's `.lbr`, `saveAs("LCSC2Fusion Library")` in the root
   folder of the active hub project. Later parts: the library is opened from its stored id (or found by name),
   then `Electron.run` `Use`, `Copy "<DEVICESET>.dev@<file>"`, `Use "-<file>"` (the same three commands SnapEDA
   uses) and `document.save`. The active document of the user is never closed.
2. With the 3D box ticked: `Electron.Create3DPackage "<part.lbr>"`, `saveAs` of the new package design, import of
   `<id>.placed.step`, left exactly as imported (SnapEDA's 90 degree turn is for its own files; for ours it lays the
   part on its side, D-026). The window then asks to check the position and to press Finish, Save, OK in Fusion.
3. `%LOCALAPPDATA%\LCSC2Fusion\state.json` remembers the library id and the added parts. The same part again is not
   merged twice; a package with an existing name but different geometry is renamed before the `.lbr` is written.

Steps 1 to 3 were confirmed inside Fusion in gate 5 (D-026, D-027). A rare Fusion dialog "Can't rename ... Error code: 5"
was seen once and stays under observation. "Open as a separate library" keeps the old behaviour (one `.lbr` opened as its own document) as a fallback.

## Local API for the MCP server

The MCP server (`lcsc2fusion/mcp`, another process) asks the running add-in to add a part to the
Fusion library and follows the progress. It uses the same local HTTP server as the window, under
`/api/local/`, with a secret instead of the window's cookie. Nothing needs to be configured: the
add-in creates a new random token (256 bit) at every start.

Discovery: while the add-in runs it writes `%LOCALAPPDATA%\LCSC2Fusion\api.json` (next to
`state.json`; `LCSC2FUSION_API` overrides the path, other systems use the usual per-user place, see
`lcsc2fusion/discovery.py`) with `{"version": 1, "url": "http://127.0.0.1:<port>", "token": "...",
"pid": <int>}`. The file is replaced atomically, readable by the user only where the file system
supports it, and removed again when the add-in stops (only when it still holds this add-in's token).
When the file cannot be written the add-in logs `MCP discovery file not written: ...` and works
without the MCP side. The token is never logged. The API exists only in the window mode, not with
`LCSC2FUSION_UI=palette`.

| Route | Purpose | Answer |
| --- | --- | --- |
| `GET /api/local/ping` | is the add-in there | `200 {"ok": true, "version": 1, "busy": bool}` |
| `POST /api/local/add` | start a job | `202 {"job": n}` |
| `GET /api/local/job/<n>` | follow a job | `200` job document, `404 {"error": "unknown_job"}` |

`POST /api/local/add` takes `{"lcsc_id": "C2040", "with_3d": false, "fusion": true}`. Only `lcsc_id` is
required (trimmed, upper-cased, `C` and 1 to 12 digits); `with_3d` defaults to false and `fusion` (hand
the part to the accumulating "LCSC2Fusion Library") to true; both must be real JSON booleans; unknown
keys are ignored. Errors are JSON `{"error": kind, "message": text}`:

- `400 bad_request`: invalid fields, or the controller refused the request (its message).
- `409 busy`: the window or an earlier API job is still working; nothing was started.
- `503 unavailable`: Fusion's main thread did not take the request in 30 s (for example a modal
  dialog is open) or the add-in is stopping. No job exists after it.
- `500 internal`: a bug; details are in the Text Commands window.

Job document (the exact fields; `ok` is null while running):

```
{"job": 1, "lcsc_id": "C2040", "state": "running" | "done" | "failed", "ok": null | true | false,
 "status": "latest status line", "message": "final outcome text", "steps": ["status lines, newest 50"],
 "added": null | {"path", "mpn", "warnings", "step_path", "placed_step_path", "with_3d"},
 "fusion": null | {"created_library", "merged", "already_present", "three_d", "library_name",
                   "message", "warnings"}}
```

A job is `done` when the `fusion_done` message says ok (or, with `fusion` false, as soon as the part is
saved), and `failed` when `fusion_done` says not ok or an error is reported. `added` is set once the
`.lbr` is written, so a failed job can still carry a usable file. Polling: the add takes 10 to 60 s
(about 45 s with the Fusion hand-off), so poll every 1 to 2 s. The last 20 jobs are kept; the add-in
does not time a job out. Only one job runs at a time, in line with the window; if the add-in's
controller is idle while a job still says running for more than 60 s, that job is marked failed
("Lost track of the job") so that it cannot block the API for good.

Security (checked in this order for every `/api/local/` request, see `http_transport.py`):

1. Host must be exactly `127.0.0.1:<port>` (DNS rebinding).
2. Any `Origin` header means 403. Browsers send it on cross-origin requests; the MCP client never does.
   So a web page cannot call the API even if it guessed the port.
3. `Authorization: Bearer <token>` compared in constant time, else 401 (plain text, `WWW-Authenticate:
   Bearer`). At most 20 failed attempts per minute are written to the log; there is no lockout.
4. The method must match the route (405 with `Allow`). POST also needs `X-LCSC: 1`,
   `Content-Type: application/json` and a JSON object body of at most 64 KiB.

The local routes ignore the session cookie, and the window's routes ignore the bearer token, so a page
in the window cannot use the API and the MCP client cannot read the window's page or event stream. There
are no CORS headers. Quick check from a shell, with the values from `api.json`:

```
curl -s -H "Authorization: Bearer TOKEN" http://127.0.0.1:PORT/api/local/ping
curl -s -X POST -H "Authorization: Bearer TOKEN" -H "X-LCSC: 1" -H "Content-Type: application/json" \
  -d '{"lcsc_id": "C2040"}' http://127.0.0.1:PORT/api/local/add
curl -s -H "Authorization: Bearer TOKEN" http://127.0.0.1:PORT/api/local/job/1
```

How it is wired (`fusion_adapter.AddinApp._start_window_stack`): the controller gets a `TeeSink`, which
forwards every message to the window's sink first and then to the `JobTracker`, so the window behaves
exactly as before. `LocalApi.submit` calls the same `MainThreadDispatcher.dispatch("add", ...)` as the
window, from the HTTP request thread. A part added in the window is not tracked (the tracker only
follows jobs started through the API), but it makes `busy` true.
Limitation: while an API job runs, an error message caused by a click in the window would also fail the
API job, because both share the controller's one message stream. Leave the window alone meanwhile.

## Window icon

`scripts/make_icons.py` (Pillow, development only) generates `palette/favicon.ico`, `icon-192.png`, `icon-512.png`,
`manifest.webmanifest` and the toolbar PNGs in `resources/` from one drawing. The generated files are committed.
