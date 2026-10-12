'use strict';
/* Test harness for addin/LCSC2Fusion/palette/http-bridge.js (run by tests/test_addin_http_bridge.py).
 *
 * The bridge is evaluated in a vm context whose globals are hand written fakes: window/location,
 * a one element document, fetch, EventSource, AbortController and a fake clock, so that the 15 s,
 * 35 s and 90 s paths run instantly and deterministically.
 *
 * Usage:
 *   node http_bridge_harness.js <path to http-bridge.js> --list   names of the scenarios (JSON)
 *   node http_bridge_harness.js <path to http-bridge.js> --all    run all, JSON {name: "ok"|error}
 */

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const TEXT_LOST = 'Lost the connection to Fusion. If Fusion is closed, close this window.';
const TEXT_STOPPED = 'LCSC2Fusion was stopped in Fusion. You can close this window.';

const settle = () => new Promise((resolve) => setImmediate(resolve));

class FakeClock {
  constructor() {
    this.now = 0;
    this.seq = 0;
    this.timers = new Map();
  }

  setTimeout(fn, ms) {
    this.seq += 1;
    this.timers.set(this.seq, { id: this.seq, at: this.now + Math.max(0, ms || 0), fn });
    return this.seq;
  }

  clearTimeout(id) {
    this.timers.delete(id);
  }

  pending() {
    return this.timers.size;
  }

  // Runs every timer that falls due within `ms`, in time order, letting promises settle between.
  async advance(ms) {
    const target = this.now + ms;
    for (;;) {
      await settle();
      let next = null;
      for (const timer of this.timers.values()) {
        if (timer.at > target) continue;
        if (next === null || timer.at < next.at || (timer.at === next.at && timer.id < next.id)) {
          next = timer;
        }
      }
      if (next === null) break;
      this.timers.delete(next.id);
      this.now = next.at;
      next.fn();
    }
    this.now = target;
    await settle();
  }
}

function jsonResponse(status, body) {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  };
}

function createEnv(options = {}) {
  const protocol = options.protocol || 'http:';
  const clock = new FakeClock();
  const sources = [];
  const calls = [];
  const delivered = [];
  const logs = { error: [], warn: [] };
  const listeners = {};
  const banner = options.banner === false ? null : { hidden: true, textContent: '' };
  const env = {
    clock,
    sources,
    calls,
    delivered,
    logs,
    banner,
    closeCalls: 0,
    closeThrows: false,
    fetchHandler: (url) => (url === '/api/ping' ? jsonResponse(200, { ok: true, subscribers: 1 }) : jsonResponse(200, { reply: 'OK' })),
  };

  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.closed = false;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      sources.push(this);
    }

    close() {
      this.readyState = 2;
      this.closed = true;
    }

    open() {
      this.readyState = 1;
      if (this.onopen) this.onopen({ type: 'open' });
    }

    // `id` becomes event.lastEventId, as for a real `id: N` line.
    message(payload, id) {
      const data = typeof payload === 'string' ? payload : JSON.stringify(payload);
      if (this.onmessage) {
        this.onmessage({ data, lastEventId: id === undefined ? '' : String(id) });
      }
    }

    // gaveUp: the browser stops retrying (readyState CLOSED); otherwise it keeps CONNECTING.
    fail(gaveUp) {
      this.readyState = gaveUp ? 2 : 0;
      if (this.onerror) this.onerror({ type: 'error' });
    }
  }

  const fakeFetch = (url, init) =>
    new Promise((resolve, reject) => {
      calls.push({ url, init: init || {} });
      const signal = init && init.signal;
      if (signal) {
        const abort = () => {
          const error = new Error('The operation was aborted.');
          error.name = 'AbortError';
          reject(error);
        };
        if (signal.aborted) abort();
        else signal.addEventListener('abort', abort);
      }
      Promise.resolve()
        .then(() => env.fetchHandler(url, init || {}))
        .then(resolve, reject);
    });

  const sandbox = {
    location: { protocol },
    document: { getElementById: (id) => (id === 'conn-banner' ? banner : null) },
    console: {
      error: (...args) => logs.error.push(args),
      warn: (...args) => logs.warn.push(args),
      log: () => {},
    },
    setTimeout: (fn, ms) => clock.setTimeout(fn, ms),
    clearTimeout: (id) => clock.clearTimeout(id),
    fetch: fakeFetch,
    EventSource: FakeEventSource,
    AbortController,
    addEventListener: (type, fn) => {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    close: () => {
      env.closeCalls += 1;
      if (env.closeThrows) throw new Error('scripts may not close this window');
    },
  };
  sandbox.window = sandbox;
  Object.assign(sandbox, options.preset || {});
  vm.createContext(sandbox);

  env.window = sandbox;
  env.listeners = listeners;
  env.load = () => {
    vm.runInContext(fs.readFileSync(env.bridgePath, 'utf8'), sandbox, { filename: 'http-bridge.js' });
    return env;
  };
  env.state = () => (sandbox.__lcscHttpBridge ? sandbox.__lcscHttpBridge.state() : undefined);
  env.send = (action, data) => sandbox.adsk.fusionSendData(action, data);
  env.fire = (type) => (listeners[type] || []).forEach((fn) => fn({ type }));
  // A stand-in for the handler that app.js defines. `impl` may throw to test isolation.
  env.installHandler = (impl) => {
    sandbox.fusionJavaScriptHandler = {
      handle: (action, data) => {
        delivered.push([action, data]);
        return impl ? impl(action, data) : 'OK';
      },
    };
  };
  env.last = () => sources[sources.length - 1];
  env.pingCalls = () => calls.filter((call) => call.url === '/api/ping');
  env.actCalls = () => calls.filter((call) => call.url === '/api/act');
  return env;
}

