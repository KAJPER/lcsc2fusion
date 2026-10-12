/* LCSC2Fusion palette. Vanilla JS, no network access: everything goes through Fusion's bridge.
 *
 * Bridge (documented by Autodesk):
 *   page to Fusion: adsk.fusionSendData(action, dataString)   (returns a Promise)
 *   Fusion to page: window.fusionJavaScriptHandler.handle(action, data) must return a non empty
 *                   string, an empty string counts as failure.
 * The message protocol is defined in controller.py. Tests compare these two lists with it.
 *
 * Texts: every text the page makes itself comes from i18n.js (t(key, params)); a message that
 * Python sends is English and is translated for display by LcscI18n.translateMessage. A piece of
 * the page that shows a text keeps what it needs to show it again (a key with its params, or the
 * raw message, as data-* attributes or in `state`), so that a language switch redraws everything
 * without losing state. Theme and language settings live in theme-init.js (window.LcscPrefs).
 */
(function () {
  'use strict';

  const I18n = window.LcscI18n;
  const Prefs = window.LcscPrefs;
  const t = (key, params, language) => I18n.t(key, params, language);

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

  const LEVEL_KEYS = Object.freeze({
    info: 'status.level.info',
    ok: 'status.level.ok',
    warn: 'status.level.warn',
    error: 'status.level.error',
  });
  const BRIDGE_WAIT_MS = 1500;
  const BRIDGE_POLL_MS = 50;
  const MAX_SAVED_ITEMS = 10;
  const SKELETON_ROWS = 3;
  // A warning of the Fusion flow about the 3D step starts like this (the first 4 characters are
  // dropped on the card, which words the problem itself). Polish templates keep the prefix.
  const THREE_D_PREFIX = '3D: ';

  // Links that leave the window. The data behind them comes from the network, so each href is
  // checked here before it is set (defence in depth, the controller builds the same URL).
  // The product page: exactly PREFIX + a canonical LCSC id + SUFFIX, nothing else.
  const LCSC_PRODUCT_PREFIX = 'https://www.lcsc.com/product-detail/';
  const LCSC_PRODUCT_SUFFIX = '.html';
  const LCSC_ID_PATTERN = /^C[0-9]{1,12}$/;
  // A datasheet may sit on these hosts and their subdomains, over https only.
  const DATASHEET_DOMAINS = Object.freeze(['lcsc.com', 'jlcpcb.com', 'easyeda.com']);
  const MAX_URL_LENGTH = 2048;

  // The "Include 3D model" and "Add to the Fusion library" choices live in the checkboxes only, so
  // they last as long as the palette page does and nothing is written to browser storage. A part
  // added without 3D can be added again with 3D, but not twice in the same mode.
  //
  // `fusion` holds what the page knows about the hand-off of each part to the Fusion library, by
  // LCSC id (the controller remembers only the latest add of an id as well):
  //   { phase: 'idle' | 'sending' | 'done', result: fusion_done message | null, before: entry | null }
  // `before` is the entry to go back to when a request is refused.
  //
  // `searching` is true from the moment a search is sent until its answer (or failure) arrives:
  // the results area then shows skeleton rows. `parts` keeps the hits of the latest search by
  // LCSC id, so a saved card can show the datasheet link of the hit it came from. `items` are the
  // same hits in order, so the rows can be drawn again after a language switch.
  //
  // `status` and `error` hold a text descriptor, { key, params } or { raw } (see `describe`).
  const state = {
    busy: false,
    pendingId: null,
    pendingFusion: false,
    pendingQuery: '',
    searching: false,
    hasResults: false,
    lastQuery: '',
    items: [],
    status: { level: 'info', text: { key: 'status.connecting' } },
    error: null,
    parts: new Map(),
    addedIds: new Set(),
    added3dIds: new Set(),
    fusion: new Map(),
    bridge: null,
  };

  // The parts of a saved library card that change later, kept off the DOM nodes themselves.
  const cardParts = new WeakMap();

  const $ = (id) => document.getElementById(id);

  // Texts ------------------------------------------------------------------------------------------

  // A text to show: a key of i18n.js, or a raw (English) message from Python.
  function textKey(key, params) {
    return { key: key, params: params };
  }

  function textRaw(raw) {
    return { raw: raw === undefined || raw === null ? '' : String(raw) };
  }

  function describe(text) {
    if (!text) return '';
    return text.raw !== undefined ? I18n.translateMessage(text.raw) : t(text.key, text.params);
  }

  // Rendering helpers (textContent only, part data is never parsed as HTML) -----------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // Text of a node from a key. The key and params stay on the node as data-i18n(-params), so
  // I18n.apply (a language switch) can write it again.
  function setText(node, key, params) {
    node.setAttribute('data-i18n', key);
    if (params) node.setAttribute('data-i18n-params', JSON.stringify(params));
    node.removeAttribute('data-msg');
    node.textContent = t(key, params);
  }

  // Text of a node from a message of Python (English), translated for display.
  function setMessage(node, raw) {
    node.setAttribute('data-msg', String(raw));
    node.removeAttribute('data-i18n');
    node.textContent = I18n.translateMessage(raw);
  }

  // Attributes of a node from keys, e.g. { 'aria-label': 'link.lcscLabel', title: 'link.lcscTitle' }.
  function setAttrs(node, keys, params) {
    node.setAttribute(
      'data-i18n-attr',
      Object.keys(keys)
        .map((name) => name + ':' + keys[name])
        .join(';')
    );
    if (params) node.setAttribute('data-i18n-params', JSON.stringify(params));
    Object.keys(keys).forEach((name) => node.setAttribute(name, t(keys[name], params)));
  }

  function textEl(tag, className, key, params) {
    const node = el(tag, className);
    setText(node, key, params);
    return node;
  }

  function messageEl(tag, className, raw) {
    const node = el(tag, className);
    setMessage(node, raw);
    return node;
  }

  // Links ------------------------------------------------------------------------------------

  // The one place that builds and validates the product URL of a part. Without `suppliedUrl` the
  // URL is built from the id; with one (from the controller) it must be exactly that URL for the
  // same id. Returns the URL or '' when anything is off, and '' means: show no link.
  function lcscProductUrl(lcscId, suppliedUrl) {
    const url = suppliedUrl === undefined ? LCSC_PRODUCT_PREFIX + lcscId + LCSC_PRODUCT_SUFFIX : suppliedUrl;
    if (typeof url !== 'string' || typeof lcscId !== 'string') return '';
    if (!url.startsWith(LCSC_PRODUCT_PREFIX) || !url.endsWith(LCSC_PRODUCT_SUFFIX)) return '';
    const middle = url.slice(LCSC_PRODUCT_PREFIX.length, url.length - LCSC_PRODUCT_SUFFIX.length);
    if (!LCSC_ID_PATTERN.test(middle) || middle !== lcscId) return '';
    return url;
  }

  // A datasheet link is shown only for https and a known host (or one of its subdomains), without
  // credentials or a port. Returns the normalised URL or ''.
  function datasheetUrl(value) {
    if (typeof value !== 'string' || value === '' || value.length > MAX_URL_LENGTH) return '';
    let parsed;
    try {
      parsed = new URL(value);
    } catch (err) {
      return '';
    }
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.port) return '';
    const host = parsed.hostname.toLowerCase();
    const known = DATASHEET_DOMAINS.some((domain) => host === domain || host.endsWith('.' + domain));
    return known ? parsed.href : '';
  }

  // An anchor to a page in the browser. `href` must already have passed a check above. The
  // window itself never navigates: the link opens in a new browser tab. `labels` are the keys of
  // its aria-label and title, `params` their values.
  function externalLink(className, href, labels, params) {
    if (!href) return null;
    const link = el('a', className);
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    setAttrs(link, labels, params);
    return link;
  }

  // The LCSC id of a part: a link to its LCSC page, or plain text when there is no safe URL.
  function lcscIdNode(lcscId, suppliedUrl) {
    const id = String(lcscId);
    const link = externalLink(
      'lcsc-link',
      lcscProductUrl(id, suppliedUrl),
      { 'aria-label': 'link.lcscLabel', title: 'link.lcscTitle' },
      { id: id }
    );
    if (!link) return el('span', 'lcsc-id', id);
    link.textContent = id;
    return link;
  }

  function datasheetNode(lcscId, url) {
    const link = externalLink(
      'datasheet-link',
      datasheetUrl(url),
      { 'aria-label': 'link.sheetLabel', title: 'link.sheetTitle' },
      { id: String(lcscId) }
    );
    if (link) setText(link, 'link.sheetText');
    return link;
  }

  function formatStock(value) {
    return new Intl.NumberFormat(I18n.getLanguage()).format(value);
  }

  // Status line and error box -------------------------------------------------------------------

  function renderStatus() {
    const status = state.status;
    $('status').className = 'status status-' + status.level;
    $('status-badge').textContent = t(LEVEL_KEYS[status.level]);
    $('status-text').textContent = describe(status.text);
  }

  // `text` is a descriptor: textKey(...) for a text of the page, textRaw(...) for Python's.
  function setStatus(level, text) {
    const known = Object.prototype.hasOwnProperty.call(LEVEL_KEYS, level) ? level : 'info';
    state.status = { level: known, text: text };
    renderStatus();
  }

  function renderError() {
    $('error-text').textContent = state.error ? describe(state.error) : '';
  }

  function showError(text) {
    state.error = text;
    renderError();
    $('error-box').hidden = false;
    setStatus('error', textKey('status.requestFailed'));
    state.pendingId = null;
    state.pendingFusion = false;
    endSearch();
    // A refused hand-off request gets no fusion_done: put its card back (a hand-off that is really
    // running sends fusion_done later and corrects the card again).
    state.fusion.forEach((entry, id) => {
      if (entry.phase === 'sending') state.fusion.set(id, entry.before || { phase: 'idle', result: null });
    });
    refreshAllFusion(true);
    updateAddButtons();
  }

  function clearError() {
    state.error = null;
    $('error-box').hidden = true;
    renderError();
  }

  function setBusy(value) {
    state.busy = value;
    $('app').setAttribute('aria-busy', String(value));
    $('busy').hidden = !value;
    $('search-button').disabled = value;
    if (!value) {
      state.pendingId = null;
      state.pendingFusion = false;
      endSearch();
      settleSendingFusion();
    }
    refreshAllFusion(true);
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
          // Like every message of a result it is English on the wire and translated for display.
          message: t('fusion.noReport', null, I18n.FALLBACK),
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
      button.textContent = added ? t('button.added') : pending ? t('button.adding') : t('button.add');
    });
  }

  // Results area: skeleton rows while a search runs, then the rows, the empty state or (after a
  // failure) whatever was shown before.

  function buildSkeleton() {
    const host = $('results-skeleton');
    host.replaceChildren();
    for (let index = 0; index < SKELETON_ROWS; index += 1) {
      const row = el('div', 'skeleton-row');
      ['id', 'mpn', 'type', 'pkg', 'mfr', 'stock', 'desc', 'desc-short', 'action'].forEach((part) => {
        row.appendChild(el('span', 'sk sk-' + part));
      });
      host.appendChild(row);
    }
  }

  function showResultsView() {
    const shown = state.searching || state.hasResults;
    $('idle-hint').hidden = shown;
    $('results-section').hidden = !shown;
    $('results-skeleton').hidden = !state.searching;
    $('results-table').hidden = state.searching;
    $('results-empty').hidden = state.searching || !state.hasResults || $('results-body').children.length > 0;
    let heading = t('results.heading');
    let count = '';
    if (state.searching) {
      heading = t('results.searching', { query: state.pendingQuery });
    } else if (state.hasResults) {
      heading = t('results.headingFor', { query: state.lastQuery });
      if (state.items.length > 0) count = t('results.count', { n: state.items.length });
    }
    $('results-heading').textContent = heading;
    $('results-count').textContent = count;
  }

  function beginSearch(query) {
    state.searching = true;
    state.pendingQuery = query;
    showResultsView();
  }

  function endSearch() {
    if (!state.searching) return;
    state.searching = false;
    showResultsView();
  }

  function drawResultRows() {
    const body = $('results-body');
    body.replaceChildren();
    state.items.forEach((item) => body.appendChild(buildResultRow(item)));
  }

  function renderResults(data) {
    state.items = Array.isArray(data.items) ? data.items : [];
    state.parts = new Map();
    state.items.forEach((item) => state.parts.set(String(item.lcsc_id), item));
    drawResultRows();
    state.searching = false;
    state.hasResults = true;
    state.lastQuery = data.query || '';
    showResultsView();
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

    // The cells follow the column order of the (screen reader only) header in index.html.
    cell('col-id').appendChild(lcscIdNode(item.lcsc_id, item.lcsc_url));
    cell('col-mpn', item.mpn || '-');

    const typeCell = cell('col-type');
    if (item.library_type) {
      const kind = String(item.library_type).toLowerCase() === 'basic' ? 'basic' : 'extended';
      typeCell.appendChild(el('span', 'badge badge-' + kind, item.library_type));
    }

    cell('col-manufacturer', item.manufacturer || '-');
    cell('col-package', item.package || '-');

    const stockCell = cell('col-stock');
    if (typeof item.stock === 'number' && Number.isFinite(item.stock)) {
      if (item.stock > 0) {
        stockCell.appendChild(el('span', 'stock-num', formatStock(item.stock)));
        stockCell.appendChild(document.createTextNode(' ' + t('stock.in')));
      } else {
        stockCell.appendChild(el('span', 'stock-out', t('stock.out')));
      }
    }

    const desc = cell('col-desc', item.description || '');
    desc.title = item.description || '';

    const sheet = datasheetNode(item.lcsc_id, item.datasheet_url);
    const linksCell = cell('col-links');
    if (sheet) linksCell.appendChild(sheet);

    const actionCell = cell('col-action');
    const button = el('button', 'primary', t('button.add'));
    button.type = 'button';
    button.dataset.add = item.lcsc_id;
    const part = item.mpn ? item.lcsc_id + ' ' + item.mpn : String(item.lcsc_id);
    button.setAttribute('aria-label', t('button.addLabel', { part: part }));
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
    title.appendChild(lcscIdNode(data.lcsc_id));
    if (data.mpn) title.appendChild(el('span', 'mpn', data.mpn));
    const known = state.parts.get(String(data.lcsc_id));
    const sheet = known ? datasheetNode(data.lcsc_id, known.datasheet_url) : null;
    if (sheet) title.appendChild(sheet);
    const badges = el('span', 'added-badges');
    badges.appendChild(
      data.opened
        ? textEl('span', 'badge badge-basic', 'card.sentToFusion')
        : textEl('span', 'badge', 'card.saved')
    );
    if (hasModel) badges.appendChild(textEl('span', 'badge badge-info', 'card.badge3d'));
    title.appendChild(badges);
    item.appendChild(title);
    item.appendChild(el('p', 'added-path', data.path));

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    if (warnings.length > 0) {
      const warningList = el('ul', 'warnings');
      warnings.forEach((warning) => warningList.appendChild(messageEl('li', null, warning)));
      item.appendChild(warningList);
    }
    parts.fusionBlock = buildFusionBlock();
    item.appendChild(parts.fusionBlock);
    if (data.with_3d && !hasModel) {
      const missing = el('p', 'model-missing');
      missing.appendChild(textEl('strong', null, 'card.noModelTitle'));
      missing.appendChild(document.createTextNode(' '));
      missing.appendChild(
        textEl('span', null, warnings.length > 0 ? 'card.noModelTextReason' : 'card.noModelText')
      );
      item.appendChild(missing);
    }

    const details = el('details');
    details.open = !data.opened && !fusionRequested;
    details.appendChild(textEl('summary', null, 'card.howTo'));
    details.appendChild(messageEl('p', null, data.instructions));
    item.appendChild(details);
    parts.instructions = details;

    const actions = el('div', 'added-actions');
    // The caption of this button follows the state of the hand-off (applyFusion writes it).
    const fusionButton = el('button', 'primary');
    fusionButton.type = 'button';
    fusionButton.dataset.fusion = data.lcsc_id;
    setAttrs(fusionButton, { 'aria-label': 'button.addToFusionLabel' }, { id: String(data.lcsc_id) });
    actions.appendChild(fusionButton);
    parts.fusionButton = fusionButton;
    const libraryButton = textEl('button', 'small', 'button.openSeparate');
    libraryButton.type = 'button';
    libraryButton.dataset.library = data.path;
    setAttrs(libraryButton, { title: 'button.openSeparateTitle' });
    actions.appendChild(libraryButton);
    const folderButton = textEl('button', 'small', 'card.openFolder');
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
    applyFusion(item, true);
    updateAddButtons();
  }

  // Fixed text lives in the <template> of index.html (translated by data-i18n), only the file
  // names are inserted here.
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
    I18n.apply(help);
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
    // The flow words a failed 3D step as a warning that starts with "3D: "; show it here once.
    const own = warnings.findIndex((text) => text.indexOf(THREE_D_PREFIX) === 0);
    const reason =
      own >= 0
        ? I18n.translateMessage(warnings[own]).slice(THREE_D_PREFIX.length)
        : t('fusion.seeWarnings');
    const rest = own >= 0 ? warnings.filter((text, index) => index !== own) : warnings;
    if (result.three_d === 'started') {
      return { kind: 'started', rest: rest, text: t('fusion.threeDStarted') };
    }
    if (result.three_d === 'failed') {
      return { kind: 'problem', rest: rest, text: t('fusion.threeDFailed', { reason: reason }) };
    }
    if (result.three_d === 'skipped') {
      return { kind: 'problem', rest: rest, text: t('fusion.threeDSkipped', { reason: reason }) };
    }
    return { kind: 'none', rest: warnings, text: '' };
  }

  // What a card says about its part and the Fusion library, from the entry in state.fusion.
  // `warnings` stay raw (English): applyFusion shows them as messages.
  function describeFusion(entry) {
    const view = { kind: 'idle', head: t('fusion.headIdle'), detail: '', threeD: null, warnings: [] };
    if (entry.phase === 'sending') {
      view.kind = 'sending';
      view.head = t('fusion.headSending');
      view.detail = t('fusion.detailSending');
      return view;
    }
    const result = entry.result;
    if (entry.phase !== 'done' || !result) return view;
    const warnings = Array.isArray(result.warnings) ? result.warnings.map(String) : [];
    if (!result.ok) {
      view.kind = 'fail';
      view.detail = I18n.translateMessage(result.message || '');
      view.head = view.detail ? t('fusion.headFailDetail') : t('fusion.headFail');
      view.warnings = warnings;
      return view;
    }
    view.head = result.library_name
      ? t('fusion.headIn', { name: result.library_name })
      : t('fusion.headInDefault');
    if (result.already_present) view.detail = t('fusion.detailPresent');
    else if (result.created_library) view.detail = t('fusion.detailCreated');
    else if (result.merged) view.detail = t('fusion.detailMerged');
    else view.detail = t('fusion.detailSaved');
    const threeD = threeDLine(result, warnings);
    view.threeD = threeD.kind === 'none' ? null : threeD;
    view.warnings = threeD.rest;
    view.kind = threeD.kind === 'problem' || threeD.rest.length > 0 ? 'warn' : 'ok';
    return view;
  }

  // Writes the state of one card into its DOM (text only). A card that went wrong opens its help
  // sections (`openHelp`); a language switch passes false, because it must not undo what the user
  // closed since.
  function applyFusion(card, openHelp) {
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
    view.warnings.forEach((text) => list.appendChild(messageEl('li', null, text)));
    list.hidden = view.warnings.length === 0;

    const button = parts.fusionButton;
    const inLibrary = view.kind === 'ok' || view.kind === 'warn';
    button.hidden = inLibrary;
    button.disabled = state.busy || view.kind === 'sending';
    button.textContent = view.kind === 'sending' ? t('button.sending') : t('button.addToFusion');

    // The manual 3D steps are the fallback: not while Fusion is working on it, and not when it
    // already opened the 3D package. They open by themselves when the hand-off went wrong.
    if (parts.modelHelp) {
      const started = Boolean(view.threeD && view.threeD.kind === 'started');
      parts.modelHelp.hidden = view.kind === 'sending' || started;
      if (openHelp && (view.kind === 'fail' || (view.threeD && view.threeD.kind === 'problem'))) {
        parts.modelHelp.open = true;
      }
    }
    if (openHelp && parts.instructions && view.kind === 'fail') parts.instructions.open = true;
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
    if (card) applyFusion(card, true);
  }

  function refreshAllFusion(openHelp) {
    Array.from($('added-list').children).forEach((card) => applyFusion(card, openHelp));
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
        setStatus(data.level, textRaw(data.text));
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
        showError(data.message ? textRaw(data.message) : textKey('error.unknown'));
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
      showError(textKey('error.notConnected'));
      return Promise.resolve(null);
    }
    try {
      return Promise.resolve(state.bridge.fusionSendData(action, JSON.stringify(payload || {}))).catch(
        (err) => {
          showError(textKey('error.cannotReach', { reason: String(err) }));
          return null;
        }
      );
    } catch (err) {
      showError(textKey('error.cannotReach', { reason: String(err) }));
      return Promise.resolve(null);
    }
  }

  function requestSearch(query) {
    const text = query.trim();
    if (!text) {
      setStatus('warn', textKey('status.typeFirst'));
      $('query').focus();
      return;
    }
    if (state.busy) {
      setStatus('warn', textKey('status.busy'));
      return;
    }
    clearError();
    beginSearch(text);
    send(TO_PYTHON.SEARCH, { query: text }).then((reply) => {
      if (reply === 'BUSY' || reply === 'ERROR') endSearch();
    });
  }

  function requestAdd(lcscId) {
    if (state.busy) {
      setStatus('warn', textKey('status.busy'));
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
      setStatus('warn', textKey('status.busy'));
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
        showError(textKey('error.noBridge'));
        return;
      }
      send(TO_PYTHON.READY, {});
    });
  }

  // Read only hook for the tests: the two checks that decide whether a link is shown at all.
  Object.defineProperty(window, '__lcscPalette', {
    value: Object.freeze({ lcscProductUrl: lcscProductUrl, datasheetUrl: datasheetUrl }),
  });

  // Settings: theme and language ----------------------------------------------------------------

  // Writes every text of the page again, in the language that is now set. State is kept: only
  // text is rewritten, open <details>, disabled buttons and the focus stay as they are.
  function retranslate() {
    I18n.apply(document);
    renderStatus();
    renderError();
    if (state.hasResults) drawResultRows();
    showResultsView();
    refreshAllFusion(false);
    updateAddButtons();
  }

  // One radio per language of window.LcscPrefs.LANGUAGES, labelled with its own name.
  function buildLanguageOptions() {
    const host = $('language-options');
    host.replaceChildren();
    Prefs.LANGUAGES.forEach((language) => {
      const input = el('input');
      input.type = 'radio';
      input.name = 'language';
      input.id = 'language-' + language.code;
      input.value = language.code;
      const name = el('span', null, language.name);
      name.lang = language.code;
      const label = el('label', 'check');
      label.htmlFor = input.id;
      label.appendChild(input);
      label.appendChild(name);
      host.appendChild(label);
    });
  }

  function checkRadio(group, value) {
    document.querySelectorAll('#settings-panel input[name="' + group + '"]').forEach((input) => {
      input.checked = input.value === value;
    });
  }

  function syncSettings() {
    checkRadio('theme', Prefs.getTheme());
    checkRadio('language', I18n.getLanguage());
  }

  function onSettingChange(event) {
    const input = event.target;
    if (!input || input.type !== 'radio') return;
    if (input.name === 'theme') {
      Prefs.setTheme(input.value);
    } else if (input.name === 'language' && I18n.setLanguage(input.value)) {
      Prefs.setLanguage(input.value);
      retranslate();
    }
  }

  // Opens or closes the panel. Opening moves the focus to the chosen theme, closing with the
  // keyboard (Escape) returns it to the button.
  function setSettingsOpen(open, restoreFocus) {
    const panel = $('settings-panel');
    const toggle = $('settings-toggle');
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) {
      const target = panel.querySelector('input[name="theme"]:checked') || panel.querySelector('input');
      if (target) target.focus();
    } else if (restoreFocus) {
      toggle.focus();
    }
  }

  function onKeydown(event) {
    if (event.key !== 'Escape' || $('settings-panel').hidden) return;
    const active = document.activeElement;
    if (active === $('settings-toggle') || $('settings-panel').contains(active)) {
      event.preventDefault();
      setSettingsOpen(false, true);
    }
  }

  // Wiring ------------------------------------------------------------------------------------

  function init() {
    buildLanguageOptions();
    syncSettings();
    $('settings-toggle').addEventListener('click', () => setSettingsOpen($('settings-panel').hidden, false));
    $('settings-panel').addEventListener('change', onSettingChange);
    document.addEventListener('keydown', onKeydown);
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
    buildSkeleton();
    retranslate();
    $('query').focus();
    connect();
  }

  // The texts first, so that the page never paints in the wrong language (the script sits at the
  // end of <body>, everything above it is parsed). Skipped where there is no page, e.g. under Node.
  if (document.body && I18n) {
    I18n.apply(document);
    renderStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
