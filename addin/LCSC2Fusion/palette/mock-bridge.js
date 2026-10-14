/* Mock bridge for inspecting the palette in a normal browser. NOT used inside Fusion.
 *
 * This file only defines window.LcscMock.install(deliver). app.js calls it when no real
 * `adsk.fusionSendData` shows up within a short wait. It never touches window.adsk, so nothing
 * changes when the page runs in Fusion. `deliver(action, data)` pushes a message to the page the
 * way palette.sendInfoToHTML would. Try: C2040, "resistor", "none" (no hits), "fail" (error).
 * With "Include 3D model" ticked, adding C2913202 shows the "no 3D model" case; the other parts
 * get a 3D model.
 *
 * "Add to the Fusion library" (the `fusion` flag of `add`, or the `add_to_fusion` button) plays
 * the hand-off with progress messages and a final `fusion_done`:
 *   - the first part creates the library, later ones are merged into it;
 *   - the same part again is "already present" (nothing merged);
 *   - with 3D the 3D package opens ("started"), except for C14663, where the 3D step fails;
 *   - C2913202 fails the first time (nothing reaches the library) and works on the retry.
 *
 * The status texts are the English sentences of controller.py (a query in Python repr quotes), so
 * the page translates them like the real ones and the Polish interface can be inspected here too.
 *
 * The update notice is chosen by the query of the page URL (index.html?update=available):
 *   update=current                nothing newer (the default)
 *   update=available              version 0.2.0 with its installer link
 *   update=available-noinstaller  the release lists no installer, so only the release link shows
 *   update=dismissed              0.2.0 exists but the user already hid it (no banner)
 *   update=disabled               the once a day check is off
 *   update=checking               0.2.0 is known and a check is running that never ends
 *   update=error                  0.2.0 is known and the automatic check just failed (silent)
 *   update_after=<one of these>   what the "Check now" button ends with after "checking" (by default
 *                                 the same as update; "checking" never ends; "error" only with a manual
 *                                 press shows its line)
 * The messages carry every field of controller.UPDATE_FIELDS. Dismiss and the checkbox are answered
 * with an `update` message like the controller does; a bad tag or flag is refused with an error.
 */