function boot(options) {
  const env = createEnv(options);
  env.bridgePath = process.argv[2];
  return env.load();
}

// Boots with an open stream: the common starting point of the request tests.
function bootOpen(options) {
  const env = boot(options);
  env.sources[0].open();
  return env;
}

const status = (text) => ({ action: 'status', data: JSON.stringify({ level: 'info', text }) });
const ERROR_MESSAGE = 'The request to Fusion failed. Check that Fusion is running, then try again.';

function assertBannerHidden(env) {
  assert.strictEqual(env.banner.hidden, true);
  assert.strictEqual(env.banner.textContent, '');
}

const scenarios = {};

// Installation -----------------------------------------------------------------------------------

function assertNothingInstalled(env, adsk) {
  assert.strictEqual(env.window.adsk, adsk);
  assert.strictEqual(env.window.__lcscHttpBridge, undefined);
  assert.strictEqual(env.sources.length, 0);
  assert.strictEqual(env.calls.length, 0);
  assert.strictEqual(env.clock.pending(), 0);
  assert.deepStrictEqual(Object.keys(env.listeners), []);
}

scenarios.not_installed_on_file_protocol = async () => {
  assertNothingInstalled(boot({ protocol: 'file:' }), undefined);
};

scenarios.not_installed_on_https = async () => {
  assertNothingInstalled(boot({ protocol: 'https:' }), undefined);
};

scenarios.not_installed_when_adsk_exists = async () => {
  const adsk = { fusionSendData: () => Promise.resolve('real') };
  assertNothingInstalled(boot({ preset: { adsk } }), adsk);
};

scenarios.installs_on_http = async () => {
  const env = boot();
  assert.strictEqual(typeof env.window.adsk.fusionSendData, 'function');
  assert.strictEqual(env.sources.length, 1);
  assert.strictEqual(env.sources[0].url, '/api/events');
  assert.strictEqual(env.state(), 'connecting');
  env.sources[0].open();
  assert.strictEqual(env.state(), 'open');
  assert.strictEqual(env.clock.pending(), 0);
  assertBannerHidden(env);
};

scenarios.test_hook_is_read_only = async () => {
  const env = boot();
  const hook = env.window.__lcscHttpBridge;
  assert.deepStrictEqual(Object.keys(hook), ['state']);
  assert.strictEqual(Reflect.set(env.window, '__lcscHttpBridge', null), false);
  assert.strictEqual(env.window.__lcscHttpBridge, hook);
  assert.strictEqual(Reflect.set(hook, 'state', () => 'hacked'), false);
  assert.strictEqual(env.state(), 'connecting');
};

// Page to Python ---------------------------------------------------------------------------------

