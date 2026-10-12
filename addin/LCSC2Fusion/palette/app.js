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
    ADD_TO_FUSION: 'add_to_fusion',
  });

  const FROM_PYTHON = Object.freeze({
    STATUS: 'status',
    RESULTS: 'results',
    ADDED: 'added',
    BUSY: 'busy',
    ERROR: 'error',
    FUSION_DONE: 'fusion_done',
  });

  const LEVELS = Object.freeze({ info: 'Info', ok: 'OK', warn: 'Warning', error: 'Error' });
  const BRIDGE_WAIT_MS = 1500;
  const BRIDGE_POLL_MS = 50;
  const MAX_SAVED_ITEMS = 10;
  const BUSY_TEXT = 'Another request is still running. Please wait for it.';
  const BUTTON_ADD_TO_FUSION = 'Add to Fusion library';
  const BUTTON_OPEN_SEPARATE = 'Open as a separate library';

  // The "Include 3D model" and "Add to the Fusion library" choices live in the checkboxes only, so
  // they last as long as the palette page does and nothing is written to browser storage. A part
  // added without 3D can be added again with 3D, but not twice in the same mode.
  //
  // `fusion` holds what the page knows about the hand-off of each part to the Fusion library, by
  // LCSC id (the controller remembers only the latest add of an id as well):
  //   { phase: 'idle' | 'sending' | 'done', result: fusion_done message | null, before: entry | null }
  // `before` is the entry to go back to when a request is refused.
  const state = {
    busy: false,
    pendingId: null,
    pendingFusion: false,
    addedIds: new Set(),
    added3dIds: new Set(),
    fusion: new Map(),
    bridge: null,
  };

  // The parts of a saved library card that change later, kept off the DOM nodes themselves.
  const cardParts = new WeakMap();

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
    state.pendingFusion = false;
    // A refused hand-off request gets no fusion_done: put its card back (a hand-off that is really
    // running sends fusion_done later and corrects the card again).
    state.fusion.forEach((entry, id) => {
      if (entry.phase === 'sending') state.fusion.set(id, entry.before || { phase: 'idle', result: null });
    });
    refreshAllFusion();
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
    if (!value) {
      state.pendingId = null;
      state.pendingFusion = false;
      settleSendingFusion();
    }
    refreshAllFusion();
    updateAddButtons();
  }

  // The controller sends fusion_done before it clears busy, so a card still "sending" when the
  // page goes idle never got its answer: say so and offer the button again.
  function settleSendingFusion() {
    state.fusion.forEach((entry, id) => {
      if (entry.phase !== 'sending') return;
      state.fusion.set(id, {
        phase: 'done',
        before: null,
        result: {
          lcsc_id: id,
          ok: false,
          created_library: false,
          merged: false,
          already_present: false,
          three_d: 'none',
          library_name: '',
          message: 'Fusion did not report back. Try again.',
          warnings: [],
        },
      });
    });
  }

  function wants3d() {
    return $('with-3d').checked;
  }

  function wantsFusion() {
    return $('to-fusion').checked;
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
    const fusionRequested = state.pendingFusion;
    state.pendingId = null;
    state.pendingFusion = false;
    const hasModel = Boolean(data.step_path || data.placed_step_path);
    state.fusion.set(data.lcsc_id, {
      phase: fusionRequested ? 'sending' : 'idle',
      result: null,
      before: null,
    });

    const item = el('li', 'added-item');
    item.dataset.lcsc = data.lcsc_id;
    const parts = {
      data: data,
      fusionBlock: null,
      fusionButton: null,
      modelHelp: null,
      instructions: null,
    };
    cardParts.set(item, parts);
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
    parts.fusionBlock = buildFusionBlock();
    item.appendChild(parts.fusionBlock);
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
    details.open = !data.opened && !fusionRequested;
    details.appendChild(el('summary', null, 'How to use it in Fusion'));
    details.appendChild(el('p', null, data.instructions));
    item.appendChild(details);
    parts.instructions = details;

    const actions = el('div', 'added-actions');
    const fusionButton = el('button', 'primary', BUTTON_ADD_TO_FUSION);
    fusionButton.type = 'button';
    fusionButton.dataset.fusion = data.lcsc_id;
    fusionButton.setAttribute('aria-label', BUTTON_ADD_TO_FUSION + ' for ' + data.lcsc_id);
    actions.appendChild(fusionButton);
    parts.fusionButton = fusionButton;
    const libraryButton = el('button', 'small', BUTTON_OPEN_SEPARATE);
    libraryButton.type = 'button';
    libraryButton.dataset.library = data.path;
    libraryButton.title = 'Opens only this part as its own library in the Fusion Electronics editor';
    actions.appendChild(libraryButton);
    const folderButton = el('button', 'small', 'Open folder');
    folderButton.type = 'button';
    folderButton.dataset.folder = data.path;
    actions.appendChild(folderButton);
    item.appendChild(actions);
    if (hasModel) {
      const help = buildModelHelp(data);
      parts.modelHelp = help.querySelector('.model-help');
      item.appendChild(help);
    }

    // An older card of the same part would show a state that no longer applies: the controller
    // hands over the latest add only, so only the newest card keeps the Fusion controls.
    Array.from($('added-list').children).forEach((card) => {
      if (card.dataset.lcsc === data.lcsc_id) retireFusionControls(card);
    });
    const list = $('added-list');
    list.insertBefore(item, list.firstChild);
    while (list.children.length > MAX_SAVED_ITEMS) list.removeChild(list.lastChild);
    $('added-section').hidden = false;
    applyFusion(item);
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

  // Fusion library state of a card ----------------------------------------------------------------

  function buildFusionBlock() {
    const block = el('div', 'fusion fusion-idle');
    const line = el('p', 'fusion-line');
    line.appendChild(el('strong', 'fusion-head'));
    line.appendChild(document.createTextNode(' '));
    line.appendChild(el('span', 'fusion-detail'));
    block.appendChild(line);
    block.appendChild(el('p', 'fusion-3d'));
    block.appendChild(el('ul', 'warnings fusion-warnings'));
    return block;
  }

  function threeDLine(result, warnings) {
    // The flow words a failed 3D step as a warning that starts with "3D:"; show it here once.
    const own = warnings.findIndex((text) => text.indexOf('3D: ') === 0);
    const reason = own >= 0 ? warnings[own].slice(4) : 'see the warnings below.';
    const rest = own >= 0 ? warnings.filter((text, index) => index !== own) : warnings;
    if (result.three_d === 'started') {
      return {
        kind: 'started',
        rest: rest,
        text: '3D package opened: check the model position, then Finish, Save, OK in Fusion.',
      };
    }
    if (result.three_d === 'failed') return { kind: 'problem', rest: rest, text: '3D failed: ' + reason };
    if (result.three_d === 'skipped') return { kind: 'problem', rest: rest, text: '3D skipped: ' + reason };
    return { kind: 'none', rest: warnings, text: '' };
  }

  // What a card says about its part and the Fusion library, from the entry in state.fusion.
  function describeFusion(entry) {
    const view = { kind: 'idle', head: 'Not in the Fusion library yet.', detail: '', threeD: null, warnings: [] };
    if (entry.phase === 'sending') {
      view.kind = 'sending';
      view.head = 'Sending to Fusion...';
      view.detail = 'Fusion may not respond for up to a minute.';
      return view;
    }
    const result = entry.result;
    if (entry.phase !== 'done' || !result) return view;
    const warnings = Array.isArray(result.warnings) ? result.warnings.map(String) : [];
    if (!result.ok) {
      view.kind = 'fail';
      view.detail = result.message || '';
      view.head = 'Not added to the Fusion library' + (view.detail ? ':' : '.');
      view.warnings = warnings;
      return view;
    }
    view.head = 'In ' + (result.library_name || 'the Fusion library') + ':';
    if (result.already_present) view.detail = 'already there, not added again.';
    else if (result.created_library) view.detail = 'created the library with this part.';
    else if (result.merged) view.detail = 'added to the library.';
    else view.detail = 'saved.';
    const threeD = threeDLine(result, warnings);
    view.threeD = threeD.kind === 'none' ? null : threeD;
    view.warnings = threeD.rest;
    view.kind = threeD.kind === 'problem' || threeD.rest.length > 0 ? 'warn' : 'ok';
    return view;
  }

  // Writes the state of one card into its DOM (text only).
  function applyFusion(card) {
    const parts = cardParts.get(card);
    if (!parts || !parts.fusionBlock) return;
    const entry = state.fusion.get(parts.data.lcsc_id) || { phase: 'idle', result: null, before: null };
    const view = describeFusion(entry);
    const block = parts.fusionBlock;
    block.className = 'fusion fusion-' + view.kind;
    block.querySelector('.fusion-head').textContent = view.head;
    block.querySelector('.fusion-detail').textContent = view.detail;

    const threeD = block.querySelector('.fusion-3d');
    threeD.hidden = !view.threeD;
    threeD.className = 'fusion-3d' + (view.threeD ? ' fusion-3d-' + view.threeD.kind : '');
    threeD.textContent = view.threeD ? view.threeD.text : '';

    const list = block.querySelector('.fusion-warnings');
    list.replaceChildren();
    view.warnings.forEach((text) => list.appendChild(el('li', null, text)));
    list.hidden = view.warnings.length === 0;

    const button = parts.fusionButton;
    const inLibrary = view.kind === 'ok' || view.kind === 'warn';
    button.hidden = inLibrary;
    button.disabled = state.busy || view.kind === 'sending';
    button.textContent = view.kind === 'sending' ? 'Sending...' : BUTTON_ADD_TO_FUSION;

    // The manual 3D steps are the fallback: not while Fusion is working on it, and not when it
    // already opened the 3D package. They open by themselves when the hand-off went wrong.
    if (parts.modelHelp) {
      const started = Boolean(view.threeD && view.threeD.kind === 'started');
      parts.modelHelp.hidden = view.kind === 'sending' || started;
      if (view.kind === 'fail' || (view.threeD && view.threeD.kind === 'problem')) {
        parts.modelHelp.open = true;
      }
    }
    if (parts.instructions && view.kind === 'fail') parts.instructions.open = true;
  }

  function retireFusionControls(card) {
    const parts = cardParts.get(card);
    if (!parts) return;
    if (parts.fusionBlock) parts.fusionBlock.remove();
    if (parts.fusionButton) parts.fusionButton.remove();
    parts.fusionBlock = null;
    parts.fusionButton = null;
    // its text points at a button this card no longer has
    if (parts.instructions) parts.instructions.open = false;
  }

  function latestCard(lcscId) {
    return Array.from($('added-list').children).find((card) => card.dataset.lcsc === lcscId) || null;
  }

  function refreshFusion(lcscId) {
    const card = latestCard(lcscId);
    if (card) applyFusion(card);
  }

  function refreshAllFusion() {
    Array.from($('added-list').children).forEach(applyFusion);
  }

  function applyFusionDone(data) {
    state.fusion.set(data.lcsc_id, { phase: 'done', result: data, before: null });
    refreshFusion(data.lcsc_id);
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
      case FROM_PYTHON.FUSION_DONE:
        applyFusionDone(data);
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
      setStatus('warn', BUSY_TEXT);
      return;
    }
    clearError();
    state.pendingId = lcscId;
    state.pendingFusion = wantsFusion();
    updateAddButtons();
    send(TO_PYTHON.ADD, { lcsc_id: lcscId, with_3d: wants3d(), fusion: wantsFusion() });
  }

  // The "Add to Fusion library" button of a saved card: the controller reuses the files of the
  // latest add of this part (and its 3D choice), so the request carries the id only.
  function requestAddToFusion(lcscId) {
    if (state.busy) {
      setStatus('warn', BUSY_TEXT);
      return;
    }
    clearError();
    state.fusion.set(lcscId, {
      phase: 'sending',
      result: null,
      before: state.fusion.get(lcscId) || null,
    });
    refreshFusion(lcscId);
    send(TO_PYTHON.ADD_TO_FUSION, { lcsc_id: lcscId }).then((reply) => {
      if (reply !== 'BUSY' && reply !== 'ERROR') return;
      const entry = state.fusion.get(lcscId);
      if (entry && entry.phase === 'sending') {
        state.fusion.set(lcscId, entry.before || { phase: 'idle', result: null, before: null });
        refreshFusion(lcscId);
      }
    });
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
      const fusion = event.target.closest('button[data-fusion]');
      if (fusion) {
        if (!fusion.disabled) requestAddToFusion(fusion.dataset.fusion);
        return;
      }
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
