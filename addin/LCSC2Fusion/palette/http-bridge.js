/* LCSC2Fusion HTTP bridge: lets the unchanged palette page (app.js) run in a normal browser
 * window that the add-in serves from a local HTTP server.
 *
 * Loaded before app.js. It installs itself ONLY when the page is served over http: and no `adsk`
 * object exists. Inside the Fusion palette (adsk injected) and for the file:// mock mode it does
 * nothing at all.
 *
 *   page to Python : window.adsk.fusionSendData(action, data)  ->  POST /api/act, resolves with
 *                    the reply string ('OK', 'BUSY', 'ERROR'). It never rejects and never hangs.
 *   Python to page : one EventSource on /api/events. Every message is {action, data} and goes to
 *                    window.fusionJavaScriptHandler.handle(action, data). HTTP has no
 *                    acknowledgement, so the handler's return value is ignored.
 *   action 'shutdown' means Fusion stops the add-in: the window closes itself.
 *   action 'overflow' is a marker of the server (the stream was too slow, it ends and the browser
 *                    reconnects with Last-Event-ID): ignored, the page does not know it.
 *
 * A connection without Last-Event-ID starts at the head of the stream: what Python published
 * before it subscribed is gone. Two rules follow from that:
 *   - the first request waits (at most STREAM_WAIT_MS) until the stream is open, so the answer of
 *     Python to `ready` is not missed while the page starts;
 *   - when the stream had failed and is back, the page sends `ready` again, which makes Python
 *     send its state (busy flag, status line) once more.
 *
 * Connection loss: the browser reconnects the stream by itself. Without a successful open for
 * LOST_AFTER_MS a banner is shown and a fresh EventSource is created every RETRY_MS (browsers stop
 * retrying after some failures), together with a GET /api/ping probe. After GIVE_UP_MS without any
 * success the window tries to close itself and keeps the banner.
 *
 * Texts come from i18n.js (loaded before this file): the banner shows a dictionary key and keeps
 * it in data-i18n, so a language switch rewrites it. The `error` message for a failed request is
 * sent to the page in English, like every message of Python, and the page translates it.
 *
 * CSP of the server is default-src 'self': no inline code, no eval, no external hosts.
 */