scenarios.post_request_shape = async () => {
  const env = bootOpen();
  const reply = await env.send('search', '{"query":"C2040"}');
  assert.strictEqual(reply, 'OK');
  const calls = env.actCalls();
  assert.strictEqual(calls.length, 1);
  const { init } = calls[0];
  assert.strictEqual(init.method, 'POST');
  assert.deepStrictEqual({ ...init.headers }, {
    'Content-Type': 'application/json',
    'X-LCSC': '1',
  });
  assert.deepStrictEqual(JSON.parse(init.body), { action: 'search', data: '{"query":"C2040"}' });
  assert.strictEqual(init.credentials, 'same-origin');
  assert.strictEqual(init.cache, 'no-store');
  assert.ok(init.signal, 'an AbortSignal is passed for the timeout');
  assert.strictEqual(init.signal.aborted, false);
  assert.strictEqual(env.clock.pending(), 0, 'the timeout timer is cleared after the reply');
  assert.strictEqual(env.state(), 'open');
};

scenarios.post_without_data_sends_an_empty_string = async () => {
  const env = bootOpen();
  await env.send('ready');
  assert.deepStrictEqual(JSON.parse(env.actCalls()[0].init.body), { action: 'ready', data: '' });
};

for (const value of ['OK', 'BUSY', 'ERROR']) {
  scenarios['reply_passthrough_' + value.toLowerCase()] = async () => {
    const env = bootOpen();
    env.installHandler();
    env.fetchHandler = () => jsonResponse(200, { reply: value });
    assert.strictEqual(await env.send('add', '{}'), value);
    assert.strictEqual(env.state(), 'open');
    assert.deepStrictEqual(env.delivered, [], 'a reply of the server is not a local error');
  };
}

const failures = {
  network_failure: () => Promise.reject(new TypeError('Failed to fetch')),
  http_403: () => jsonResponse(403, { reply: 'ERROR' }),
  http_500: () => jsonResponse(500, { reply: 'ERROR' }),
  http_504: () => jsonResponse(504, { reply: 'ERROR' }),
  http_502_html: () => jsonResponse(502, '<html>bad gateway</html>'),
  non_json_body: () => jsonResponse(200, 'this is not json'),
  json_without_reply: () => jsonResponse(200, { ok: true }),
  reply_of_wrong_type: () => jsonResponse(200, { reply: 5 }),
};

for (const [name, handler] of Object.entries(failures)) {
  scenarios['post_failure_' + name] = async () => {
    const env = bootOpen();
    env.installHandler();
    env.fetchHandler = handler;
    assert.strictEqual(await env.send('search', '{}'), 'ERROR');
    assert.strictEqual(env.state(), 'broken');
    assert.strictEqual(env.delivered.length, 1);
    assert.strictEqual(env.delivered[0][0], 'error');
    assert.deepStrictEqual(JSON.parse(env.delivered[0][1]), { message: ERROR_MESSAGE });
    assert.strictEqual(env.clock.pending() > 0, true, 'a retry/probe is scheduled');
  };
}

scenarios.post_refused_with_400_is_an_error_but_not_a_broken_connection = async () => {
  const env = bootOpen();
  env.installHandler();
  env.fetchHandler = () => jsonResponse(400, { reply: 'ERROR' });
  assert.strictEqual(await env.send('search', '{}'), 'ERROR');
  assert.strictEqual(env.state(), 'open');
  assert.strictEqual(env.delivered.length, 1);
};

scenarios.post_timeout_after_35_seconds = async () => {
  const env = bootOpen();
  env.installHandler();
  env.fetchHandler = () => new Promise(() => {}); // never answers
  let result = null;
  env.send('add', '{}').then((value) => {
    result = value;
  });
  await env.clock.advance(34999);
  assert.strictEqual(result, null);
  assert.strictEqual(env.state(), 'open');
  await env.clock.advance(1);
  assert.strictEqual(result, 'ERROR');
  assert.strictEqual(env.actCalls()[0].init.signal.aborted, true);
  assert.strictEqual(env.state(), 'broken');
  assert.strictEqual(env.delivered.length, 1);
};

scenarios.post_failure_without_the_page_handler_is_queued = async () => {
  const env = bootOpen();
  env.fetchHandler = () => Promise.reject(new TypeError('Failed to fetch'));
  assert.strictEqual(await env.send('ready', ''), 'ERROR');
  env.installHandler();
  await env.clock.advance(50);
  assert.strictEqual(env.delivered.length, 1);
  assert.strictEqual(env.delivered[0][0], 'error');
};

