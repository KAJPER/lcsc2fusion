"""Browser driver for tests/test_addin_update_ui.py (the update notice of the window).

It needs Playwright, which is not a dependency of the project, so the test runs this file with
whichever Python on the machine has it. Usage: python update_ui_browser.py BASE_URL SHOT_DIR

BASE_URL is the fixture server of tests/test_addin_http_bridge.py (palette files, the real HTTP
bridge, a fake controller that never answers with `update`). The driver plays the controller of
the update notice itself: it feeds scripted `update` messages to the page handler (the call the
bridge makes for a message of Python, plus one message through the real event stream) and records
what the page shows and what it sends. It prints ONE JSON report line, or {"skipped": reason}
when no browser can be started.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

WIDTH = 520
HEIGHT = 900
SENT_AT = 1_760_000_000.0  # 2025-10-09: a fixed "last check" time for the texts
RELEASES = "https://github.com/KAJPER/lcsc2fusion/releases/"
UPDATE_ACTIONS = {"check_update", "dismiss_update", "set_update_check"}

FEED = "(message) => window.fusionJavaScriptHandler.handle('update', JSON.stringify(message))"

# Everything the banner shows, as the page really renders it.
BANNER_PROBE = """() => {
  const banner = document.getElementById('update-banner');
  const links = Array.from(banner.querySelectorAll('a')).map((a) => ({
    kind: a.className, href: a.getAttribute('href'), target: a.target, rel: a.rel,
    text: a.textContent, label: a.getAttribute('aria-label'), title: a.title,
  }));
  const dismiss = document.getElementById('update-dismiss');
  const box = banner.getBoundingClientRect();
  return {
    hidden: banner.hidden,
    shown: box.width > 0 && box.height > 0,
    role: banner.getAttribute('role'),
    text: document.getElementById('update-text').textContent,
    hint: document.getElementById('update-hint').textContent,
    links: links,
    dismissText: dismiss.textContent,
    dismissTitle: dismiss.title,
    focus: document.activeElement.id || document.activeElement.className,
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}"""

# The Updates group of the settings panel.
SETTINGS_PROBE = """() => {
  const result = document.getElementById('update-result');
  const checked = document.getElementById('update-checked');
  const button = document.getElementById('update-check');
  return {
    group: document.querySelector('.setting-updates legend').textContent,
    checkbox: document.getElementById('update-enabled').checked,
    label: document.querySelector('label[for=update-enabled] span').textContent,
    privacy: document.getElementById('update-privacy').textContent,
    button: button.textContent,
    buttonDisabled: button.disabled,
    result: result.textContent,
    resultRole: result.getAttribute('role'),
    resultQuiet: result.classList.contains('sr-only'),
    resultError: result.classList.contains('update-result-error'),
    checked: checked.textContent,
    checkedHidden: checked.hidden,
  };
}"""

# The element that has the keyboard focus after each of four Tab presses from the Settings button.
TAB_PROBE = "() => { const e = document.activeElement; return e.id || e.className; }"

# Parts of the notice and of the settings panel that stick out of the window (a clipped text or a
# button off the edge), and whether the page scrolls sideways.
FIT_PROBE = """() => {
  const limit = document.documentElement.clientWidth;
  const outside = [];
  const nodes = document.querySelectorAll(
    '#update-banner, #update-banner *, #settings-panel, #settings-panel *');
  nodes.forEach((node) => {
    const box = node.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) return;
    if (box.right > limit + 0.5 || box.left < -0.5) outside.push(node.id || node.className);
  });
  return {
    width: limit,
    outside: outside,
    sideways: document.documentElement.scrollWidth - limit,
  };
}"""


def control(base: str, path: str, payload: dict[str, Any] | None = None) -> None:
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def poll(check: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    """Waits for `check` (page.wait_for_function would need eval, which the page's CSP refuses)."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.05)


def message(state: str = "current", **fields: Any) -> dict[str, Any]:
    """An `update` message with every field, like the controller sends it."""
    base: dict[str, Any] = {
        "state": state,
        "current": "0.1.0",
        "latest": None,
        "release_url": None,
        "installer_url": None,
        "checked_at": SENT_AT,
        "enabled": True,
        "dismissed": False,
    }
    base.update(fields)
    return base


def newer(version: str = "0.2.0", **fields: Any) -> dict[str, Any]:
    """The message of a newer release, with the two URLs that Python builds from `version`."""
    values: dict[str, Any] = {
        "latest": version,
        "release_url": f"{RELEASES}tag/v{version}",
        "installer_url": f"{RELEASES}download/v{version}/LCSC2Fusion-Setup-{version}.exe",
    }
    values.update(fields)
    return message("available", **values)


def run(page: Any, context: Any, base: str, shot: Callable[[str], None]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    sent: list[dict[str, Any]] = []
    refused: set[str] = set()

    def on_request(request: Any) -> None:
        if request.method != "POST" or not request.url.endswith("/api/act"):
            return
        body = json.loads(request.post_data or "{}")
        sent.append({"action": body.get("action"), "data": json.loads(body.get("data") or "{}")})

    def on_route(route: Any) -> None:
        body = json.loads(route.request.post_data or "{}")
        if body.get("action") in refused:
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps({"reply": "ERROR"})
            )
        else:
            route.continue_()

    page.on("request", on_request)
    page.route("**/api/act", on_route)

    def banner() -> dict[str, Any]:
        return page.evaluate(BANNER_PROBE)

    def settings() -> dict[str, Any]:
        return page.evaluate(SETTINGS_PROBE)

    def feed(payload: dict[str, Any]) -> None:
        assert page.evaluate(FEED, payload) == "OK"

    def update_requests() -> list[dict[str, Any]]:
        return [item for item in sent if item["action"] in UPDATE_ACTIONS]

    def click(selector: str) -> list[dict[str, Any]]:
        """Clicks and returns the update requests that the click made."""
        before = len(update_requests())
        page.click(selector)
        poll(lambda: len(update_requests()) > before, f"a request after {selector}")
        return update_requests()[before:]

    def reset() -> None:
        feed(message("current", latest="0.1.0"))

    # 1) the page is up; nothing is known about updates yet ----------------------------------------
    control(base, "/__test/delay", {"seconds": 0})
    page.goto(base + "/")
    poll(lambda: "Ready (fixture)" in page.inner_text("#status-text"), "the ready status")
    found["initial"] = {"banner": banner(), "settings": settings()}
    found["initial_requests"] = [item["action"] for item in sent]

    # 2) a message through the real event stream, the rest straight to the handler ----------------
    control(base, "/__test/publish", {"action": "update", "data": json.dumps(newer())})
    poll(lambda: page.locator("#update-banner").is_visible(), "the banner (event stream)")
    found["available"] = banner()
    found["available_settings"] = settings()
    shot("update_banner_light")
    page.emulate_media(color_scheme="dark")
    shot("update_banner_dark")
    page.emulate_media(color_scheme="light")

    # keyboard: the links come in reading order and Dismiss comes last
    page.focus("#settings-toggle")
    order = []
    for _ in range(4):
        page.keyboard.press("Tab")
        order.append(page.evaluate(TAB_PROBE))
    found["tab_order"] = order
    page.focus("#query")

    # 3) no installer in the release: the text, the release link and Dismiss stay -----------------
    feed(newer(installer_url=None))
    found["no_installer"] = banner()
    feed(newer(release_url=None))
    found["no_release_link"] = banner()
    feed(newer(release_url=None, installer_url=None))
    found["no_links"] = banner()

    # 4) hostile or malformed data never becomes a link or a banner -------------------------------
    hostile = {
        "javascript": newer(release_url="javascript:alert(1)", installer_url="javascript:alert(2)"),
        "other_host": newer(
            release_url="https://evil.example/KAJPER/lcsc2fusion/releases/tag/v0.2.0",
            installer_url="https://evil.example/LCSC2Fusion-Setup-0.2.0.exe",
        ),
        "http": newer(
            release_url="http://github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0",
            installer_url="http://github.com/KAJPER/lcsc2fusion/releases/download/v0.2.0/"
            "LCSC2Fusion-Setup-0.2.0.exe",
        ),
        "wrong_asset": newer(installer_url=f"{RELEASES}download/v0.2.0/evil.exe"),
        "other_version_in_url": newer(
            release_url=f"{RELEASES}tag/v0.9.9",
            installer_url=f"{RELEASES}download/v0.9.9/LCSC2Fusion-Setup-0.9.9.exe",
        ),
        "query_and_fragment": newer(
            release_url=f"{RELEASES}tag/v0.2.0?next=1",
            installer_url=f"{RELEASES}download/v0.2.0/LCSC2Fusion-Setup-0.2.0.exe#x",
        ),
        "userinfo_and_port": newer(
            release_url="https://user@github.com/KAJPER/lcsc2fusion/releases/tag/v0.2.0",
            installer_url="https://github.com:8443/KAJPER/lcsc2fusion/releases/download/v0.2.0/"
            "LCSC2Fusion-Setup-0.2.0.exe",
        ),
        "not_strings": newer(release_url=12, installer_url=["x"]),
    }
    found["hostile"] = {}
    dialogs: list[str] = []

    def on_dialog(dialog: Any) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", on_dialog)
    for name, payload in hostile.items():
        feed(payload)
        found["hostile"][name] = banner()
    found["hostile_dialogs"] = dialogs
    # a version that is not X.Y.Z (or a text that tries to be markup) never makes a banner
    found["bad_versions"] = {}
    for version in ("0.2", "v0.2.0", "01.2.3", "0.2.0-evil", "0.2.0.1", "1234567.0.0", "<b>x</b>"):
        feed(newer(version=version))
        found["bad_versions"][version] = banner()["hidden"]
    feed(message("available", latest=None))
    found["available_without_version"] = banner()["hidden"]
    feed(message("available", latest=123))
    found["available_with_number"] = banner()["hidden"]
    feed(newer())
    feed({**newer("0.9.9"), "state": "no_such_state"})  # not one of the five states: ignored
    found["unknown_state"] = banner()

    # 5) the settings panel ----------------------------------------------------------------------
    feed(newer())
    page.click("#settings-toggle")
    found["panel_open"] = settings()
    shot("update_settings_light")

    # a language switch rewrites the banner, the group and the result line, and keeps the state
    found["english"] = {"banner": banner(), "settings": settings()}
    page.check("#language-pl")
    found["polish"] = {"banner": banner(), "settings": settings()}
    page.check("#language-en")
    found["english_again"] = {"banner": banner(), "settings": settings()}

    # an automatic check that fails is silent, and it never hides a banner that is shown
    feed(newer())
    feed(message("checking"))
    found["automatic_checking"] = {"banner": banner(), "settings": settings()}
    feed(message("error"))
    found["automatic_error"] = {"banner": banner(), "settings": settings()}

    # 6) "Check now": the request, the overlay and every kind of answer ---------------------------
    found["press_1"] = click("#update-check")
    found["pressed_1"] = {"banner": banner(), "settings": settings()}
    feed(message("checking", latest="0.2.0"))
    found["checking_1"] = {"banner": banner(), "settings": settings()}
    feed(message("error"))  # a failed check: the line says so, the banner stays
    found["error_1"] = {"banner": banner(), "settings": settings()}
    shot("update_error_light")
    page.check("#language-pl")
    found["error_1_polish"] = {"banner": banner(), "settings": settings()}
    page.check("#language-en")

    found["press_2"] = click("#update-check")
    feed(message("checking"))
    page.focus("a.update-download")
    feed(message("checking"))
    feed(newer())  # the same version again: the banner is not rebuilt, the focus is not lost
    found["focus_kept"] = page.evaluate(TAB_PROBE)
    found["available_result"] = {"banner": banner(), "settings": settings()}
    page.check("#language-pl")
    found["available_result_polish"] = settings()
    page.check("#language-en")

    found["press_3"] = click("#update-check")
    feed(message("checking"))
    feed(message("current", latest="0.1.0"))
    found["current_result"] = {"banner": banner(), "settings": settings()}
    page.check("#language-pl")
    found["current_result_polish"] = settings()
    page.check("#language-en")
    shot("update_current_light")

    # while the answer is pending, the button cannot be pressed again
    before = len(update_requests())
    page.click("#update-check")
    poll(lambda: len(update_requests()) > before, "a request")
    page.click("#update-check", force=True)  # a second click on the disabled button
    time.sleep(0.2)
    found["double_press_requests"] = len(update_requests()) - before
    feed(message("current", latest="0.1.0"))

    # a request that Python refused: the page goes back, and "Check now" reports it
    refused.add("check_update")
    found["press_refused"] = click("#update-check")
    poll(lambda: not page.evaluate(SETTINGS_PROBE)["buttonDisabled"], "the button after a refusal")
    found["refused_check"] = settings()
    refused.clear()

    # 7) Dismiss ----------------------------------------------------------------------------------
    feed(newer())
    page.focus("#update-dismiss")
    found["dismiss_request"] = click("#update-dismiss")
    found["dismissed_now"] = banner()  # hidden at once, before any answer
    feed(newer(dismissed=True))
    found["dismissed_confirmed"] = banner()
    feed(newer("0.3.0"))
    found["newer_version_shows_again"] = banner()
    feed(newer("0.3.0", dismissed=True))
    found["newer_dismissed"] = banner()["hidden"]

    # a refused dismissal brings the banner back
    feed(newer("0.4.0"))
    refused.add("dismiss_update")
    found["dismiss_refused_request"] = click("#update-dismiss")
    poll(lambda: page.locator("#update-banner").is_visible(), "the banner after a refusal")
    found["dismiss_refused"] = banner()
    refused.clear()

    # 8) the checkbox ----------------------------------------------------------------------------
    reset()
    page.uncheck("#update-enabled")
    poll(lambda: any(item["action"] == "set_update_check" for item in sent), "the switch request")
    found["switch_off_request"] = [
        i for i in update_requests() if i["action"] == "set_update_check"
    ]
    feed(message("disabled", enabled=False))
    found["switched_off"] = settings()
    found["switched_off_banner_hidden"] = banner()["hidden"]
    sent.clear()
    page.check("#update-enabled")
    poll(lambda: any(item["action"] == "set_update_check" for item in sent), "the switch request")
    found["switch_on_request"] = update_requests()
    feed(message("current", latest="0.1.0", enabled=True))
    found["switched_on"] = settings()
    # a message decides what the box shows, whoever clicked
    feed(message("current", latest="0.1.0", enabled=False))
    found["box_follows_message"] = settings()["checkbox"]
    feed(message("current", latest="0.1.0", enabled=True))
    # the same while Python refuses the request: the box goes back to what Python has
    refused.add("set_update_check")
    page.click("#update-enabled")
    poll(lambda: page.evaluate(SETTINGS_PROBE)["checkbox"] is True, "the box after a refusal")
    found["switch_refused"] = settings()["checkbox"]
    refused.clear()

    # 9) the time of the last check --------------------------------------------------------------
    feed(message("current", latest="0.1.0", checked_at=SENT_AT))
    found["last_check"] = settings()
    feed(message("current", latest="0.1.0", checked_at=None))
    found["no_last_check"] = settings()
    feed(message("current", latest="0.1.0", checked_at="yesterday"))
    found["bad_last_check"] = settings()

    # 10) a check that never ends does not leave the button disabled for good --------------------
    feed(newer())
    click("#update-check")
    page.clock.run_for(30000)  # the clock also ticks by itself: stay well below 45 s
    found["guard_before"] = settings()
    page.clock.run_for(20000)
    found["guard_after"] = {"banner": banner(), "settings": settings()}
    feed(message("checking"))  # the same for a check that nobody asked for
    found["guard_auto_before"] = settings()
    page.clock.run_for(60000)
    found["guard_auto_after"] = {"banner": banner(), "settings": settings()}

    # 11) narrow windows, Polish (the longest texts) -------------------------------------------
    feed(newer())
    page.check("#language-pl")
    found["fit"] = {}
    for width in (420, 360):
        page.set_viewport_size({"width": width, "height": HEIGHT})
        found["fit"][str(width)] = page.evaluate(FIT_PROBE)
        shot(f"update_pl_{width}")
        page.emulate_media(color_scheme="dark")
        shot(f"update_pl_dark_{width}")
        page.emulate_media(color_scheme="light")
    page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
    page.check("#language-en")

    return found


def main(base: str, shot_dir: Path) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    report: dict[str, Any] = {"shots": []}
    shot_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            return {"skipped": f"cannot start chromium: {str(exc)[:300]}"}
        try:
            context = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                color_scheme="light",
                locale="en-US",
                timezone_id="UTC",
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.clock.install()  # fake time that flows normally, so that 45 s can be skipped
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on(
                "console", lambda m: console_errors.append(m.text) if m.type == "error" else None
            )
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            def shot(name: str) -> None:
                path = shot_dir / f"{name}.png"
                page.screenshot(path=str(path))
                report["shots"].append(str(path))

            report["found"] = run(page, context, base, shot)
            report["console_errors"] = console_errors
            report["page_errors"] = page_errors
        finally:
            browser.close()
    return report


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], Path(sys.argv[2]))))
