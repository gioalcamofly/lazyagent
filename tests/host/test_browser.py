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
    """The hosted TUI's terminal shows ``needle`` (at least ``count`` times)."""
    await page.wait_for_function(
        "([needle, count]) => { const b = window.lazyagent.tui.term.buffer.active;"
        " let text = ''; for (let i = 0; i < b.length; i++)"
        " text += b.getLine(i).translateToString(true) + '\\n';"
        " return text.split(needle).length > count; }",
        arg=[needle, count],
        timeout=20000,
    )


# The text of a native terminal (any renderer), by session id.
TERM_TEXT = """(id) => {
  const t = window.lazyagent.terminals.sessions.get(id);
  if (!t) return '';
  const b = t.term.buffer.active;
  let text = '';
  for (let i = 0; i < b.length; i++) text += b.getLine(i).translateToString(true) + '\\n';
  return text;
}"""


async def native_has(page, session_id: str, needle: str, timeout: float = 20000) -> None:
    await page.wait_for_function(
        "([id, needle]) => (" + TERM_TEXT + ")(id).includes(needle)",
        arg=[session_id, needle],
        timeout=timeout,
    )


async def shown_session(page) -> str:
    """The session of the agent tab on screen."""
    return await page.evaluate("() => window.lazyagent.store.shownAgent()?.session_id")


async def open_page(context, url):
    page = await context.new_page()
    problems = Problems()
    problems.watch(page)
    await page.goto(url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    return page, problems


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
    page, problems = await open_page(context, host.server.url)
    # The token leaves the address bar once read.
    assert "token" not in page.url
    await page.wait_for_function(
        "() => document.querySelectorAll('#worktrees li.wt').length === 3"
    )
    # The page shows its own (native) view; the hosted TUI renders nothing.
    await _terminal_has(page, "Shown in the browser")
    await page.wait_for_selector(".empty:has-text('No agent in repo')")

    await page.locator("li.wt", has_text="repo-feat").click()
    await page.wait_for_selector("li.wt.current:has-text('repo-feat')")
    feat = str(tmp_path / "repo-feat")

    # The worktree shell, native, in the bottom pane (Ctrl+L).
    await page.keyboard.press("Control+l")
    shell = await page.wait_for_function(
        "(path) => window.lazyagent.store.shells.value[path]", arg=feat
    )
    shell_id = await shell.json_value()
    await native_has(page, shell_id, "$")
    await page.keyboard.type("echo U2_SHELL_$((6*7))")
    await page.keyboard.press("Enter")
    await native_has(page, shell_id, "U2_SHELL_42")

    # Spawn from the page's own dialog; the TUI stays blank.
    await page.click("#toolbar button[data-action=spawn]")
    await page.wait_for_selector("#spawn-dialog")
    await page.click("#spawn-dialog button[type=submit]")
    tab = page.locator(".tabs .tab[data-agent]")
    await tab.wait_for()
    agent = await shown_session(page)
    await native_has(page, agent, "your turn>")
    assert "running" in await page.locator("li.wt.current li.agent").inner_text()
    assert await page.evaluate("() => window.lazyagent.store.centerView.value") == "native"

    # Typing claims the size: the agent sees the terminal's own size.
    await page.locator(".agent-slot").click()
    dims = await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).proposed()", agent
    )
    await page.keyboard.type("size")
    await page.keyboard.press("Enter")
    await native_has(page, agent, f"size {dims['rows']}x{dims['cols']}")

    # Hide and show (the TUI tab and back) keeps the screen and the stream.
    await page.click(".tui-tab")
    await page.wait_for_selector(".center.tui-shown")
    await page.click(".tabs .tab[data-agent]")
    await page.wait_for_selector(".center:not(.tui-shown)")
    # The TUI is blank again: whatever it claimed while in front is done.
    await page.wait_for_function(
        "() => { const b = window.lazyagent.tui.term.buffer.active; let t = '';"
        " for (let i = 0; i < b.length; i++) t += b.getLine(i).translateToString(true);"
        " return t.includes('Shown in the browser'); }",
        timeout=20000,
    )
    await native_has(page, agent, f"size {dims['rows']}x{dims['cols']}")
    assert await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).streamId !== null", agent
    )

    # Showing the TUI tab let the TUI claim the PTY (its view claims when
    # Textual lays it out, late under load): the native terminal takes the
    # size back, so a later "size" prints the terminal's own.
    marker = f"size {dims['rows']}x{dims['cols']}"
    count = "([id, needle]) => (" + TERM_TEXT + ")(id).split(needle).length - 1"
    before = await page.evaluate(count, [agent, marker])
    await page.locator(".agent-slot").click()
    for _ in range(5):
        await page.keyboard.type("size")
        await page.keyboard.press("Enter")
        try:
            await page.wait_for_function(
                f"(args) => ({count})(args) > {before}", arg=[agent, marker], timeout=3000
            )
            break
        except Exception:
            continue
    else:
        text = await page.evaluate(TERM_TEXT, agent)
        raise AssertionError(f"{marker!r} never came back: {text.strip()!r}")

    # A reload re-attaches with a snapshot of the screen.
    await page.reload()
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.locator("li.wt", has_text="repo-feat").click()
    await native_has(page, agent, marker)
    await native_has(page, agent, "your turn>")
    assert await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).mode", agent
    ) == "snapshot"

    # The agent's exit becomes a toast and a notification; its tab closes.
    await page.locator(".agent-slot").click()
    await page.keyboard.type("exit 3")
    await page.keyboard.press("Enter")
    toast = page.locator(".toast", has_text="exited (code 3)")
    await toast.wait_for()
    assert "(repo-feat)" in await toast.inner_text()
    notes = await page.evaluate("window.__notes")
    assert any("exited (code 3)" in note for note in notes)
    await page.wait_for_selector(".tabs .tab[data-agent]", state="detached")
    await page.wait_for_selector("li.agent", state="detached")

    # q in the TUI quits lazyagent: the page says so. The TUI tab makes the
    # TUI show feat (its keyboard goes to the shell there); Ctrl+K only once
    # that focus has landed, and q only once the TUI has left the shell,
    # else a late focus would send q to the shell.
    view = "(focus) => window.lazyagent.store.view.value?.focus === focus"
    await page.click(".tui-tab")
    await page.wait_for_function(view, arg="shell")
    await page.locator("#terminal").click()
    await page.keyboard.press("Control+k")
    await page.wait_for_function(view, arg="none")
    await page.keyboard.press("q")
    await page.wait_for_selector("#overlay:not([hidden])", timeout=20000)
    assert "lazyagent exited (code 0)" in await page.inner_text("#overlay")
    assert await asyncio.wait_for(host.task, 30) == 0
    assert list(problems) == []


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
    await page.click(".tui-tab")
    await page.locator("#terminal").click()
    await page.keyboard.press("?")
    await asyncio.sleep(0.3)
    assert await sheet.count() == 0
    await page.keyboard.press("Escape")
    await page.click(".tui-tab")
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