scenarios.send_never_throws_on_odd_arguments = async () => {
  const env = bootOpen();
  assert.strictEqual(await env.send(undefined, undefined), 'OK');
  assert.deepStrictEqual(JSON.parse(env.actCalls()[0].init.body), { action: 'undefined', data: '' });
  assert.strictEqual(await env.send('x', { a: 1 }), 'OK');
  assert.deepStrictEqual(JSON.parse(env.actCalls()[1].init.body), { action: 'x', data: '{"a":1}' });
};

// Python to page -------------------------------------------------------------------------------

scenarios.events_wait_for_the_handler_and_keep_their_order = async () => {
  const env = bootOpen();
  const messages = [status('one'), { action: 'busy', data: '{"value":true}' }, status('three')];
  messages.forEach((message, index) => env.sources[0].message(message, index + 1));
  await env.clock.advance(120);
  assert.deepStrictEqual(env.delivered, []);
  env.installHandler();
  await env.clock.advance(50);
  assert.deepStrictEqual(env.delivered, messages.map((m) => [m.action, m.data]));
  env.sources[0].message(status('four'), 4);
  assert.strictEqual(env.delivered.length, 4, 'delivered at once when the handler exists');
  assert.strictEqual(env.delivered[3][1], status('four').data);
  assert.strictEqual(env.clock.pending(), 0);
};

scenarios.events_flush_on_arrival_once_the_handler_exists = async () => {
  const env = bootOpen();
  env.sources[0].message(status('one'), 1);
  env.installHandler();
  assert.strictEqual(env.delivered.length, 0);
  env.sources[0].message(status('two'), 2);
  assert.deepStrictEqual(
    env.delivered.map(([, data]) => JSON.parse(data).text),
    ['one', 'two']
  );
};

scenarios.queue_keeps_the_newest_200 = async () => {
  const env = bootOpen();
  for (let i = 1; i <= 250; i += 1) {
    env.sources[0].message({ action: 'status', data: String(i) }, i);
  }
  env.installHandler();
  await env.clock.advance(50);
  assert.strictEqual(env.delivered.length, 200);
  assert.strictEqual(env.delivered[0][1], '51');
  assert.strictEqual(env.delivered[199][1], '250');
};

scenarios.handler_polling_stops_after_10_seconds_but_the_queue_stays = async () => {
  const env = bootOpen();
  env.sources[0].message(status('early'), 1);
  assert.strictEqual(env.clock.pending(), 1);
  await env.clock.advance(10000);
  assert.strictEqual(env.clock.pending(), 0, 'the poll timer is gone');
  env.installHandler();
  await env.clock.advance(1000);
  assert.strictEqual(env.delivered.length, 0, 'nothing polls any more');
  env.sources[0].message(status('late'), 2);
  assert.deepStrictEqual(
    env.delivered.map(([, data]) => JSON.parse(data).text),
    ['early', 'late']
  );
};

scenarios.handler_exceptions_do_not_break_the_stream = async () => {
  const env = bootOpen();
  let calls = 0;
  env.installHandler(() => {
    calls += 1;
    if (calls === 1) throw new Error('boom');
    return 'OK';
  });
  env.sources[0].message(status('a'), 1);
  env.sources[0].message(status('b'), 2);
  env.sources[0].message(status('c'), 3);
  assert.strictEqual(env.delivered.length, 3);
  assert.strictEqual(env.logs.error.length, 1);
  assert.strictEqual(env.state(), 'open');
};

scenarios.handler_exception_does_not_lose_the_rest_of_the_queue = async () => {
  const env = bootOpen();
  env.sources[0].message(status('a'), 1);
  env.sources[0].message(status('b'), 2);
  let calls = 0;
  env.installHandler(() => {
    calls += 1;
    if (calls === 1) throw new Error('boom');
    return 'OK';
  });
  await env.clock.advance(50);
  assert.strictEqual(env.delivered.length, 2);
  assert.strictEqual(env.logs.error.length, 1);
};

scenarios.malformed_events_are_ignored = async () => {
  const env = bootOpen();
  env.installHandler();
  const source = env.sources[0];
  source.message('this is not json', 1);
  source.message('null', 2);
  source.message('{"data":"x"}', 3);
  source.message('{"action":5,"data":"x"}', 4);
  source.message('{"action":""}', 5);
  assert.deepStrictEqual(env.delivered, []);
  assert.strictEqual(env.logs.error.length, 5);
  assert.strictEqual(env.state(), 'open');
  source.message('{"action":"busy"}', 6);
  source.message({ action: 'results', data: { query: 'x' } }, 7);
  assert.deepStrictEqual(env.delivered, [
    ['busy', ''],
    ['results', '{"query":"x"}'],
  ]);
};

