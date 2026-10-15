# LCSC2Fusion add-in

Autodesk Fusion add-in: search LCSC parts, press "Add to library", get an Eagle `.lbr` (symbol and
footprint, optionally a 3D model) written by the `lcsc2fusion` core. The UI is a separate browser
window, like SnapEDA's own window, served by a small local HTTP server inside the add-in. Fusion
deletes every API palette whenever the user switches workspace (for example when a library opens in
the Electronics editor), so a palette cannot survive that; the separate window does (D-022).
Status: the window architecture and the accumulating library were confirmed inside Fusion (gates 4 and 5,
DECISIONS.md D-027). The restyled window, the settings, the LCSC links and the local API for the MCP server
are covered by automated tests and a real browser, and still wait for gate 6 of `docs/MANUAL_TEST.md`.
The update notice (see "Updates") is covered by automated tests only and has not been run inside Fusion.

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
  update_check.py             the GitHub release lookup of the update notice: one request, pure,
                              standard library only, no adsk
  update_service.py           update.json state file, the check schedule and the service the
                              controller uses for the update notice, no adsk
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
closes it and returns the focus to the button) with two radio groups and a third group, Updates
(see "Updates" below; that choice is kept by Python in `update.json`, not in `localStorage`).

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

End users install from a release: download `LCSC2Fusion-Setup-<version>.exe` (or the installer zip, then
double click `Install-LCSC2Fusion.bat`) from the
[Releases page](https://github.com/KAJPER/lcsc2fusion/releases/latest). It copies the add-in to
`%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion` for the current user, with no administrator rights and no
Python. Fusion starts the add-in on its next launch; if Fusion is running, Stop and Run it in Scripts and
Add-Ins (Shift+S). The plain `LCSC2Fusion-<version>-addin.zip` of the same release holds the add-in
folder that the build below produces, for a manual install (also for macOS, untested). The files are built by
`scripts/build_release.py` and published by the release workflow, see `RELEASING.md`.

To build the package yourself (stdlib only), which copies the `lcsc2fusion/` core into the add-in folder:

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

## Updates

The window tells the user when a newer GitHub release exists: the add-in checks, notifies and links the
installer. It never downloads or runs anything (see "Possible one click update" below for why). This request
is the only network access of the add-in's own code; the part data comes from the core (`lcsc2fusion/client`).
Every host that is contacted and what each one receives is listed in the Privacy section of the repository
`README.md`; keep that list in step with the code when a request is added or changed. The lookup
is `update_check.py`, the schedule and `update.json` are `update_service.py`, the protocol is in
`controller.py`, and the banner and the settings group are in `palette/index.html`, `app.js` and `i18n.js`.

**Protocol, page to Python** (JSON object payloads like every action):

- `check_update {force}`: `force` is an optional bool, default false. The "Check now" button sends
  `{force: true}`.
- `dismiss_update {tag}`: `tag` is required and must match
  `^v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$`, for example `v0.2.0`. Anything else is
  answered with `ERROR` and the text `Invalid request: tag must be a release tag such as v1.2.3.`
- `set_update_check {enabled}`: `enabled` is required and must be a real JSON boolean, else `ERROR` with
  `Invalid request: enabled must be true or false.`

All three answer `OK` or `ERROR` like the other actions, and never `BUSY`. Both error texts have entries in
`MESSAGES` of `i18n.js`.

**Protocol, Python to page:** one action `update`, exactly the fields of `controller.UPDATE_FIELDS`:

| Field | Value |
| --- | --- |
| `state` | `available`, `current`, `error`, `disabled` or `checking` |
| `current` | the running version, "0.1.0", without a leading v |
| `latest` | the newest stable release, "0.2.0", or null when nothing is known (also null in `disabled`) |
| `release_url` | `https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0`, or null |
| `installer_url` | `https://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/LCSC2Fusion-Setup-0.2.0.exe`, or null when the release does not list exactly that file |
| `checked_at` | epoch seconds (float) of the last attempt, or null |
| `enabled` | bool, the once a day check |
| `dismissed` | bool, true only in state `available` when the user hid exactly this version |

The message carries codes only: the page words everything and Python sends no free text for updates. The URLs
are built by Python from a validated `X.Y.Z` and are set only while a newer version is known.

When messages are sent:

- `ready` (also after a reconnect of the page): one `update` from the cache (no network). If the automatic
  check is due, a second `update` with state `checking` follows, and later the result.
- `check_update`: when a network check starts, `update` with state `checking` at once and the result later
  (`available`, `current` or `error`). When nothing starts (a check is already running, or a forced check came
  less than 60 s after the previous attempt of this session) exactly one `update` follows, with the current
  knowledge: `checking` while a check runs, else the cached result.
- `dismiss_update` and `set_update_check`: one `update` after the change. Switching the check on also starts an
  automatic check when one is due.

State rules:

- `disabled` is sent whenever the automatic check is off, except as the immediate result of a manual check made
  while it is off (then `available`, `current` or `error`). In `disabled` the page shows no banner.
- `error` is sent only as the result of a check that just failed. Cached knowledge (`ready`, dismiss, set)
  never says `error`. The page shows a note for it only in answer to its own "Check now", and an `error` never
  hides a banner that is already shown.
- Nothing known yet, or nothing newer, is `current` (`latest` is null or not newer than `current`). A downgrade
  is never offered.
- A failed attempt keeps the last successful result, so a known newer version survives a flaky network.

The check runs in the background and never sets `busy`, so it cannot make a search or an add fail. Only the
network part (`UpdateService.fetch_latest`) runs on the controller's worker thread and touches no shared
state. Everything else, including writing `update.json`, runs on Fusion's main thread, so the service needs no
lock. A failed check is written to the add-in log as `Update check failed: <reason code>` (for example
`network`, `timeout`, `tls`, `rate_limited`, `not_found`, `bad_tag`); the page never shows these codes.

**Schedule and limits:**

- Automatic check: when the window opens (`ready`), only if it is on and the last attempt is at least 24 hours
  old, or at least 6 hours old when that attempt failed.
- "Check now" (`force`) ignores both limits, but not the flood limit: two attempts of one session less than
  60 s apart give no second request. The flood limit is counted in memory per add-in session.
- One check at a time. Dismissing a version hides the banner until a newer release appears.
- The request: one `GET https://api.github.com/repos/KAJPER/lcsc2fusion/releases/latest` with the headers
  `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` and
  `User-Agent: LCSC2Fusion/<version>`. No token, cookie, account or machine identifier. Timeout 8 s (also for
  reading the body), response cap 256 KiB, a compressed or larger answer is refused, certificate verification
  on, at most 3 redirects and only to `https://api.github.com`, system proxy settings as any program. The
  network sits behind an injectable `Fetch` callable, so the tests never use it.

**State file.** `%LOCALAPPDATA%\LCSC2Fusion\update.json`, next to `state.json` and `api.json` (the same folder
rules as `library_registry.default_state_path`), written atomically (temporary file in the same folder, then
replace). Format:

```
{"version": 1, "enabled": true, "last_checked_at": 1760000000.0, "last_attempt_ok": true,
 "last_result": {"latest_tag": "v0.2.0", "newer": true, "installer": true},
 "dismissed_tag": null}
```

- `version`: the file format, 1. Another value means the file is not understood.
- `enabled`: the once a day check, default true.
- `last_checked_at`: epoch seconds of the last attempt, successful or not.
- `last_attempt_ok`: whether that attempt succeeded. It selects the 24 or the 6 hour wait.
- `last_result`: the outcome of the last successful check. `latest_tag` is the release tag, `newer` says it was
  newer than the running version when found, `installer` says the release listed the installer file. The
  window's view is recomputed from it against the running version at every start, so after an upgrade a
  banner for the version you just installed disappears.
- `dismissed_tag`: the tag the user hid. The banner is hidden only while it equals the tag of `last_result`.

A missing file means the defaults (the check is on). A file that exists but is not understood (unreadable, not
JSON, not an object, another `version`, an `enabled` that is not a boolean) counts as off, because the user's
choice is unknown and a request is not something to guess about; the log says so and the user can switch the
check on again in Settings. A bad field elsewhere falls back to its default and the rest is kept. When the
file cannot be written the choice lasts until the add-in stops and the log says why.

**Trust model.** The answer from GitHub is untrusted data:

- Of the whole document only `tag_name`, `draft`, `prerelease` and the asset names are read. `draft` and
  `prerelease` must both be false, and `tag_name` must be `v<number>.<number>.<number>` without leading zeros
  and with at most 6 digits per part. The release text and every other field are never shown or interpreted,
  and `html_url` and `browser_download_url` are not used.
- The release page and installer URLs are built in `update_check.py` from the validated version. The installer
  URL exists only when the release lists an asset named exactly `LCSC2Fusion-Setup-<version>.exe`.
- The page validates again, with exact prefixes, in the spirit of `lcscProductUrl`: the release URL must equal
  `https://github.com/KAJPER/lcsc2fusion/releases/tag/v<V>` and the installer URL
  `https://github.com/KAJPER/lcsc2fusion/releases/download/v<V>/LCSC2Fusion-Setup-<V>.exe`, with `<V>` equal to
  `latest` and of the shape `X.Y.Z` without leading zeros. Anything else (another scheme, host, port, userinfo,
  query, fragment or path) gives no link. Anchors are built with DOM APIs only, with `target="_blank"` and
  `rel="noopener noreferrer"`.

**Page.** The banner (`role="status"`, above the search form) appears for state `available` when `dismissed`
is false and `latest` is a valid version: "Version X is available (you have Y).", a "Download installer" link
(only when the installer URL passes the check), "What is new" (the release page), "Dismiss" (sends
`dismiss_update` and hides the banner at once) and a hint with the steps (run the installer, then Shift+S,
Add-Ins, LCSC2Fusion, Stop, Run). Without an installer link the hint says to download the installer from the
release page. The page keeps the last `available`, `current` or `disabled` message as the source of the banner,
treats `checking` as an overlay that never hides it, and remembers whether the user pressed "Check now" so that
only that press shows a result line.

The Updates group of the Settings panel has the checkbox "Check for updates once a day" (shows `enabled`,
sends `set_update_check` on change), the note "Asks api.github.com for the latest release. Nothing about you
is sent." and the "Check now" button, disabled and labelled "Checking..." while a check runs. Under it a
result line appears only after the button was pressed: "You have the latest version (X).", "Version X is
available." or "Could not check for updates. Try again later." The line is rewritten on a language switch.
A second line, "Last check: <date and time>", formats `checked_at` in the language of the window. All texts
are in `i18n.js` in English and Polish.

To see every state without Fusion, open `palette/index.html` in a browser and choose the state with the URL
query `?update=available`, `available-noinstaller`, `current`, `error`, `disabled`, `checking` or `dismissed`
(default `current`). `?update_after=` sets the result of a manual "Check now". The mock never runs inside
Fusion.

**Tests:** `tests/test_addin_update_check.py` (the lookup: tag, flags and asset rules, URLs, failure reasons,
with a fake `Fetch`), `tests/test_addin_update_service.py` (schedule, `update.json`, the states of the view),
`tests/test_addin_update_controller.py` (the three actions and the order of the `update` messages) and
`tests/test_addin_update_ui.py` with its browser driver `tests/js/update_ui_browser.py` (banner, URL
validation and the settings group in the page, in a real browser when Playwright is available). None of
them touches the network: `tests/conftest.py` makes any urllib request to GitHub fail like an offline
machine, so even a test that wires the real service cannot reach the API.

## Possible one click update (design note, not implemented)

Today the add-in only notifies. Releases are not code signed yet, and a self updater that downloads and runs
code would let anyone who gets control of the GitHub account run code on every user's machine. A later one
click update would need all of this, and none of it exists:

1. Signed releases (Authenticode) and a pinned signer subject kept in the add-in.
2. Verification of the downloaded installer with `Get-AuthenticodeSignature` before it is ever started: the
   status must be Valid and the signer subject must be the exact pinned one, not just any valid signature.
3. The `SHA256SUMS.txt` check of the same release as a second, independent check.
4. Download only from the URL the add-in built itself, over HTTPS, into a fresh private temporary folder, with a
   size cap and no redirect to another host.
5. The user confirms in the window before anything is started.
6. The installer is started detached after the add-in announced that it will stop. Every failure leaves the old
   version untouched (the installer already installs atomically and keeps the user's data).

There is no code for it.

## Window icon

`scripts/make_icons.py` (Pillow, development only) generates `palette/favicon.ico`, `icon-192.png`, `icon-512.png`,
`manifest.webmanifest` and the toolbar PNGs in `resources/` from one drawing. The generated files are committed.