(function () {
  'use strict';

  // Every field of a controller search hit (controller.RESULT_FIELDS), so the page sees the same
  // shape as in Fusion. `lcsc_url` is built like links.lcsc_product_url does. C2913202 has no
  // datasheet and C8734 is out of stock, to show both states.
  const PRODUCT_URL_PREFIX = 'https://www.lcsc.com/product-detail/';
  const DATASHEET_URL_PREFIX = 'https://www.lcsc.com/datasheet/lcsc_datasheet_';

  function samplePart(fields) {
    return Object.assign(
      {
        image_url: '',
        lcsc_url: PRODUCT_URL_PREFIX + fields.lcsc_id + '.html',
        datasheet_url: DATASHEET_URL_PREFIX + fields.lcsc_id + '.pdf',
      },
      fields
    );
  }

  const SAMPLE_PARTS = [
    samplePart({
      lcsc_id: 'C2040',
      mpn: 'RP2040',
      manufacturer: 'Raspberry Pi',
      package: 'LQFN-56',
      description: 'Dual ARM Cortex-M0+ microcontroller, 264KB SRAM, USB 1.1',
      library_type: 'Extended',
      stock: 120000,
    }),
    samplePart({
      lcsc_id: 'C25804',
      mpn: '0603WAF1002T5E',
      manufacturer: 'UNI-ROYAL',
      package: '0603',
      description: '10kOhms 100mW 0603 thick film resistor, +/-1%',
      library_type: 'Basic',
      stock: 9000000,
    }),
    samplePart({
      lcsc_id: 'C14663',
      mpn: 'CC0603KRX7R9BB104',
      manufacturer: 'YAGEO',
      package: '0603',
      description: '100nF 50V X7R +/-10% 0603 multilayer ceramic capacitor',
      library_type: 'Basic',
      stock: 7000000,
    }),
    samplePart({
      lcsc_id: 'C2913202',
      mpn: 'ESP32-S3-WROOM-1-N16R8',
      manufacturer: 'Espressif Systems',
      package: 'SMD-41',
      description:
        'Wi-Fi and Bluetooth LE module with dual core Xtensa LX7, 16MB flash and 8MB octal PSRAM, PCB antenna, a deliberately long description to test line clamping',
      library_type: 'Extended',
      stock: 5000,
      datasheet_url: '',
    }),
    samplePart({
      lcsc_id: 'C8734',
      mpn: 'STM32F103C8T6',
      manufacturer: 'STMicroelectronics',
      package: 'LQFP-48',
      description: 'ARM Cortex-M3 microcontroller, 72MHz, 64KB flash, 20KB SRAM',
      library_type: 'Extended',
      stock: 0,
    }),
  ];

  const MOCK_MANUAL_STEPS =
    'File > Open > Open from my computer, select the .lbr file, then File > Save into a project so it appears in the Library Manager.';
  const MOCK_INSTRUCTIONS =
    'Press "Add to Fusion library" to merge the part into your LCSC2Fusion Library. To use just this part on its own, press "Open as a separate library", then use File > Save in the editor and pick a project so the library appears in the Library Manager. If that does not work, do it manually: ' +
    MOCK_MANUAL_STEPS;
  const LIBRARY_NAME = 'LCSC2Fusion Library';
  const FAILS_FIRST_ID = 'C2913202';
  const BROKEN_3D_ID = 'C14663';

  // The update notice. The URLs are built like the ones Python builds from a validated version.
  const RELEASES_URL = 'https://github.com/KAJPER/lcsc2fusion/releases/';
  const MOCK_CURRENT = '0.1.0';
  const MOCK_LATEST = '0.2.0';
  const UPDATE_KINDS = [
    'available',
    'available-noinstaller',
    'current',
    'error',
    'disabled',
    'checking',
    'dismissed',
  ];
  const RELEASE_TAG = /^v(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$/;

  // A query parameter of the page URL that names an update kind, else `fallback`. There is no
  // `location` under Node, and a file URL may carry no query at all.
  function updateKindParam(name, fallback) {
    try {
      const search =
        typeof location !== 'undefined' && location && typeof location.search === 'string'
          ? location.search
          : '';
      const value = new URLSearchParams(search).get(name);
      return UPDATE_KINDS.indexOf(value) >= 0 ? value : fallback;
    } catch (err) {
      return fallback;
    }
  }

  function install(deliver) {
    const later = (ms, fn) => setTimeout(fn, ms);

    function search(query) {
      deliver('busy', { value: true });
      deliver('status', { level: 'info', text: "Searching for '" + query + "'..." });
      later(600, () => {
        const q = query.toLowerCase();
        if (q === 'fail') {
          deliver('error', { message: 'Mock: the search service is not reachable.' });
          deliver('busy', { value: false });
          return;
        }
        let items;
        if (/^c\d+$/.test(q)) {
          items = SAMPLE_PARTS.filter((part) => part.lcsc_id.toLowerCase() === q);
        } else if (q === 'none') {
          items = [];
        } else if (q === 'resistor' || q === 'all') {
          items = SAMPLE_PARTS;
        } else {
          items = SAMPLE_PARTS.filter((part) =>
            (part.mpn + ' ' + part.description).toLowerCase().includes(q)
          );
        }
        deliver('results', { query: query, items: items });
        deliver('status', items.length
          ? { level: 'ok', text: 'Found ' + items.length + " part(s) for '" + query + "'." }
          : { level: 'warn', text: "No parts found for '" + query + "'. Try an LCSC number such as C2040 or fewer words." });
        deliver('busy', { value: false });
      });
    }

    const NO_MODEL_ID = 'C2913202';
    const MOCK_DIR = 'C:\\Users\\mock\\LCSC2Fusion\\libraries\\';

    // What the page has added so far, like the controller's memory: id -> { with3d, hasModel, mpn }.
    const addedParts = new Map();
    const libraryParts = new Set();
    const failedOnce = new Set();
    let libraryCreated = false;

    function label(lcscId) {
      const mpn = addedParts.has(lcscId) ? addedParts.get(lcscId).mpn : '';
      return mpn ? lcscId + ' (' + mpn + ')' : lcscId;
    }

    // The hand-off to the Fusion library: progress as `status`, then `fusion_done`, then idle.
    function handOff(lcscId) {
      const info = addedParts.get(lcscId);
      const name = label(lcscId);
      const wantsModel = info.with3d && info.hasModel;
      const steps = [];
      let result;
      if (lcscId === FAILS_FIRST_ID && !failedOnce.has(lcscId)) {
        failedOnce.add(lcscId);
        steps.push(['info', 'Opening the ' + LIBRARY_NAME + '...']);
        result = {
          ok: false, created_library: false, merged: false, already_present: false,
          three_d: wantsModel ? 'skipped' : 'none',
          message: 'Could not merge the part: mock failure in Fusion. The library may be open with unsaved changes: close it without saving before you try again.',
          warnings: [],
        };
      } else {
        const present = libraryParts.has(lcscId);
        const created = !present && !libraryCreated;
        const merged = !present && !created;
        let message;
        if (present) {
          message = name + ' is already in the ' + LIBRARY_NAME + ', so it was not added again.';
        } else if (created) {
          steps.push(['info', 'Creating the ' + LIBRARY_NAME + ' from ' + name + '...']);
          message = 'Created the ' + LIBRARY_NAME + ' with ' + name + '.';
        } else {
          steps.push(['info', 'Opening the ' + LIBRARY_NAME + '...']);
          steps.push(['info', 'Merging ' + name + ' into the library...']);
          steps.push(['info', 'Saving the library...']);
          message = 'Added ' + name + ' to the ' + LIBRARY_NAME + '.';
        }
        let threeD = 'none';
        const warnings = [];
        if (wantsModel) {
          steps.push(['info', 'Creating the 3D package for ' + name + '...']);
          if (lcscId === BROKEN_3D_ID) {
            threeD = 'failed';
            warnings.push('3D: could not import ' + lcscId + '.placed.step: mock error from Fusion. You can import ' + MOCK_DIR + lcscId + '.placed.step by hand.');
            message += ' The 3D package could not be created, see the warnings.';
          } else {
            steps.push(['info', 'Importing the 3D model...']);
            threeD = 'started';
            message += ' The 3D package editor is open: check the position of the model, then click Finish, Save, OK.';
          }
        }
        libraryParts.add(lcscId);
        libraryCreated = true;
        result = {
          ok: true, created_library: created, merged: merged, already_present: present,
          three_d: threeD, message: message, warnings: warnings,
        };
      }
      result.lcsc_id = lcscId;
      result.library_name = LIBRARY_NAME;

      deliver('busy', { value: true });
      deliver('status', { level: 'info', text: 'Sending ' + lcscId + ' to Fusion...' });
      steps.forEach((step, index) => {
        later(500 * (index + 1), () => deliver('status', { level: step[0], text: step[1] }));
      });
      later(500 * (steps.length + 1), () => {
        const level = !result.ok ? 'warn' : result.warnings.length ? 'warn' : result.already_present ? 'info' : 'ok';
        deliver('status', { level: level, text: result.message });
        deliver('fusion_done', result);
        deliver('busy', { value: false });
      });
    }

    function add(lcscId, with3d, fusion) {
      const part = SAMPLE_PARTS.find((candidate) => candidate.lcsc_id === lcscId);
      deliver('busy', { value: true });
      deliver('status', { level: 'info', text: 'Downloading ' + lcscId + (with3d ? ' and building the library and the 3D model...' : ' and building the library...') });
      later(900, () => {
        const withWarnings = lcscId === 'C2913202';
        const hasModel = with3d && lcscId !== NO_MODEL_ID;
        const warnings = withWarnings ? [
          lcscId + ': pad 41 is shared by 9 pads, renamed to 41, 41_2, ...',
          lcscId + ': polygon pad approximated by its bounding box',
        ] : [];
        if (with3d && !hasModel) {
          warnings.push(lcscId + ': no 3D model available');
        }
        deliver('added', {
          lcsc_id: lcscId,
          path: MOCK_DIR + lcscId + '.lbr',
          mpn: part ? part.mpn : '',
          warnings: warnings,
          opened: false,
          instructions: MOCK_INSTRUCTIONS,
          step_path: hasModel ? MOCK_DIR + lcscId + '.step' : null,
          placed_step_path: hasModel ? MOCK_DIR + lcscId + '.placed.step' : null,
          with_3d: with3d,
        });
        const label = part && part.mpn ? lcscId + ' (' + part.mpn + ')' : lcscId;
        let status;
        if (with3d && !hasModel) {
          status = { level: 'warn', text: 'Saved ' + label + ' without a 3D model: none was produced for this part. See the warnings on the result.' };
        } else if (warnings.length) {
          status = { level: 'warn', text: 'Saved ' + label + ' with ' + warnings.length + ' warning(s).' };
        } else {
          status = { level: 'ok', text: hasModel ? 'Saved ' + label + ' and its 3D model to ' + MOCK_DIR.slice(0, -1) : 'Saved ' + label + ' to ' + MOCK_DIR + lcscId + '.lbr' };
        }
        deliver('status', status);
        addedParts.set(lcscId, {
          with3d: with3d,
          hasModel: hasModel,
          mpn: part ? part.mpn : '',
        });
        if (fusion) {
          handOff(lcscId); // the page stays busy until fusion_done, like the controller
        } else {
          deliver('busy', { value: false });
        }
      });
    }

    function addToFusion(lcscId) {
      if (!addedParts.has(lcscId)) {
        deliver('error', { message: lcscId + ' was not added in this session. Press Add to library for it first.' });
        return;
      }
      handOff(lcscId);
    }

    // Like Fusion: opening works for every library except C2913202, which shows the fallback.
    function openLibrary(path) {
      const name = String(path).split(/[\\/]/).pop();
      if (name === 'C2913202.lbr') {
        deliver('status', { level: 'warn', text: 'Fusion did not open ' + name + ' (mock failure). Open it manually: ' + MOCK_MANUAL_STEPS });
      } else {
        deliver('status', { level: 'ok', text: 'Sent ' + name + ' to Fusion for opening. In the editor use File > Save and pick a project so it appears in the Library Manager.' });
      }
    }

    // Update notice: what the mock knows. `known` is the kind of the last result (the cached
    // knowledge Python would send again); an error or a running check is never cached.
    const scenario = updateKindParam('update', 'current');
    const settings = { enabled: scenario !== 'disabled' };
    let known = scenario === 'current' || scenario === 'disabled' ? 'current' : scenario;
    if (known === 'error' || known === 'checking') known = 'available';
    const resultAfterPress = updateKindParam(
      'update_after',
      scenario === 'disabled' ? 'current' : scenario
    );
    let hiddenVersion = scenario === 'dismissed' ? MOCK_LATEST : null;
    let checkedAt = Date.now() / 1000 - 7200;

    // One `update` message with every field of controller.UPDATE_FIELDS.
    function updateMessage(kind) {
      const newer =
        kind === 'available' || kind === 'available-noinstaller' || kind === 'dismissed';
      const version = newer ? MOCK_LATEST : kind === 'current' ? MOCK_CURRENT : null;
      const urls = version !== null;
      return {
        state: newer ? 'available' : kind,
        current: MOCK_CURRENT,
        latest: version,
        release_url: urls ? RELEASES_URL + 'tag/v' + version : null,
        installer_url:
          urls && kind !== 'available-noinstaller'
            ? RELEASES_URL + 'download/v' + version + '/LCSC2Fusion-Setup-' + version + '.exe'
            : null,
        checked_at: checkedAt,
        enabled: settings.enabled,
        dismissed: newer && hiddenVersion === MOCK_LATEST,
      };
    }

    function sendUpdate(kind) {
      deliver('update', updateMessage(kind));
    }

    // `ready`: the cached knowledge, then (for error and checking) an automatic check.
    function sendCachedUpdate() {
      sendUpdate(settings.enabled ? known : 'disabled');
      if (scenario === 'checking' || scenario === 'error') {
        later(700, () => sendUpdate('checking'));
        if (scenario === 'error') {
          later(1500, () => {
            checkedAt = Date.now() / 1000;
            sendUpdate('error');
          });
        }
      }
    }

    // "Check now": "checking" at once, the result a moment later (never, for update_after=checking).
    function checkForUpdate() {
      sendUpdate('checking');
      if (resultAfterPress === 'checking') return;
      later(900, () => {
        checkedAt = Date.now() / 1000;
        if (resultAfterPress !== 'error' && resultAfterPress !== 'disabled') known = resultAfterPress;
        sendUpdate(resultAfterPress === 'disabled' ? 'current' : resultAfterPress);
      });
    }

    // Like the controller: a bad request is an error message and the reply "ERROR".
    function refuse(message) {
      deliver('error', { message: message });
      return 'ERROR';
    }

    function dismissUpdate(tag) {
      if (typeof tag !== 'string' || !RELEASE_TAG.test(tag)) {
        return refuse('Invalid request: tag must be a release tag such as v1.2.3.');
      }
      if (tag === 'v' + MOCK_LATEST) hiddenVersion = MOCK_LATEST;
      sendUpdate(settings.enabled ? known : 'disabled');
      return 'OK';
    }

    function setUpdateCheck(enabled) {
      if (typeof enabled !== 'boolean') {
        return refuse('Invalid request: enabled must be true or false.');
      }
      settings.enabled = enabled;
      sendUpdate(enabled ? known : 'disabled');
      return 'OK';
    }

    return {
      fusionSendData: function (action, dataString) {
        const data = dataString ? JSON.parse(dataString) : {};
        let reply = 'OK';
        switch (action) {
          case 'ready':
            deliver('busy', { value: false });
            deliver('status', { level: 'info', text: 'Ready. Libraries are saved to C:\\Users\\mock\\LCSC2Fusion\\libraries' });
            sendCachedUpdate();
            break;
          case 'search':
            search(data.query);
            break;
          case 'add':
            add(data.lcsc_id, data.with_3d === true, data.fusion === true);
            break;
          case 'add_to_fusion':
            addToFusion(data.lcsc_id);
            break;
          case 'open_library':
            openLibrary(data.path);
            break;
          case 'open_folder':
            deliver('status', { level: 'info', text: 'Mock: would open ' + data.path });
            break;
          case 'check_update':
            checkForUpdate();
            break;
          case 'dismiss_update':
            reply = dismissUpdate(data.tag);
            break;
          case 'set_update_check':
            reply = setUpdateCheck(data.enabled);
            break;
          default:
            deliver('error', { message: 'Mock: unknown action ' + action });
        }
        return Promise.resolve(reply);
      },
    };
  }

  window.LcscMock = { install: install };
})();