scenarios.events_with_the_same_last_event_id_are_all_delivered = async () => {
  // The browser keeps the previous id for an event without an `id:` line (the server's overflow
  // marker has none), so the page must not drop anything by id.
  const env = bootOpen();
  env.installHandler();
  [1, 1, 1, 2, 2].forEach((id, index) => {
    env.sources[0].message({ action: 'status', data: String(index) }, id);
  });
  assert.deepStrictEqual(env.delivered.map(([, data]) => data), ['0', '1', '2', '3', '4']);
};

scenarios.the_overflow_marker_is_not_passed_to_the_page = async () => {
  const env = bootOpen();
  env.installHandler();
  env.sources[0].message(status('before'), 7);
  env.sources[0].message({ action: 'overflow', data: '{}' }, 7); // no id line: id stays 7
  assert.deepStrictEqual(env.delivered.map(([action]) => action), ['status']);
  assert.strictEqual(env.state(), 'open');
};

// The stream starts at the head: first request waits, a stream that came back triggers `ready` ----

scenarios.the_first_request_waits_for_the_stream = async () => {
  const env = boot();
  let reply = null;
  env.send('ready', '{}').then((value) => {
    reply = value;
  });
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 0);
  assert.strictEqual(reply, null);
  env.sources[0].open();
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 1);
  assert.strictEqual(reply, 'OK');
  assert.strictEqual(env.clock.pending(), 0);
  // a stream that opened on time needs no `ready` of the bridge itself
  await env.clock.advance(100000);
  assert.strictEqual(env.actCalls().length, 1);
};

scenarios.the_first_requests_wait_together_and_keep_their_order = async () => {
  const env = boot();
  ['one', 'two', 'three'].forEach((name) => env.send(name, ''));
  await env.clock.advance(100);
  assert.strictEqual(env.actCalls().length, 0);
  env.sources[0].open();
  await env.clock.advance(0);
  assert.deepStrictEqual(
    env.actCalls().map((call) => JSON.parse(call.init.body).action),
    ['one', 'two', 'three']
  );
};

scenarios.the_first_request_goes_out_after_3_seconds_without_a_stream = async () => {
  const env = boot();
  env.send('ready', '{}');
  await env.clock.advance(2999);
  assert.strictEqual(env.actCalls().length, 0);
  await env.clock.advance(1);
  assert.strictEqual(env.actCalls().length, 1);
  env.send('search', '{}'); // the gate is over: no second wait
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 2);
  // the answer of the first request may have been missed: `ready` once the stream opens
  env.sources[0].open();
  await env.clock.advance(0);
  const actions = env.actCalls().map((call) => JSON.parse(call.init.body).action);
  assert.deepStrictEqual(actions, ['ready', 'search', 'ready']);
};

scenarios.the_first_request_goes_out_when_the_stream_fails = async () => {
  const env = boot();
  env.send('ready', '{}');
  await env.clock.advance(100);
  assert.strictEqual(env.actCalls().length, 0);
  env.sources[0].fail(false);
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 1);
};

scenarios.a_stream_that_failed_and_is_back_makes_the_page_say_ready_again = async () => {
  const env = bootOpen();
  await env.clock.advance(100000);
  assert.strictEqual(env.actCalls().length, 0, 'nothing is sent while the stream is fine');
  env.sources[0].fail(false);
  await env.clock.advance(1000);
  env.sources[0].open(); // the browser reconnected by itself
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 1);
  assert.deepStrictEqual(JSON.parse(env.actCalls()[0].init.body), { action: 'ready', data: '{}' });
  await env.clock.advance(100000);
  assert.strictEqual(env.actCalls().length, 1, 'once per recovery');
  env.sources[0].fail(false);
  env.sources[0].open();
  await env.clock.advance(0);
  assert.strictEqual(env.actCalls().length, 2);
};

scenarios.a_fresh_stream_after_a_long_outage_makes_the_page_say_ready_again = async () => {
  const env = bootOpen();
  env.sources[0].fail(true);
  await env.clock.advance(20000);
  assert.strictEqual(env.banner.hidden, false);
  env.last().open();
  await env.clock.advance(0);
  assertBannerHidden(env);
  const actions = env.actCalls().map((call) => JSON.parse(call.init.body).action);
  assert.deepStrictEqual(actions, ['ready']);
};

