'use strict';
/* Test harness for addin/LCSC2Fusion/palette/theme-init.js and i18n.js (run by
 * tests/test_addin_i18n.py).
 *
 * Both scripts are plain browser scripts. They are evaluated in a vm context whose globals are
 * hand written fakes of what they touch: <html> with attributes, the theme-color metas,
 * localStorage (that can be blocked or throw), navigator.language and a small tree of nodes for
 * the data-i18n hooks.
 *
 * Usage:
 *   node i18n_harness.js <palette dir> --list   names of the scenarios (JSON)
 *   node i18n_harness.js <palette dir> --all    run all, JSON {name: "ok"|error}
 *   node i18n_harness.js <palette dir> --dump   JSON {languages, dictionaries, messages} of i18n.js
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const LIGHT = '#f2f4f8';
const DARK = '#1e2227';
const THEME_KEY = 'lcsc2fusion.theme';
const LANG_KEY = 'lcsc2fusion.lang';

class FakeNode {
  constructor(attributes = {}, text = '') {
    this.nodeType = 1;
    this.attributes = { ...attributes };
    this.textContent = text;
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }
}

function createEnv(options = {}) {
  const html = new FakeNode({ lang: 'en' });
  const metas = (options.metas || [
    { media: '(prefers-color-scheme: light)', content: LIGHT },
    { media: '(prefers-color-scheme: dark)', content: DARK },
  ]).map((meta) => new FakeNode({ name: 'theme-color', media: meta.media, content: meta.content }));
  const tree = options.tree || [];
  const store = { ...(options.stored || {}) };
  const writes = [];

  const storage = {
    getItem(key) {
      if (options.getThrows) throw new Error('SecurityError: get');
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    setItem(key, value) {
      writes.push([key, String(value)]);
      if (options.setThrows) throw new Error('QuotaExceededError');
      store[key] = String(value);
    },
  };

  // The only two queries the scripts make: the theme-color metas, and (i18n.js apply) every node
  // with a data-i18n hook, which is `tree`.
  const document = {
    documentElement: html,
    querySelectorAll(selector) {
      return selector === 'meta[name="theme-color"]' ? metas : tree;
    },
  };

  const sandbox = { document, console };
  sandbox.window = sandbox;
  if (options.storageBlocked) {
    Object.defineProperty(sandbox, 'localStorage', {
      get() {
        throw new Error('SecurityError: storage is blocked');
      },
    });
  } else {
    sandbox.localStorage = storage;
  }
  const navigator = {};
  if (options.language !== undefined) navigator.language = options.language;
  sandbox.navigator = navigator;
  vm.createContext(sandbox);

  const env = { sandbox, html, metas, store, writes, tree };
  env.load = (name) => {
    const file = path.join(process.argv[2], name);
    vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: name });
    return env;
  };
  env.metaContents = () => metas.map((meta) => meta.getAttribute('content'));
  return env;
}

const scenarios = {};

// theme-init.js: theme ------------------------------------------------------------------------

scenarios.theme_default_is_system_without_an_attribute = () => {
  const env = createEnv({ language: 'en-US' }).load('theme-init.js');
  assert.strictEqual(env.html.getAttribute('data-theme'), null);
  assert.strictEqual(env.sandbox.LcscPrefs.getTheme(), 'system');
  assert.deepStrictEqual(env.metaContents(), [LIGHT, DARK]);
  assert.deepStrictEqual(env.writes, [], 'loading never writes to the storage');
};

for (const theme of ['light', 'dark']) {
  scenarios['theme_saved_' + theme + '_is_applied_before_anything_else'] = () => {
    const env = createEnv({ stored: { [THEME_KEY]: theme }, language: 'en' }).load('theme-init.js');
    assert.strictEqual(env.html.getAttribute('data-theme'), theme);
    assert.strictEqual(env.sandbox.LcscPrefs.getTheme(), theme);
    const color = theme === 'dark' ? DARK : LIGHT;
    assert.deepStrictEqual(env.metaContents(), [color, color], 'both metas show the forced scheme');
  };
}

scenarios.theme_saved_system_leaves_no_attribute_and_the_original_metas = () => {
  const env = createEnv({ stored: { [THEME_KEY]: 'system' } }).load('theme-init.js');
  assert.strictEqual(env.html.getAttribute('data-theme'), null);
  assert.deepStrictEqual(env.metaContents(), [LIGHT, DARK]);
};

scenarios.invalid_saved_themes_count_as_nothing_saved = () => {
  for (const value of ['purple', '', 'DARK', ' dark', 'dark ', 'null', '{"theme":"dark"}', 'auto', '0']) {
    const env = createEnv({ stored: { [THEME_KEY]: value } }).load('theme-init.js');
    assert.strictEqual(env.html.getAttribute('data-theme'), null, JSON.stringify(value));
    assert.strictEqual(env.sandbox.LcscPrefs.getTheme(), 'system', JSON.stringify(value));
    assert.deepStrictEqual(env.metaContents(), [LIGHT, DARK], JSON.stringify(value));
  }
};

scenarios.set_theme_applies_persists_and_system_restores_the_metas = () => {
  const env = createEnv({ language: 'en' }).load('theme-init.js');
  const prefs = env.sandbox.LcscPrefs;
  assert.strictEqual(prefs.setTheme('dark'), true);
  assert.strictEqual(env.html.getAttribute('data-theme'), 'dark');
  assert.deepStrictEqual(env.metaContents(), [DARK, DARK]);
  assert.strictEqual(env.store[THEME_KEY], 'dark');
  assert.strictEqual(prefs.setTheme('light'), true);
  assert.deepStrictEqual(env.metaContents(), [LIGHT, LIGHT]);
  assert.strictEqual(env.store[THEME_KEY], 'light');
  assert.strictEqual(prefs.setTheme('system'), true);
  assert.strictEqual(env.html.getAttribute('data-theme'), null);
  assert.deepStrictEqual(env.metaContents(), [LIGHT, DARK], 'System puts the original values back');
  assert.strictEqual(env.store[THEME_KEY], 'system');
  assert.strictEqual(prefs.getTheme(), 'system');
};

scenarios.set_theme_refuses_values_that_are_not_allowed = () => {
  const env = createEnv({}).load('theme-init.js');
  const prefs = env.sandbox.LcscPrefs;
  for (const value of ['purple', '', 'Dark', null, undefined, 3, {}, ['dark']]) {
    assert.strictEqual(prefs.setTheme(value), false, String(value));
  }
  assert.deepStrictEqual(env.writes, []);
  assert.strictEqual(env.html.getAttribute('data-theme'), null);
};

scenarios.unusual_theme_color_metas_do_not_break_the_script = () => {
  const none = createEnv({ metas: [], stored: { [THEME_KEY]: 'dark' } }).load('theme-init.js');
  assert.strictEqual(none.html.getAttribute('data-theme'), 'dark');
  const one = createEnv({
    metas: [{ media: '(prefers-color-scheme: dark)', content: DARK }],
    stored: { [THEME_KEY]: 'light' },
  }).load('theme-init.js');
  assert.strictEqual(one.html.getAttribute('data-theme'), 'light');
  assert.deepStrictEqual(one.metaContents(), [DARK], 'no light colour known: the meta is left alone');
};

// theme-init.js: language ----------------------------------------------------------------------

scenarios.language_follows_the_browser_when_nothing_is_saved = () => {
  const cases = [
    ['pl', 'pl'],
    ['pl-PL', 'pl'],
    ['PL-pl', 'pl'],
    ['pl_PL', 'en'],
    ['plx', 'en'],
    ['en-US', 'en'],
    ['en-GB', 'en'],
    ['de-DE', 'en'],
    ['', 'en'],
    [undefined, 'en'],
  ];
  for (const [browser, expected] of cases) {
    const env = createEnv({ language: browser }).load('theme-init.js');
    assert.strictEqual(env.html.getAttribute('lang'), expected, String(browser));
    assert.strictEqual(env.sandbox.LcscPrefs.getLanguage(), expected, String(browser));
  }
};

scenarios.a_saved_language_wins_over_the_browser = () => {
  const polish = createEnv({ language: 'en-US', stored: { [LANG_KEY]: 'pl' } }).load('theme-init.js');
  assert.strictEqual(polish.html.getAttribute('lang'), 'pl');
  const english = createEnv({ language: 'pl-PL', stored: { [LANG_KEY]: 'en' } }).load('theme-init.js');
  assert.strictEqual(english.html.getAttribute('lang'), 'en');
};

scenarios.an_invalid_saved_language_falls_back_to_the_browser = () => {
  for (const value of ['xx', '', 'PL', 'pl-PL', ' pl', 'null']) {
    const env = createEnv({ language: 'pl-PL', stored: { [LANG_KEY]: value } }).load('theme-init.js');
    assert.strictEqual(env.html.getAttribute('lang'), 'pl', JSON.stringify(value));
  }
};

scenarios.set_language_persists_applies_and_refuses_unknown_codes = () => {
  const env = createEnv({ language: 'en-US' }).load('theme-init.js');
  const prefs = env.sandbox.LcscPrefs;
  assert.strictEqual(prefs.setLanguage('pl'), true);
  assert.strictEqual(env.html.getAttribute('lang'), 'pl');
  assert.strictEqual(env.store[LANG_KEY], 'pl');
  assert.strictEqual(prefs.getLanguage(), 'pl');
  for (const value of ['xx', '', 'PL', null, undefined, 1]) {
    assert.strictEqual(prefs.setLanguage(value), false, String(value));
  }
  assert.strictEqual(env.html.getAttribute('lang'), 'pl');
};

scenarios.the_language_list_is_frozen_and_names_each_language_in_itself = () => {
  const env = createEnv({}).load('theme-init.js');
  const prefs = env.sandbox.LcscPrefs;
  assert.ok(Object.isFrozen(prefs));
  assert.ok(Object.isFrozen(prefs.LANGUAGES));
  assert.ok(prefs.LANGUAGES.every((language) => Object.isFrozen(language)));
  assert.deepStrictEqual(Array.from(prefs.THEMES), ['system', 'light', 'dark']);
  const names = Object.fromEntries(prefs.LANGUAGES.map((language) => [language.code, language.name]));
  assert.strictEqual(names.en, 'English');
  assert.strictEqual(names.pl, 'Polski');
  assert.strictEqual(Reflect.set(env.sandbox, 'LcscPrefs', null), false);
};

// theme-init.js: storage that does not work -------------------------------------------------------

scenarios.blocked_storage_still_gives_a_working_page = () => {
  const cases = [
    { storageBlocked: true },
    { getThrows: true, setThrows: true },
    { setThrows: true },
  ];
  for (const options of cases) {
    const env = createEnv({ language: 'pl-PL', ...options }).load('theme-init.js');
    const prefs = env.sandbox.LcscPrefs;
    assert.strictEqual(env.html.getAttribute('lang'), 'pl', JSON.stringify(options));
    assert.strictEqual(prefs.getTheme(), 'system');
    assert.strictEqual(prefs.setTheme('dark'), true, 'the choice still applies for this page');
    assert.strictEqual(env.html.getAttribute('data-theme'), 'dark');
    assert.strictEqual(prefs.setLanguage('en'), true);
    assert.strictEqual(env.html.getAttribute('lang'), 'en');
  }
};

// i18n.js ----------------------------------------------------------------------------------------

function loadBoth(options) {
  return createEnv(options).load('theme-init.js').load('i18n.js');
}

scenarios.i18n_starts_in_the_language_of_the_prefs = () => {
  assert.strictEqual(loadBoth({ language: 'en-US' }).sandbox.LcscI18n.getLanguage(), 'en');
  assert.strictEqual(loadBoth({ language: 'pl-PL' }).sandbox.LcscI18n.getLanguage(), 'pl');
  assert.strictEqual(
    loadBoth({ language: 'pl-PL', stored: { [LANG_KEY]: 'en' } }).sandbox.LcscI18n.getLanguage(),
    'en'
  );
  const alone = createEnv({}).load('i18n.js'); // without theme-init.js
  assert.strictEqual(alone.sandbox.LcscI18n.getLanguage(), 'en');
};

scenarios.i18n_languages_are_the_ones_of_the_prefs = () => {
  const env = loadBoth({});
  const codes = Array.from(env.sandbox.LcscPrefs.LANGUAGES, (language) => language.code).sort();
  assert.deepStrictEqual(Array.from(env.sandbox.LcscI18n.languages()).sort(), codes);
  assert.strictEqual(env.sandbox.LcscI18n.FALLBACK, 'en');
};

scenarios.t_fills_placeholders_and_falls_back_to_english_then_the_key = () => {
  const env = loadBoth({ language: 'pl-PL' });
  const i18n = env.sandbox.LcscI18n;
  assert.strictEqual(i18n.t('link.lcscLabel', { id: 'C2040' }), 'Otwórz C2040 w LCSC');
  assert.strictEqual(i18n.t('link.lcscLabel', { id: 'C2040' }, 'en'), 'Open C2040 on LCSC');
  assert.strictEqual(i18n.t('error.cannotReach'), 'Nie udało się połączyć z Fusion: {reason}');
  assert.strictEqual(i18n.t('no.such.key'), 'no.such.key');
  delete i18n.DICTIONARIES.pl['error.unknown'];
  assert.strictEqual(i18n.t('error.unknown'), 'Unknown error.', 'a gap in Polish shows the English text');
  assert.strictEqual(i18n.t('results.headingFor', { query: 'a<b' }), 'Wyniki dla "a<b"');
};

scenarios.t_picks_the_plural_form_of_the_language = () => {
  const i18n = loadBoth({ language: 'pl-PL' }).sandbox.LcscI18n;
  const polish = [1, 2, 3, 4, 5, 12, 22, 25, 112].map((n) => i18n.t('results.count', { n }));
  assert.deepStrictEqual(polish, [
    '1 część',
    '2 części',
    '3 części',
    '4 części',
    '5 części',
    '12 części',
    '22 części',
    '25 części',
    '112 części',
  ]);
  const english = [1, 2, 0].map((n) => i18n.t('results.count', { n }, 'en'));
  assert.deepStrictEqual(english, ['1 part', '2 parts', '0 parts']);
  assert.strictEqual(i18n.t('results.count', {}, 'en'), '{n} parts', 'without n the plain form');
};

scenarios.set_language_switches_t_and_the_lang_attribute = () => {
  const env = loadBoth({ language: 'en-US' });
  const i18n = env.sandbox.LcscI18n;
  assert.strictEqual(i18n.t('search.button'), 'Search');
  assert.strictEqual(i18n.setLanguage('pl'), true);
  assert.strictEqual(i18n.t('search.button'), 'Szukaj');
  assert.strictEqual(i18n.getLanguage(), 'pl');
  assert.strictEqual(env.html.getAttribute('lang'), 'pl');
  assert.strictEqual(i18n.setLanguage('xx'), false);
  assert.strictEqual(i18n.setLanguage(undefined), false);
  assert.strictEqual(i18n.getLanguage(), 'pl');
  assert.strictEqual(i18n.setLanguage('en'), true);
  assert.strictEqual(env.html.getAttribute('lang'), 'en');
  assert.strictEqual(env.store[LANG_KEY], undefined, 'i18n.js never writes to the storage');
};

scenarios.apply_writes_text_attributes_params_and_messages = () => {
  const text = new FakeNode({ 'data-i18n': 'search.button' }, 'Search');
  const link = new FakeNode(
    {
      'data-i18n': 'link.sheetText',
      'data-i18n-attr': 'aria-label:link.sheetLabel;title:link.sheetTitle',
      'data-i18n-params': JSON.stringify({ id: 'C1' }),
    },
    'Datasheet'
  );
  const message = new FakeNode({ 'data-msg': 'Another request is still running. Please wait for it.' }, '');
  const bogusParams = new FakeNode({ 'data-i18n': 'results.headingFor', 'data-i18n-params': '{not json' }, '');
  const plain = new FakeNode({}, 'untouched');
  const env = loadBoth({ language: 'pl-PL', tree: [text, link, message, bogusParams, plain] });
  const i18n = env.sandbox.LcscI18n;
  const saved = i18n.MESSAGES.splice(0, i18n.MESSAGES.length);
  i18n.MESSAGES.push({ re: /^Another request is still running\. Please wait for it\.$/, pl: '@status.busy' });
  i18n.apply(env.sandbox.document);
  assert.strictEqual(text.textContent, 'Szukaj');
  assert.strictEqual(link.textContent, 'Karta katalogowa');
  assert.strictEqual(link.getAttribute('aria-label'), 'Otwórz kartę katalogową C1');
  assert.strictEqual(link.getAttribute('title'), 'Otwórz kartę katalogową C1 (otworzy się w przeglądarce)');
  assert.strictEqual(message.textContent, i18n.t('status.busy'));
  assert.strictEqual(bogusParams.textContent, 'Wyniki dla "{query}"', 'broken params are ignored');
  assert.strictEqual(plain.textContent, 'untouched');
  i18n.setLanguage('en');
  i18n.apply(env.sandbox.document);
  assert.strictEqual(text.textContent, 'Search');
  assert.strictEqual(link.getAttribute('aria-label'), 'Open the datasheet of C1');
  assert.strictEqual(message.textContent, 'Another request is still running. Please wait for it.');
  i18n.MESSAGES.splice(0, i18n.MESSAGES.length, ...saved);
};

scenarios.apply_works_on_one_element_and_on_a_fragment_like_root = () => {
  const node = new FakeNode({ 'data-i18n': 'search.button' }, 'Search');
  node.querySelectorAll = () => [];
  const env = loadBoth({ language: 'pl-PL' });
  env.sandbox.LcscI18n.apply(node);
  assert.strictEqual(node.textContent, 'Szukaj');
};

// Messages from Python (the mechanism, with entries made up here) --------------------------------

function withMessages(entries, language, run) {
  const env = loadBoth({ language });
  const i18n = env.sandbox.LcscI18n;
  const saved = i18n.MESSAGES.splice(0, i18n.MESSAGES.length);
  i18n.MESSAGES.push(...entries);
  try {
    run(i18n);
  } finally {
    i18n.MESSAGES.splice(0, i18n.MESSAGES.length, ...saved);
  }
}

scenarios.translate_message_never_touches_english_and_unknown_text = () => {
  withMessages([{ re: /^Hello (?<name>.+)$/, pl: 'Cześć {name}' }], 'pl-PL', (i18n) => {
    assert.strictEqual(i18n.translateMessage('Hello Ola'), 'Cześć Ola');
    assert.strictEqual(i18n.translateMessage('  Hello Ola \n'), 'Cześć Ola', 'surrounding blanks are ignored');
    assert.strictEqual(i18n.translateMessage('Hello Ola', 'en'), 'Hello Ola', 'English is never translated');
    assert.strictEqual(i18n.translateMessage('C1: pad 41 is shared by 9 pads'), 'C1: pad 41 is shared by 9 pads');
    assert.strictEqual(i18n.translateMessage(''), '');
    assert.strictEqual(i18n.translateMessage(null), '');
    assert.strictEqual(i18n.translateMessage(undefined), '');
    assert.strictEqual(i18n.translateMessage(12), '12');
    assert.strictEqual(i18n.findMessage('nothing here'), null);
    assert.strictEqual(i18n.findMessage('Hello Ola').index, 0);
  });
};

scenarios.translate_message_first_match_wins_and_a_language_without_a_template_falls_through = () => {
  const entries = [
    { re: /^Saved (?<id>C\d+) to (?<where>.+)$/, pl: 'Zapisano {id} w {where}' },
    { re: /^Saved (?<what>.+)$/, pl: 'Zapisano coś: {what}', xx: 'never used' },
  ];
  withMessages(entries, 'pl-PL', (i18n) => {
    assert.strictEqual(i18n.translateMessage('Saved C1 to D:\\x'), 'Zapisano C1 w D:\\x');
    assert.strictEqual(i18n.translateMessage('Saved a thing'), 'Zapisano coś: a thing');
    i18n.DICTIONARIES.xx = { 'search.button': 'XX' };
    assert.strictEqual(i18n.setLanguage('xx'), true);
    assert.strictEqual(i18n.translateMessage('Saved C1 to D:\\x'), 'Saved C1 to D:\\x', 'no template for xx');
    assert.strictEqual(i18n.translateMessage('Saved a thing'), 'never used');
    delete i18n.DICTIONARIES.xx;
  });
};

scenarios.translate_message_supports_plural_templates_dictionary_keys_and_nested_messages = () => {
  const entries = [
    {
      re: /^Found (?<n>\d+) part\(s\) for (?<query>.+)\.$/,
      pl: { one: '{n} część dla {query}', few: '{n} części dla {query}', other: '{n} części (inne) dla {query}' },
    },
    { re: /^Busy now\.$/, pl: '@status.busy' },
    {
      re: /^(?<head>Created the library with C1\.) The 3D package editor is open\.$/,
      pl: '{head:msg} Edytor pakietu 3D jest otwarty.',
    },
    { re: /^Created the library with (?<id>C\d+)\.$/, pl: 'Utworzono bibliotekę z {id}.' },
  ];
  withMessages(entries, 'pl-PL', (i18n) => {
    assert.strictEqual(i18n.translateMessage("Found 1 part(s) for 'a'."), "1 część dla 'a'");
    assert.strictEqual(i18n.translateMessage("Found 3 part(s) for 'a'."), "3 części dla 'a'");
    assert.strictEqual(i18n.translateMessage("Found 7 part(s) for 'a'."), "7 części (inne) dla 'a'");
    assert.strictEqual(i18n.translateMessage('Busy now.'), i18n.t('status.busy'));
    assert.strictEqual(
      i18n.translateMessage('Created the library with C1. The 3D package editor is open.'),
      'Utworzono bibliotekę z C1. Edytor pakietu 3D jest otwarty.'
    );
    assert.strictEqual(i18n.translateMessage('Found x part(s) for q.'), 'Found x part(s) for q.');
  });
};

scenarios.translate_message_leaves_group_values_alone_even_when_they_look_like_markup = () => {
  withMessages([{ re: /^Open (?<path>.+)$/, pl: 'Otwórz {path} ({path})' }], 'pl-PL', (i18n) => {
    assert.strictEqual(i18n.translateMessage('Open <b>$1</b> {id} $&'), 'Otwórz <b>$1</b> {id} $& (<b>$1</b> {id} $&)');
  });
};

scenarios.the_i18n_api_is_read_only = () => {
  const env = loadBoth({});
  const i18n = env.sandbox.LcscI18n;
  assert.ok(Object.isFrozen(i18n));
  assert.strictEqual(Reflect.set(env.sandbox, 'LcscI18n', null), false);
  assert.strictEqual(Reflect.set(i18n, 't', () => 'hacked'), false);
};

// Runner -----------------------------------------------------------------------------------------

function dump() {
  const env = loadBoth({});
  const i18n = env.sandbox.LcscI18n;
  const messages = i18n.MESSAGES.map((entry) => {
    const { re, ...templates } = entry;
    return { source: re.source, flags: re.flags, templates };
  });
  const prefs = env.sandbox.LcscPrefs;
  return {
    languages: i18n.languages(),
    prefs_languages: prefs.LANGUAGES.map((language) => language.code),
    prefs_themes: Array.from(prefs.THEMES),
    dictionaries: i18n.DICTIONARIES,
    messages,
  };
}

function main() {
  const [, , paletteDir, mode] = process.argv;
  if (!paletteDir || !fs.existsSync(path.join(paletteDir, 'i18n.js'))) {
    throw new Error('usage: node i18n_harness.js <palette dir> --list|--all|--dump');
  }
  if (mode === '--list') {
    process.stdout.write(JSON.stringify(Object.keys(scenarios)));
    return;
  }
  if (mode === '--dump') {
    process.stdout.write(JSON.stringify(dump()));
    return;
  }
  if (mode !== '--all') throw new Error('unknown mode ' + mode);
  const results = {};
  for (const [name, run] of Object.entries(scenarios)) {
    try {
      run();
      results[name] = 'ok';
    } catch (error) {
      results[name] = String((error && error.stack) || error);
    }
  }
  process.stdout.write(JSON.stringify(results));
}

main();
