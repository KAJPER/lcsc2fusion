"""Browser driver for tests/test_addin_http_bridge.py.

It needs Playwright, which is not a dependency of the project, so the test runs this file with
whichever Python on the machine has it. Usage: python http_bridge_browser.py BASE_URL SHOT_DIR

BASE_URL is the fixture server of the test (serves the palette files and fake /api endpoints).
The driver walks one page through a normal session, a lost connection with recovery and a
shutdown, takes screenshots at 520 px width (light and dark) and prints ONE JSON report line.
It prints {"skipped": reason} when no browser can be started.
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
HEIGHT = 780
INIT_SCRIPT = (
    "window.__closeCalls = 0;"
    "window.close = function () { window.__closeCalls += 1; };"  # headless pages cannot close
)
BRIDGE_STATE = "window.__lcscHttpBridge.state()"


def control(base: str, path: str, payload: dict[str, Any] | None = None) -> None:
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def poll(check: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    """Waits for `check`. page.wait_for_function is no option: it evaluates its predicate with
    eval after the first poll, which the CSP of the page (default-src 'self') refuses."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.05)


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
                viewport={"width": WIDTH, "height": HEIGHT}, color_scheme="light"
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.add_init_script(INIT_SCRIPT)
            console_errors: list[str] = []
            page_errors: list[str] = []

            def on_console(message: Any) -> None:
                if message.type == "error":
                    console_errors.append(message.text)

            page.on("console", on_console)
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            def shot(name: str) -> None:
                path = shot_dir / f"{name}.png"
                page.screenshot(path=str(path))
                report["shots"].append(str(path))

            def wait_state(state: str) -> None:
                poll(lambda: page.evaluate(BRIDGE_STATE) == state, f"bridge state {state}")

            def wait_status(text: str) -> None:
                poll(lambda: text in page.inner_text("#status-text"), f"status {text!r}")

            # Fake time, flowing normally, so that the 15 s and 3 s paths can be fast forwarded.
            page.clock.install()

            # 1) a normal session: ready, status, search, add. The stream is accepted late, and
            # a fresh stream does not replay: only a first request that waits for the stream
            # gets its answer.
            report["stream_delay_used"] = 0.5
            control(base, "/__test/delay", {"seconds": report["stream_delay_used"]})
            page.goto(base + "/")
            wait_status("Ready (fixture)")
            report["initial_status"] = page.inner_text("#status-text")
            control(base, "/__test/delay", {"seconds": 0})
            report["mock_banner_hidden"] = page.locator("#mock-banner").is_hidden()
            report["conn_banner_hidden_at_start"] = page.locator("#conn-banner").is_hidden()
            report["state_at_start"] = page.evaluate(BRIDGE_STATE)

            page.fill("#query", "C2040")
            page.click("#search-button")
            page.wait_for_selector(".result-row")
            report["result_rows"] = page.locator(".result-row").all_inner_texts()
            wait_status("Found 1 part")
            report["status_after_search"] = page.inner_text("#status-text")

            page.click("button[data-add]")
            page.wait_for_selector(".added-item")
            report["added_items_before"] = page.locator(".added-item").count()
            shot("session_light")

            # 2) the connection is lost: the server drops the streams and refuses new ones
            control(base, "/__test/drop")
            wait_state("broken")
            page.clock.run_for(14000)
            report["conn_banner_hidden_before_15s"] = page.locator("#conn-banner").is_hidden()
            page.clock.run_for(2000)
            page.wait_for_selector("#conn-banner:not([hidden])")
            report["lost_banner_text"] = page.inner_text("#conn-banner")
            report["lost_banner_role"] = page.get_attribute("#conn-banner", "role")
            report["lost_banner_live"] = page.get_attribute("#conn-banner", "aria-live")
            report["state_when_lost"] = page.evaluate(BRIDGE_STATE)
            shot("lost_light")
            page.emulate_media(color_scheme="dark")
            shot("lost_dark")
            page.emulate_media(color_scheme="light")

            # 3) the server is back: a fresh EventSource (no replay), the page says `ready` again
            page.evaluate("document.getElementById('status-text').textContent = 'stale'")
            control(base, "/__test/restore")
            page.clock.run_for(3500)
            wait_state("open")
            page.wait_for_selector("#conn-banner", state="hidden")
            wait_status("Ready (fixture)")
            report["status_after_recovery"] = page.inner_text("#status-text")
            report["state_after_recovery"] = page.evaluate(BRIDGE_STATE)
            report["added_items_after"] = page.locator(".added-item").count()
            report["result_rows_after"] = page.locator(".result-row").count()
            report["close_calls_before_shutdown"] = page.evaluate("window.__closeCalls")

            # 4) Fusion stops the add-in
            control(base, "/__test/publish", {"action": "shutdown", "data": "bye"})
            page.wait_for_selector("#conn-banner:not([hidden])")
            report["shutdown_banner_text"] = page.inner_text("#conn-banner")
            report["state_after_shutdown"] = page.evaluate(BRIDGE_STATE)
            report["close_calls_after_shutdown"] = page.evaluate("window.__closeCalls")
            shot("stopped_light")
            page.emulate_media(color_scheme="dark")
            shot("stopped_dark")

            report["console_errors"] = console_errors
            report["page_errors"] = page_errors
        finally:
            browser.close()
    return report


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], Path(sys.argv[2]))))