scenarios.a_post_failure_alone_does_not_make_the_page_say_ready_again = async () => {
  const env = bootOpen();
  env.installHandler();
  env.fetchHandler = (url) =>
    url === '/api/ping' ? jsonResponse(200, { ok: true, subscribers: 1 }) : jsonResponse(500, { reply: 'ERROR' });
  await env.send('search', '{}');
  await env.clock.advance(3000);
  assert.strictEqual(env.state(), 'open');
  assert.strictEqual(env.actCalls().length, 1, 'the stream never failed: no resync');
};

// Shutdown ---------------------------------------------------------------------------------------

scenarios.shutdown_stops_the_bridge_and_closes_the_window = async () => {
  const env = bootOpen();
  env.installHandler();
  env.sources[0].message({ action: 'shutdown', data: 'bye' }, 1);
  assert.strictEqual(env.banner.hidden, false);
  assert.strictEqual(env.banner.textContent, TEXT_STOPPED);
  assert.strictEqual(env.closeCalls, 1);
  assert.strictEqual(env.sources[0].closed, true);
  assert.strictEqual(env.state(), 'stopped');
  assert.deepStrictEqual(env.delivered, [], 'shutdown is not passed to the page');
  assert.strictEqual(env.clock.pending(), 0);
  const callsBefore = env.calls.length;
  assert.strictEqual(await env.send('search', '{}'), 'ERROR');
  assert.strictEqual(env.calls.length, callsBefore, 'no request after the shutdown');
  await env.clock.advance(120000);
  assert.strictEqual(env.sources.length, 1, 'no reconnect after the shutdown');
  assert.strictEqual(env.banner.textContent, TEXT_STOPPED);
};

scenarios.shutdown_survives_a_window_that_cannot_be_closed = async () => {
  const env = bootOpen();
  env.closeThrows = true;
  env.sources[0].message({ action: 'shutdown', data: '' }, 1);
  assert.strictEqual(env.closeCalls, 1);
  assert.strictEqual(env.banner.textContent, TEXT_STOPPED);
  assert.strictEqual(env.state(), 'stopped');
};

scenarios.shutdown_works_without_a_banner_element = async () => {
  const env = bootOpen({ banner: false });
  env.sources[0].message({ action: 'shutdown', data: '' }, 1);
  assert.strictEqual(env.closeCalls, 1);
  assert.strictEqual(env.state(), 'stopped');
};

scenarios.shutdown_drops_queued_messages_and_polling = async () => {
  const env = bootOpen();
  env.sources[0].message(status('queued'), 1);
  env.sources[0].message({ action: 'shutdown', data: '' }, 2);
  assert.strictEqual(env.clock.pending(), 0);
  env.installHandler();
  await env.clock.advance(1000);
  assert.deepStrictEqual(env.delivered, []);
};

scenarios.shutdown_replaces_the_lost_connection_banner = async () => {
  const env = bootOpen();
  env.sources[0].fail(true);
  await env.clock.advance(15000);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  env.last().open();
  env.last().message({ action: 'shutdown', data: '' }, 1);
  assert.strictEqual(env.banner.textContent, TEXT_STOPPED);
  assert.strictEqual(env.banner.hidden, false);
};

// Connection loss --------------------------------------------------------------------------------

scenarios.banner_after_15_seconds_and_recovery = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  assert.strictEqual(env.state(), 'broken');
  await env.clock.advance(14999);
  assertBannerHidden(env);
  await env.clock.advance(1);
  assert.strictEqual(env.banner.hidden, false);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  assert.strictEqual(env.state(), 'broken');
  env.last().open();
  assert.strictEqual(env.state(), 'open');
  assertBannerHidden(env);
  await env.clock.advance(200000);
  assert.strictEqual(env.closeCalls, 0);
  assertBannerHidden(env);
  assert.strictEqual(env.clock.pending(), 0);
};

scenarios.reconnect_within_15_seconds_never_shows_the_banner = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  await env.clock.advance(10000);
  env.sources[0].open(); // the browser reconnected by itself
  assert.strictEqual(env.state(), 'open');
  await env.clock.advance(200000);
  assertBannerHidden(env);
  assert.strictEqual(env.sources.length, 1);
  assert.strictEqual(env.closeCalls, 0);
};

