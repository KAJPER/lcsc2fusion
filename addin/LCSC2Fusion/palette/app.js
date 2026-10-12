/* LCSC2Fusion palette. Vanilla JS, no network access: everything goes through Fusion's bridge.
 *
 * Bridge (documented by Autodesk):
 *   page to Fusion: adsk.fusionSendData(action, dataString)   (returns a Promise)
 *   Fusion to page: window.fusionJavaScriptHandler.handle(action, data) must return a non empty
 *                   string, an empty string counts as failure.
 * The message protocol is defined in controller.py. Tests compare these two lists with it.
 */
(function () {
  'use strict';

  const TO_PYTHON = Object.freeze({
    READY: 'ready',
    SEARCH: 'search',
    ADD: 'add',
    OPEN_FOLDER: 'open_folder',
    OPEN_LIBRARY: 'open_library',
  });

  const FROM_PYTHON = Object.freeze({
    STATUS: 'status',
    RESULTS: 'results',
    ADDED: 'added',
    BUSY: 'busy',
    ERROR: 'error',
  });

  const LEVELS = Object.freeze({ info: 'Info', ok: 'OK', warn: 'Warning', error: 'Error' });
  const BRIDGE_WAIT_MS = 1500;
  const BRIDGE_POLL_MS = 50;
  const MAX_SAVED_ITEMS = 10;

  // The "Include 3D model" choice lives in the checkbox only, so it lasts as long as the palette
  // page does and nothing is written to browser storage. A part added without 3D can be added
  // again with 3D, but not twice in the same mode.
  const state = {
    busy: false,
    pendingId: null,
    addedIds: new Set(),
    added3dIds: new Set(),
    bridge: null,
  };

  const $ = (id) => document.getElementById(id);

  // Rendering helpers (textContent only, part data is never parsed as HTML) -----------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function setStatus(level, text) {
    const known = Object.prototype.hasOwnProperty.call(LEVELS, level) ? level : 'info';
    $('status').className = 'status status-' + known;
    $('status-badge').textContent = LEVELS[known];
    $('status-text').textContent = text || '';
  }

  function showError(message) {
    $('error-text').textContent = message || 'Unknown error.';
    $('error-box').hidden = false;
    setStatus('error', 'The request failed.');
    state.pendingId = null;
    updateAddButtons();
  }

  function clearError() {
    $('error-box').hidden = true;
    $('error-text').textContent = '';
  }

  function setBusy(value) {
    state.busy = value;
    $('app').setAttribute('aria-busy', String(value));
    $('busy').hidden = !value;
    $('search-button').disabled = value;
    if (!value) state.pendingId = null;
    updateAddButtons();
  }

  function wants3d() {
    return $('with-3d').checked;
  }

  function fileName(path) {
    return String(path).split(/[\\/]/).pop();
  }

  function updateAddButtons() {
    const done = wants3d() ? state.added3dIds : state.addedIds;
    document.querySelectorAll('button[data-add]').forEach((button) => {
      const id = button.dataset.add;
      const added = done.has(id);
      const pending = state.pendingId === id;
      button.disabled = state.busy || added;
      button.textContent = added ? 'Added' : pending ? 'Adding...' : 'Add to library';
    });
  }

  function renderResults(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    const body = $('results-body');
    body.replaceChildren();
    $('results-heading').textContent = 'Results for "' + (data.query || '') + '"';
    $('results-empty').hidden = items.length > 0;
    items.forEach((item) => body.appendChild(buildResultRow(item)));
    $('results-section').hidden = false;
    updateAddButtons();
  }

  function buildResultRow(item) {
    const row = el('tr', 'result-row');
    row.setAttribute('role', 'row');

    const cell = (className, text) => {
      const td = el('td', className, text);
      td.setAttribute('role', 'cell');
      row.appendChild(td);
      return td;
    };

    cell('col-id', item.lcsc_id);
    cell('col-mpn', item.mpn || '-');

    const typeCell = cell('col-type');
    if (item.library_type) {
      const kind = String(item.library_type).toLowerCase() === 'basic' ? 'basic' : 'extended';
      typeCell.appendChild(el('span', 'badge badge-' + kind, item.library_type));
    }

    cell('col-manufacturer', item.manufacturer || '-');
    cell('col-package', item.package || '-');
    const desc = cell('col-desc', item.description || '');
    desc.title = item.description || '';

    const actionCell = cell('col-action');
    const button = el('button', 'primary', 'Add to library');
    button.type = 'button';
    button.dataset.add = item.lcsc_id;
    button.setAttribute('aria-label', 'Add ' + item.lcsc_id + ' ' + (item.mpn || '') + ' to library');
    actionCell.appendChild(button);
    return row;
  }

  function renderAdded(data) {
    state.addedIds.add(data.lcsc_id);
    if (data.with_3d) state.added3dIds.add(data.lcsc_id);
    state.pendingId = null;
    const hasModel = Boolean(data.step_path || data.placed_step_path);

    const item = el('li', 'added-item');
    const title = el('p', 'added-title');
    title.appendChild(el('span', null, data.lcsc_id));
    if (data.mpn) title.appendChild(el('span', null, data.mpn));
    title.appendChild(
      data.opened
        ? el('span', 'badge badge-basic', 'Sent to Fusion')
        : el('span', 'badge', 'Saved')
    );
    if (hasModel) title.appendChild(el('span', 'badge badge-basic', '3D'));
    item.appendChild(title);
    item.appendChild(el('p', 'added-path', data.path));

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    if (warnings.length > 0) {
      const warningList = el('ul', 'warnings');
      warnings.forEach((warning) => warningList.appendChild(el('li', null, warning)));
      item.appendChild(warningList);
    }
    if (data.with_3d && !hasModel) {
      const missing = el('p', 'model-missing');
      missing.appendChild(el('strong', null, 'No 3D model. '));
      missing.appendChild(
        document.createTextNode(
          'A 3D model was requested but none was produced for this part, so only the symbol and footprint were saved.' +
            (warnings.length > 0 ? ' The reason is in the warnings above.' : '')
        )
      );
      item.appendChild(missing);
    }

    const details = el('details');
    details.open = !data.opened;
    details.appendChild(el('summary', null, 'How to use it in Fusion'));
    details.appendChild(el('p', null, data.instructions));
    item.appendChild(details);

    const actions = el('div', 'added-actions');
    const libraryButton = el('button', 'primary', 'Open in Fusion Electronics');
    libraryButton.type = 'button';
    libraryButton.dataset.library = data.path;
    actions.appendChild(libraryButton);
    const folderButton = el('button', null, 'Open folder');
    folderButton.type = 'button';
    folderButton.dataset.folder = data.path;
    actions.appendChild(folderButton);
    item.appendChild(actions);
    if (hasModel) item.appendChild(buildModelHelp(data));

    const list = $('added-list');
    list.insertBefore(item, list.firstChild);
    while (list.children.length > MAX_SAVED_ITEMS) list.removeChild(list.lastChild);
    $('added-section').hidden = false;
    updateAddButtons();
  }

  // Fixed text lives in the <template> of index.html, only the file names are inserted here.
  function buildModelHelp(data) {
    const help = $('model-template').content.cloneNode(true);
    const fill = (selector, path) => {
      const node = help.querySelector(selector);
      if (node) node.textContent = path ? fileName(path) : '';
    };
    fill('.model-pick', data.placed_step_path || data.step_path);
    fill('.model-placed', data.placed_step_path);
    fill('.model-raw', data.step_path);
    if (!data.placed_step_path) help.querySelector('.model-placed-row').remove();
    if (!data.step_path) help.querySelector('.model-raw-row').remove();
    return help;
  }

  // Messages from Python --------------------------------------------------------------------

  function handleMessage(action, rawData) {
    let data;
    try {
      data = rawData ? JSON.parse(rawData) : {};
    } catch (err) {
      console.error('Invalid JSON from Fusion for action ' + action, err);
      return 'ERROR: invalid JSON';
    }
    switch (action) {
      case FROM_PYTHON.STATUS:
        setStatus(data.level, data.text);
        break;
      case FROM_PYTHON.RESULTS:
        renderResults(data);
        break;
      case FROM_PYTHON.ADDED:
        renderAdded(data);
        break;
      case FROM_PYTHON.BUSY:
        setBusy(Boolean(data.value));
        break;
      case FROM_PYTHON.ERROR:
        showError(data.message);
        break;
      default:
        return 'Unexpected action: ' + action;
    }
    return 'OK';
  }

  // Installed immediately: Fusion may call it as soon as the page has loaded.
  window.fusionJavaScriptHandler = {
    handle: function (action, data) {
      try {
        return handleMessage(action, data);
      } catch (err) {
        console.error('Failed to handle ' + action, err);
        return 'ERROR: ' + (err && err.message ? err.message : 'unknown');
      }
    },
  };

  // Messages to Python ----------------------------------------------------------------------

  function send(action, payload) {
    if (!state.bridge) {
      showError('Not connected to Fusion.');
      return Promise.resolve(null);
    }
    try {
      return Promise.resolve(state.bridge.fusionSendData(action, JSON.stringify(payload || {}))).catch(
        (err) => {
          showError('Could not reach Fusion: ' + err);
          return null;
        }
      );
    } catch (err) {
      showError('Could not reach Fusion: ' + err);
      return Promise.resolve(null);
    }
  }

  function requestSearch(query) {
    const text = query.trim();
    if (!text) {
      setStatus('warn', 'Type an LCSC number or a search phrase first.');
      $('query').focus();
      return;
    }
    if (state.busy) {
      setStatus('warn', 'Another request is still running. Please wait for it.');
      return;
    }
    clearError();
    send(TO_PYTHON.SEARCH, { query: text });
  }

  function requestAdd(lcscId) {
    if (state.busy) {
      setStatus('warn', 'Another request is still running. Please wait for it.');
      return;
    }
    clearError();
    state.pendingId = lcscId;
    updateAddButtons();
    send(TO_PYTHON.ADD, { lcsc_id: lcscId, with_3d: wants3d() });
  }

  // Bridge discovery (Fusion injects `adsk`; a plain browser gets the mock) --------------------

  function realBridge() {
    return window.adsk && typeof window.adsk.fusionSendData === 'function' ? window.adsk : null;
  }

  function waitForBridge(done) {
    const started = Date.now();
    (function poll() {
      const found = realBridge();
      if (found) return done(found, false);
      if (Date.now() - started >= BRIDGE_WAIT_MS) return done(null, true);
      setTimeout(poll, BRIDGE_POLL_MS);
    })();
  }

  function deliverFromMock(action, data) {
    window.fusionJavaScriptHandler.handle(action, JSON.stringify(data));
  }

  // Safety net: if Fusion injects `adsk` later than the wait above, reload so the page connects
  // to the real bridge instead of staying in mock mode.
  function reloadWhenRealBridgeAppears() {
    const timer = setInterval(() => {
      if (realBridge()) {
        clearInterval(timer);
        window.location.reload();
      }
    }, 500);
    setTimeout(() => clearInterval(timer), 60000);
  }

  function connect() {
    waitForBridge((found, timedOut) => {
      if (found) {
        state.bridge = found;
      } else if (timedOut && window.LcscMock) {
        state.bridge = window.LcscMock.install(deliverFromMock);
        $('mock-banner').hidden = false;
        reloadWhenRealBridgeAppears();
      } else {
        showError('The Fusion bridge (adsk.fusionSendData) is not available.');
        return;
      }
      send(TO_PYTHON.READY, {});
    });
  }

  // Wiring ------------------------------------------------------------------------------------

  function init() {
    $('search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      requestSearch($('query').value);
    });
    $('error-dismiss').addEventListener('click', clearError);
    $('with-3d').addEventListener('change', updateAddButtons);
    $('results-body').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-add]');
      if (button && !button.disabled) requestAdd(button.dataset.add);
    });
    $('added-list').addEventListener('click', (event) => {
      const library = event.target.closest('button[data-library]');
      if (library) {
        clearError();
        send(TO_PYTHON.OPEN_LIBRARY, { path: library.dataset.library });
        return;
      }
      const folder = event.target.closest('button[data-folder]');
      if (folder) send(TO_PYTHON.OPEN_FOLDER, { path: folder.dataset.folder });
    });
    $('query').focus();
    connect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
