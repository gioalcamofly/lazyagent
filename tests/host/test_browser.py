"""Browser smoke test of the host page (Playwright, headless Chromium).

Skipped unless Playwright and a Chromium it can launch are available; run it
with ``uv run --with playwright pytest tests/host/test_browser.py`` (it
falls back to the installed Google Chrome when Playwright's own browser is
not downloaded).
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

playwright = pytest.importorskip("playwright.async_api")

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def browser():
    async with playwright.async_playwright() as p:
        launched = None
        for options in ({}, {"channel": "chrome"}):
            try:
                launched = await p.chromium.launch(**options)
                break
            except playwright.Error:
                continue
        if launched is None:
            pytest.skip("no Chromium Playwright can launch")
        try:
            yield launched
        finally:
            await launched.close()


async def _terminal_has(page, needle: str, count: int = 1) -> None:
    await page.wait_for_function(
        "([needle, count]) => document.querySelector('#terminal .xterm-rows')"
        ".innerText.split(needle).length > count",
        arg=[needle, count],
        timeout=20000,
    )


async def test_page_in_a_real_browser(host, browser, tmp_path):
    page = await browser.new_page(viewport={"width": 1400, "height": 820})
    problems = []
    page.on(
        "console",
        lambda m: problems.append(m.text) if m.type in ("error", "warning") else None,
    )
    page.on("pageerror", lambda e: problems.append(str(e)))

    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    # The token leaves the address bar once read.
    assert "token" not in page.url
    await page.wait_for_function(
        "document.querySelectorAll('#worktrees li.wt').length === 3"
    )
    await _terminal_has(page, "Select a worktree")

    await page.locator("li.wt", has_text="repo-feat").click()
    await page.wait_for_selector("li.wt.current:has-text('repo-feat')")

    await page.locator("#terminal").click()
    await page.keyboard.type("echo H2_BROWSER_OK")
    await page.keyboard.press("Enter")
    await _terminal_has(page, "H2_BROWSER_OK", 2)

    await page.click("#toolbar button[data-action=spawn]")
    await page.wait_for_selector("#toolbar button[data-action=spawn]:disabled")
    await _terminal_has(page, "Spawn agent in feat")
    await page.keyboard.press("Escape")
    await page.wait_for_selector("#toolbar button[data-action=spawn]:enabled")

    await page.locator("#terminal").click()
    await page.keyboard.press("Control+k")
    await asyncio.sleep(0.3)
    await page.keyboard.press("q")
    await page.wait_for_selector("#overlay:not([hidden])", timeout=20000)
    assert "lazyagent exited (code 0)" in await page.inner_text("#overlay")
    assert await asyncio.wait_for(host.task, 30) == 0
    assert problems == []