scenarios.slow_first_open_shows_the_banner_and_the_open_hides_it = async () => {
  const env = boot();
  await env.clock.advance(14999);
  assertBannerHidden(env);
  assert.strictEqual(env.state(), 'connecting');
  await env.clock.advance(1);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  env.last().open();
  assertBannerHidden(env);
  assert.strictEqual(env.state(), 'open');
};

scenarios.retry_creates_a_fresh_event_source_when_the_browser_gave_up = async () => {
  const env = bootOpen();
  env.sources[0].fail(true);
  await env.clock.advance(2999);
  assert.strictEqual(env.sources.length, 1);
  await env.clock.advance(1);
  assert.strictEqual(env.sources.length, 2);
  assert.strictEqual(env.sources[0].closed, true);
  assert.strictEqual(env.sources[1].url, '/api/events');
  assert.notStrictEqual(env.sources[1], env.sources[0]);
  assert.strictEqual(env.pingCalls().length, 1);
  assert.strictEqual(env.pingCalls()[0].init.method, undefined, 'the probe is a GET');
  assert.strictEqual(env.pingCalls()[0].init.credentials, 'same-origin');
  env.sources[1].fail(true);
  await env.clock.advance(3000);
  assert.strictEqual(env.sources.length, 3);
  assert.strictEqual(env.pingCalls().length, 2);
};

scenarios.a_stale_event_source_cannot_change_the_state = async () => {
  const env = bootOpen();
  const first = env.sources[0];
  first.fail(true);
  await env.clock.advance(3000);
  env.sources[1].open();
  assert.strictEqual(env.state(), 'open');
  first.fail(true); // handlers were detached when it was replaced
  first.open();
  assert.strictEqual(env.state(), 'open');
};

scenarios.the_browsers_own_retry_is_left_alone_until_the_banner_shows = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  await env.clock.advance(14999);
  assert.strictEqual(env.sources.length, 1);
  assert.strictEqual(env.pingCalls().length > 0, true, 'the server is probed meanwhile');
  await env.clock.advance(1);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  assert.strictEqual(env.sources.length, 2, 'a fresh instance as soon as the banner shows');
  await env.clock.advance(3000);
  assert.strictEqual(env.sources.length, 3);
  await env.clock.advance(3000);
  assert.strictEqual(env.sources.length, 4);
  assert.strictEqual(env.sources.filter((source) => !source.closed).length, 1);
};

scenarios.a_successful_ping_clears_a_failed_post_while_the_stream_is_open = async () => {
  const env = bootOpen();
  env.installHandler();
  env.fetchHandler = (url) =>
    url === '/api/ping' ? jsonResponse(200, { ok: true, subscribers: 1 }) : jsonResponse(500, { reply: 'ERROR' });
  assert.strictEqual(await env.send('search', '{}'), 'ERROR');
  assert.strictEqual(env.state(), 'broken');
  await env.clock.advance(3000);
  assert.strictEqual(env.pingCalls().length, 1);
  assert.strictEqual(env.state(), 'open');
  assert.strictEqual(env.clock.pending(), 0);
  await env.clock.advance(100000);
  assertBannerHidden(env);
  assert.strictEqual(env.closeCalls, 0);
  assert.strictEqual(env.sources.length, 1, 'a working stream is never replaced');
};

scenarios.a_ping_alone_does_not_clear_the_banner = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  await env.clock.advance(15000);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  await env.clock.advance(9000); // pings succeed, the stream still does not open
  assert.strictEqual(env.pingCalls().length > 0, true);
  assert.strictEqual(env.banner.hidden, false);
  assert.strictEqual(env.state(), 'broken');
  env.last().open();
  assertBannerHidden(env);
};

scenarios.a_dead_session_shows_the_banner_and_closes_after_90_seconds = async () => {
  const env = bootOpen();
  env.installHandler();
  env.fetchHandler = () => jsonResponse(403, { reply: 'ERROR' });
  assert.strictEqual(await env.send('search', '{}'), 'ERROR');
  await env.clock.advance(14999);
  assertBannerHidden(env);
  await env.clock.advance(1);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  assert.strictEqual(env.sources.length, 1, 'the open stream is not thrown away');
  assert.strictEqual(env.sources[0].closed, false);
  await env.clock.advance(74999);
  assert.strictEqual(env.closeCalls, 0);
  await env.clock.advance(1);
  assert.strictEqual(env.closeCalls, 1);
};

