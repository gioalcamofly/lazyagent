"""Browser smoke test of the host page (Playwright, headless Chromium).

Local only (marker ``e2e``): skipped unless Playwright and a Chromium it
can launch are available (``uv sync --group dev --group e2e``; it falls back
to the installed Google Chrome when Playwright's own browser is not
downloaded). tests/browser.py has the fixtures.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.browser import Problems, needs_playwright

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


async def _terminal_has(page, needle: str, count: int = 1) -> None:
    await page.wait_for_function(
        "([needle, count]) => document.querySelector('#terminal .xterm-rows')"
        ".innerText.split(needle).length > count",
        arg=[needle, count],
        timeout=20000,
    )


async def test_page_in_a_real_browser(fake_claude, host, browser, tmp_path):
    context = await browser.new_context(viewport={"width": 1400, "height": 820})
    await context.grant_permissions(["notifications"])
    # Record notifications, and look unfocused so the page sends them.
    await context.add_init_script(
        "window.__notes = [];"
        "window.Notification = class { static permission = 'granted';"
        " constructor(title, options) { window.__notes.push(options.body); } };"
        "document.hasFocus = () => false;"
    )
    page = await context.new_page()
    problems = Problems()
    problems.watch(page)

    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    # The token leaves the address bar once read.
    assert "token" not in page.url
    await page.wait_for_function(
        "() => document.querySelectorAll('#worktrees li.wt').length === 3"
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
    await page.keyboard.press("n")
    await page.wait_for_selector("#toolbar button[data-action=spawn]:enabled")

    # The agent shows up in the sidebar, and its exit becomes a toast.
    agent = page.locator("li.wt.current li.agent")
    await agent.wait_for()
    assert "running" in await agent.inner_text()
    await _terminal_has(page, "your turn>")
    await page.keyboard.type("exit 3")
    await page.keyboard.press("Enter")
    toast = page.locator(".toast", has_text="exited (code 3)")
    await toast.wait_for()
    assert "(repo-feat)" in await toast.inner_text()
    notes = await page.evaluate("window.__notes")
    assert any("exited (code 3)" in note for note in notes)
    await page.wait_for_selector("li.agent", state="detached")

    await page.locator("#terminal").click()
    await page.keyboard.press("Control+k")
    await asyncio.sleep(0.3)
    await page.keyboard.press("q")
    await page.wait_for_selector("#overlay:not([hidden])", timeout=20000)
    assert "lazyagent exited (code 0)" in await page.inner_text("#overlay")
    assert await asyncio.wait_for(host.task, 30) == 0
    assert problems == []


async def test_theme_and_help_sheet(host, browser):
    from lazyagent.app import LazyAgent

    from tests.host.test_page_js import _binding_keys

    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    problems = Problems()
    problems.watch(page)
    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    root = page.locator("html")
    assert await root.get_attribute("data-theme") == "dark"

    # ? outside the terminal opens the sheet; it lists every TUI binding.
    await page.locator("#engine-state").click()
    await page.keyboard.press("?")
    sheet = page.locator("#help")
    await sheet.wait_for()
    listed = set(" ".join(await sheet.locator("tr[data-tui-key]").evaluate_all(
        "rows => rows.map(r => r.dataset.tuiKey)"
    )).split())
    assert _binding_keys(LazyAgent.BINDINGS) <= listed
    await page.keyboard.press("Escape")
    await sheet.wait_for(state="detached")
    # ? typed into the TUI goes to the TUI, not to the page.
    await page.locator("#terminal").click()
    await page.keyboard.press("?")
    await asyncio.sleep(0.3)
    assert await sheet.count() == 0
    await page.keyboard.press("Escape")
    # The top bar button opens it too.
    await page.click("#help-button")
    await sheet.wait_for()
    await page.click("#help .close")
    await sheet.wait_for(state="detached")

    background = "getComputedStyle(document.body).backgroundColor"
    dark = await page.evaluate(background)
    await page.click("#theme")
    assert await root.get_attribute("data-theme") == "light"
    assert await page.evaluate(background) == "rgb(246, 246, 246)" != dark
    # The hosted TUI paints its own dark palette: its terminal stays dark.
    assert await page.evaluate("window.lazyagent.tui.term.options.theme.background") == "#121212"
    # Kept across a reload (the token is in sessionStorage).
    await page.reload()
    await page.wait_for_selector("#conn.ok", timeout=30000)
    assert await root.get_attribute("data-theme") == "light"
    await page.click("#theme")  # system: headless Chrome reports dark
    assert await page.inner_text("#theme") == "System"
    await page.click("#theme")
    assert await root.get_attribute("data-theme") == "dark"
    await page.close()
    assert list(problems) == []
