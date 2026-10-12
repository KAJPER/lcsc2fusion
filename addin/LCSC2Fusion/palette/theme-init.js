/* LCSC2Fusion window preferences: theme and language. Loaded synchronously in <head> (a classic
 * script, no defer), so the saved choice is on <html> before the first paint and the page never
 * flashes the wrong scheme. CSP is default-src 'self', so this cannot be an inline script.
 *
 * What it does at load time:
 *   - data-theme="light" or "dark" on <html> forces a scheme; no attribute means System, which is
 *     the prefers-color-scheme media query in style.css;
 *   - both <meta name="theme-color"> tags follow the choice (the window colour is --bg);
 *   - <html lang> is the language: the saved choice, else the browser language when it is one of
 *     LANGUAGES, else English.
 * It also owns the two localStorage keys. Browser storage can be blocked (private mode, policy), so
 * every access is wrapped and the page works without it: a choice then lasts until the page closes.
 * Stored values are validated, anything unknown counts as "nothing saved".
 *
 * window.LcscPrefs is the read only API for app.js and i18n.js. To add a language: add it to
 * LANGUAGES here, add a dictionary and message patterns in i18n.js (see addin/README.md).
 */
(function () {
  'use strict';

  const THEME_KEY = 'lcsc2fusion.theme';
  const LANG_KEY = 'lcsc2fusion.lang';

  const SYSTEM = 'system';
  const THEMES = Object.freeze([SYSTEM, 'light', 'dark']);
  const DEFAULT_LANGUAGE = 'en';
  // `name` is the name of the language in that language: it is shown as is, never translated.
  const LANGUAGES = Object.freeze([
    Object.freeze({ code: 'en', name: 'English' }),
    Object.freeze({ code: 'pl', name: 'Polski' }),
  ]);

  const root = document.documentElement;

  function readStorage(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (err) {
      return false;
    }
  }

  function isTheme(value) {
    return typeof value === 'string' && THEMES.indexOf(value) >= 0;
  }

  function isLanguage(value) {
    return typeof value === 'string' && LANGUAGES.some((language) => language.code === value);
  }

  // The browser language ("pl-PL", "en-GB", ...) reduced to a supported code, else English.
  function detectLanguage() {
    const wanted = String(window.navigator.language || '').toLowerCase().split('-')[0];
    return isLanguage(wanted) ? wanted : DEFAULT_LANGUAGE;
  }

  function getTheme() {
    const saved = readStorage(THEME_KEY);
    return isTheme(saved) ? saved : SYSTEM;
  }

  function getLanguage() {
    const saved = readStorage(LANG_KEY);
    return isLanguage(saved) ? saved : detectLanguage();
  }

  // The theme-color metas as the HTML wrote them: one per scheme, picked by their media query.
  // They hold the System values; a forced theme sets both to the colour of that scheme.
  const metas = Array.prototype.slice
    .call(document.querySelectorAll('meta[name="theme-color"]'))
    .map((node) => {
      const media = node.getAttribute('media') || '';
      const scheme = media.indexOf('dark') >= 0 ? 'dark' : 'light';
      return { node: node, scheme: scheme, content: node.getAttribute('content') };
    });

  function colorOf(scheme) {
    const found = metas.find((meta) => meta.scheme === scheme);
    return found ? found.content : null;
  }

  function applyTheme(theme) {
    const choice = isTheme(theme) ? theme : SYSTEM;
    if (choice === SYSTEM) root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', choice);
    metas.forEach((meta) => {
      const color = choice === SYSTEM ? meta.content : colorOf(choice);
      if (color) meta.node.setAttribute('content', color);
    });
  }

  function applyLanguage(code) {
    root.setAttribute('lang', isLanguage(code) ? code : DEFAULT_LANGUAGE);
  }

  // Applies and remembers a choice. Returns false for a value that is not allowed (nothing changes).
  function setTheme(theme) {
    if (!isTheme(theme)) return false;
    writeStorage(THEME_KEY, theme);
    applyTheme(theme);
    return true;
  }

  function setLanguage(code) {
    if (!isLanguage(code)) return false;
    writeStorage(LANG_KEY, code);
    applyLanguage(code);
    return true;
  }

  applyTheme(getTheme());
  applyLanguage(getLanguage());

  Object.defineProperty(window, 'LcscPrefs', {
    value: Object.freeze({
      THEMES: THEMES,
      LANGUAGES: LANGUAGES,
      THEME_KEY: THEME_KEY,
      LANG_KEY: LANG_KEY,
      getTheme: getTheme,
      getLanguage: getLanguage,
      setTheme: setTheme,
      setLanguage: setLanguage,
    }),
  });
})();