(function () {
  'use strict';

  if (window.location.protocol !== 'http:' || window.adsk) return;

  const ACT_URL = '/api/act';
  const EVENTS_URL = '/api/events';
  const PING_URL = '/api/ping';
  const BANNER_ID = 'conn-banner';

  const ACT_TIMEOUT_MS = 35000;
  const PING_TIMEOUT_MS = 5000;
  const STREAM_WAIT_MS = 3000;
  const HANDLER_POLL_MS = 50;
  const HANDLER_WAIT_MS = 10000;
  const QUEUE_CAP = 200;
  const LOST_AFTER_MS = 15000;
  const RETRY_MS = 3000;
  const GIVE_UP_MS = 90000;

  // EventSource.readyState values, spelled out so that the logic does not depend on the constants.
  const SOURCE_OPEN = 1;
  const SOURCE_CLOSED = 2;

  const ACTION_READY = 'ready'; // an action of app.js (TO_PYTHON.READY)
  const ACTION_ERROR = 'error'; // a message of app.js (FROM_PYTHON.ERROR), shows a failed request
  const ACTION_SHUTDOWN = 'shutdown';
  const ACTION_OVERFLOW = 'overflow';
  const REPLY_ERROR = 'ERROR';

  const KEY_LOST = 'bridge.lost';
  const KEY_STOPPED = 'bridge.stopped';
  const KEY_REQUEST_FAILED = 'bridge.requestFailed';
  const WIRE_LANGUAGE = 'en'; // the language of the messages that travel to the page

  // A text of i18n.js; without that script (never the case in the page) the key itself shows.
  function t(key, language) {
    const i18n = window.LcscI18n;
    return i18n ? i18n.t(key, null, language) : key;
  }

  // 'connecting' until the first successful open, then 'open' or 'broken', finally 'stopped'.
  let state = 'connecting';
  let source = null;
  let gateExpired = false;
  let streamFailed = false; // the stream failed since it was last open: events may have been missed
  let lostBannerShown = false;
  let gaveUp = false;
  let probeController = null;
  let pollsLeft = 0;
  const queue = [];
  const waiters = [];
  const timers = { lost: null, giveUp: null, retry: null, poll: null, ping: null, gate: null };

  // Banner -----------------------------------------------------------------------------------

  // `key` is a dictionary key; it stays on the node as data-i18n, so a language switch redraws it.
  function showBanner(key) {
    const node = document.getElementById(BANNER_ID);
    if (!node) return;
    node.textContent = t(key);
    if (typeof node.setAttribute === 'function') node.setAttribute('data-i18n', key);
    node.hidden = false;
  }

  function hideBanner() {
    const node = document.getElementById(BANNER_ID);
    if (!node) return;
    node.textContent = '';
    if (typeof node.removeAttribute === 'function') node.removeAttribute('data-i18n');
    node.hidden = true;
  }

  function closeWindow() {
    try {
      window.close();
    } catch (err) {
      // Best effort: a window that was not opened by a script may refuse.
    }
  }

  // Timers -----------------------------------------------------------------------------------

  function cancel(name) {
    if (timers[name] !== null) {
      clearTimeout(timers[name]);
      timers[name] = null;
    }
  }

  function cancelAll() {
    Object.keys(timers).forEach(cancel);
  }

  // Delivery of Python messages to app.js ------------------------------------------------------

  function currentHandler() {
    const handler = window.fusionJavaScriptHandler;
    return handler && typeof handler.handle === 'function' ? handler : null;
  }

  // Returns false when app.js has not defined its handler yet; the messages then stay queued.
  function flush() {
    const handler = currentHandler();
    if (!handler) return false;
    while (queue.length > 0) {
      const message = queue.shift();
      try {
        handler.handle(message.action, message.data);
      } catch (err) {
        console.error('LCSC2Fusion: the page failed to handle "' + message.action + '"', err);
      }
    }
    return true;
  }

  function pollTick() {
    timers.poll = null;
    if (flush()) return;
    pollsLeft -= 1;
    if (pollsLeft > 0) timers.poll = setTimeout(pollTick, HANDLER_POLL_MS);
  }

  function ensurePoll() {
    if (timers.poll !== null) return;
    pollsLeft = HANDLER_WAIT_MS / HANDLER_POLL_MS;
    timers.poll = setTimeout(pollTick, HANDLER_POLL_MS);
  }

  function enqueue(action, data) {
    queue.push({ action: action, data: data });
    if (queue.length > QUEUE_CAP) queue.splice(0, queue.length - QUEUE_CAP);
    if (!flush()) ensurePoll();
  }

  // The first request waits for the stream -------------------------------------------------------

  function releaseWaiters() {
    cancel('gate');
    waiters.splice(0, waiters.length).forEach((resolve) => resolve());
  }

  function onGateTimeout() {
    timers.gate = null;
    gateExpired = true;
    streamFailed = true; // the request goes out without a stream: its answer may be missed
    releaseWaiters();
  }

  function waitForStream() {
    if (state !== 'connecting' || gateExpired) return Promise.resolve();
    return new Promise((resolve) => {
      waiters.push(resolve);
      if (timers.gate === null) timers.gate = setTimeout(onGateTimeout, STREAM_WAIT_MS);
    });
  }

  // Connection state ----------------------------------------------------------------------------

  function connectionOk() {
    if (state === 'stopped') return;
    const resync = streamFailed;
    streamFailed = false;
    state = 'open';
    gaveUp = false;
    cancel('lost');
    cancel('giveUp');
    cancel('retry');
    releaseWaiters();
    if (lostBannerShown) {
      lostBannerShown = false;
      hideBanner();
    }
    if (resync) fusionSendData(ACTION_READY, '{}'); // never rejects, the reply is not needed
  }

  function onLostTimeout() {
    timers.lost = null;
    if (state === 'open' || state === 'stopped') return;
    lostBannerShown = true;
    showBanner(KEY_LOST);
  }

  function onGiveUp() {
    timers.giveUp = null;
    if (state === 'open' || state === 'stopped') return;
    gaveUp = true;
    lostBannerShown = true;
    showBanner(KEY_LOST);
    closeWindow();
  }

  function probe() {
    if (probeController !== null || state === 'stopped') return;
    const controller = new AbortController();
    probeController = controller;
    timers.ping = setTimeout(() => controller.abort(), PING_TIMEOUT_MS);
    fetch(PING_URL, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => {
        // The server answers, so a failed POST was a hiccup. Only a working stream counts as
        // recovered: without events the page would look alive but stay silent.
        if (body && body.ok === true && source && source.readyState === SOURCE_OPEN) {
          connectionOk();
        }
      })
      .catch(() => {})
      .then(() => {
        cancel('ping');
        probeController = null;
      });
  }

  function onRetryTick() {
    timers.retry = null;
    if (state === 'stopped' || state === 'open') return;
    const readyState = source ? source.readyState : SOURCE_CLOSED;
    // While the browser is still retrying on its own (and keeps Last-Event-ID) leave it alone,
    // until the banner shows that this does not work. A working stream is never replaced.
    if (readyState === SOURCE_CLOSED || (lostBannerShown && readyState !== SOURCE_OPEN)) {
      openStream();
    }
    probe();
    scheduleRetry();
  }

  function scheduleRetry() {
    if (timers.retry === null && state !== 'stopped') {
      timers.retry = setTimeout(onRetryTick, RETRY_MS);
    }
  }

  // Starts the clocks of the banner, of the give up and of the retries. Idempotent.
  function armFailureTimers() {
    if (state === 'stopped') return;
    if (!lostBannerShown && timers.lost === null) {
      timers.lost = setTimeout(onLostTimeout, LOST_AFTER_MS);
    }
    if (!gaveUp && timers.giveUp === null) {
      timers.giveUp = setTimeout(onGiveUp, GIVE_UP_MS);
    }
    scheduleRetry();
  }

  function connectionLost() {
    if (state === 'stopped') return;
    state = 'broken';
    releaseWaiters(); // no stream to wait for any more
    armFailureTimers();
  }

  function streamLost() {
    streamFailed = true;
    connectionLost();
  }

  // Event stream ----------------------------------------------------------------------------

  function closeSource() {
    if (!source) return;
    const old = source;
    source = null;
    old.onopen = null;
    old.onmessage = null;
    old.onerror = null;
    try {
      old.close();
    } catch (err) {
      // Nothing left to clean up.
    }
  }

  function openStream() {
    closeSource();
    let created;
    try {
      created = new EventSource(EVENTS_URL);
    } catch (err) {
      console.error('LCSC2Fusion: cannot open the event stream', err);
      streamLost();
      return;
    }
    source = created;
    created.onopen = () => {
      if (source === created) connectionOk();
    };
    created.onmessage = (event) => {
      if (source === created) onStreamMessage(event);
    };
    created.onerror = () => {
      if (source === created) streamLost();
    };
  }

  function normalizeData(data) {
    if (typeof data === 'string') return data;
    return data === undefined || data === null ? '' : JSON.stringify(data);
  }

  function onStreamMessage(event) {
    if (state === 'stopped') return;
    if (state !== 'open') connectionOk();
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      console.error('LCSC2Fusion: ignoring a malformed event', err);
      return;
    }
    if (!message || typeof message.action !== 'string' || message.action === '') {
      console.error('LCSC2Fusion: ignoring an event without an action');
      return;
    }
    if (message.action === ACTION_OVERFLOW) return;
    if (message.action === ACTION_SHUTDOWN) {
      shutDown();
      return;
    }
    enqueue(message.action, normalizeData(message.data));
  }

  // Stopping --------------------------------------------------------------------------------

  function stop() {
    state = 'stopped';
    cancelAll();
    releaseWaiters();
    if (probeController !== null) probeController.abort();
    closeSource();
  }

  function shutDown() {
    stop();
    lostBannerShown = false;
    showBanner(KEY_STOPPED);
    closeWindow();
  }

  // Page to Python ----------------------------------------------------------------------------

  // `broken` marks the connection as broken (server or session gone); otherwise the request was
  // refused, which says nothing about the connection. app.js ignores the reply of
  // fusionSendData, so the user gets the failure as an `error` message of the page.
  function requestFailed(reason, broken) {
    console.warn('LCSC2Fusion: request to Fusion failed (' + reason + ')');
    if (state === 'stopped') return REPLY_ERROR;
    if (broken) connectionLost();
    enqueue(ACTION_ERROR, JSON.stringify({ message: t(KEY_REQUEST_FAILED, WIRE_LANGUAGE) }));
    return REPLY_ERROR;
  }

  async function fusionSendData(action, data) {
    await waitForStream();
    if (state === 'stopped') return REPLY_ERROR;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ACT_TIMEOUT_MS);
    try {
      const response = await fetch(ACT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-LCSC': '1' },
        body: JSON.stringify({ action: String(action), data: normalizeData(data) }),
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      let body = null;
      try {
        body = await response.json();
      } catch (err) {
        body = null;
      }
      const reply = body && typeof body.reply === 'string' ? body.reply : null;
      if (response.ok && reply !== null) return reply;
      const refused = !response.ok && response.status !== 403 && response.status < 500;
      return requestFailed('HTTP ' + response.status, !refused);
    } catch (err) {
      return requestFailed(err && err.name ? err.name : 'network error', true);
    } finally {
      clearTimeout(timer);
    }
  }

  // Install ------------------------------------------------------------------------------------

  window.adsk = { fusionSendData: fusionSendData };
  Object.defineProperty(window, '__lcscHttpBridge', {
    value: Object.freeze({ state: () => state }),
  });
  window.addEventListener('pagehide', stop);
  window.addEventListener('beforeunload', stop);
  openStream();
  armFailureTimers();
})();