scenarios.gives_up_after_90_seconds_and_keeps_the_banner = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  await env.clock.advance(89999);
  assert.strictEqual(env.closeCalls, 0);
  await env.clock.advance(1);
  assert.strictEqual(env.closeCalls, 1);
  assert.strictEqual(env.banner.hidden, false);
  assert.strictEqual(env.banner.textContent, TEXT_LOST);
  assert.strictEqual(env.state(), 'broken');
  const created = env.sources.length;
  await env.clock.advance(30000);
  assert.strictEqual(env.closeCalls, 1, 'closing is attempted once');
  assert.strictEqual(env.sources.length > created, true, 'it keeps retrying');
  assert.strictEqual(env.banner.hidden, false);
  env.last().open(); // and still recovers if the server comes back
  assert.strictEqual(env.state(), 'open');
  assertBannerHidden(env);
};

scenarios.the_90_second_clock_restarts_after_a_recovery = async () => {
  const env = bootOpen();
  env.sources[0].fail(false);
  await env.clock.advance(60000);
  env.last().open();
  env.last().fail(false);
  await env.clock.advance(89999);
  assert.strictEqual(env.closeCalls, 0);
  await env.clock.advance(1);
  assert.strictEqual(env.closeCalls, 1);
};

scenarios.failures_work_without_a_banner_element = async () => {
  const env = bootOpen({ banner: false });
  env.sources[0].fail(false);
  await env.clock.advance(100000);
  assert.strictEqual(env.closeCalls, 1);
  assert.strictEqual(env.state(), 'broken');
};

// Cleanup ----------------------------------------------------------------------------------------

for (const type of ['pagehide', 'beforeunload']) {
  scenarios[type + '_stops_retries_and_clears_the_timers'] = async () => {
    const env = bootOpen();
    env.sources[0].fail(true);
    await env.clock.advance(20000);
    assert.strictEqual(env.banner.hidden, false);
    const created = env.sources.length;
    const pings = env.pingCalls().length;
    assert.ok(env.listeners[type] && env.listeners[type].length > 0, 'listener registered');
    env.fire(type);
    assert.strictEqual(env.state(), 'stopped');
    assert.strictEqual(env.clock.pending(), 0);
    assert.strictEqual(env.last().closed, true);
    await env.clock.advance(200000);
    assert.strictEqual(env.sources.length, created);
    assert.strictEqual(env.pingCalls().length, pings);
    assert.strictEqual(env.closeCalls, 0, 'only a shutdown or the 90 s give up closes the window');
  };
}

scenarios.pagehide_while_a_probe_is_in_flight_clears_its_timer = async () => {
  const env = bootOpen();
  env.fetchHandler = (url) => (url === '/api/ping' ? new Promise(() => {}) : jsonResponse(200, { reply: 'OK' }));
  env.sources[0].fail(true);
  await env.clock.advance(3000);
  assert.strictEqual(env.pingCalls().length, 1);
  assert.strictEqual(env.clock.pending() > 0, true);
  env.fire('pagehide');
  await env.clock.advance(0);
  assert.strictEqual(env.clock.pending(), 0);
};

scenarios.pagehide_drops_late_events = async () => {
  const env = bootOpen();
  env.installHandler();
  const source = env.sources[0];
  env.fire('pagehide');
  source.message(status('late'), 1); // detached handlers: nothing happens
  assert.deepStrictEqual(env.delivered, []);
  assert.strictEqual(env.state(), 'stopped');
};

// Runner -----------------------------------------------------------------------------------------

async function main() {
  const [, , bridgePath, mode] = process.argv;
  if (!bridgePath || !fs.existsSync(bridgePath)) {
    throw new Error('usage: node http_bridge_harness.js <http-bridge.js> --list|--all');
  }
  if (mode === '--list') {
    process.stdout.write(JSON.stringify(Object.keys(scenarios)));
    return;
  }
  if (mode !== '--all') throw new Error('unknown mode ' + mode);
  const results = {};
  for (const [name, run] of Object.entries(scenarios)) {
    try {
      await run();
      results[name] = 'ok';
    } catch (error) {
      results[name] = String((error && error.stack) || error);
    }
  }
  process.stdout.write(JSON.stringify(results));
}

main().catch((error) => {
  process.stderr.write(String((error && error.stack) || error) + '\n');
  process.exit(2);
});
