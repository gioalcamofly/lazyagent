"""Native terminals in the page (U2) beyond the main walk-through in
test_browser.py: a dropped WebSocket resumes every terminal with a tail, and
the DOM renderer stands in for WebGL."""

from __future__ import annotations

import pytest

from lazyagent.engine.client import Client
from tests.browser import needs_playwright
from tests.contract.helpers import wait_for_text
from tests.host.test_browser import native_has, open_page

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


async def _agent(host, worktree: str) -> dict:
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        agent = await driver.request("spawn_agent", worktree_path=worktree)
        await wait_for_text(driver, agent, "your turn>")
        return agent
    finally:
        await driver.close()


async def _shown(page, worktree: str, agent: dict) -> None:
    await page.locator("li.wt", has_text="repo-feat").click()
    await native_has(page, agent["session_id"], "your turn>")


async def test_a_dropped_connection_resumes_with_a_tail(fake_claude, host, browser, tmp_path):
    feat = str(tmp_path / "repo-feat")
    page, problems = await open_page(await browser.new_context(), host.server.url)
    agent = await _agent(host, feat)
    await _shown(page, feat, agent)
    session = agent["session_id"]
    await page.locator(".agent-slot").click()
    await page.keyboard.type("before")
    await page.keyboard.press("Enter")
    await native_has(page, session, "got: 'before'")

    await page.evaluate("() => window.lazyagent.conn.socket.close()")
    await page.wait_for_selector("#conn:has-text('reconnecting')")
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.wait_for_function(
        "(id) => window.lazyagent.terminals.sessions.get(id).streamId !== null", arg=session
    )
    assert await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).mode", session
    ) == "tail"
    await page.locator(".agent-slot").click()
    await page.keyboard.type("after")
    await page.keyboard.press("Enter")
    await native_has(page, session, "got: 'after'")
    assert await page.locator("#overlay").is_hidden()
    # The engine dropped the old connection's streams: the page reports
    # nothing wrong.
    assert list(problems) == []


async def test_the_dom_renderer_stands_in_for_webgl(fake_claude, host, browser, tmp_path):
    feat = str(tmp_path / "repo-feat")
    context = await browser.new_context()
    page, problems = await open_page(context, host.server.url)
    await page.evaluate("() => { window.lazyagent.terminals.forceDom = true; }")
    agent = await _agent(host, feat)
    await _shown(page, feat, agent)
    session = agent["session_id"]
    assert await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).renderer", session
    ) == "dom"
    rows = page.locator(f".native-term[data-session='{session}'] .xterm-rows")
    await rows.wait_for()
    assert "your turn>" in await rows.inner_text()
    assert list(problems) == []


async def test_keys_move_between_sidebar_tabs_and_shell(fake_claude, host, browser, tmp_path):
    feat = str(tmp_path / "repo-feat")
    page, problems = await open_page(await browser.new_context(), host.server.url)
    first = await _agent(host, feat)
    second = await _agent(host, feat)
    await _shown(page, feat, second)
    focused = "() => document.activeElement?.closest('.native-term')?.dataset.session ?? null"
    shown = "() => window.lazyagent.store.shownAgent()?.session_id"

    await page.locator(".agent-slot").click()
    await page.wait_for_function(f"() => ({focused})() === '{second['session_id']}'")
    # Alt+N / Alt+P cycle the agent tabs and keep the keyboard in them.
    await page.keyboard.press("Alt+n")
    await page.wait_for_function(f"() => ({shown})() === '{first['session_id']}'")
    await page.wait_for_function(f"() => ({focused})() === '{first['session_id']}'")
    await page.keyboard.press("Alt+p")
    await page.wait_for_function(f"() => ({shown})() === '{second['session_id']}'")

    # Ctrl+K: the sidebar; k / j move the selection; Enter: back to the agent.
    await page.keyboard.press("Control+k")
    await page.wait_for_function("() => document.activeElement?.matches('li.wt.current')")
    await page.keyboard.press("k")
    await page.wait_for_function(
        f"() => window.lazyagent.store.selected.value !== {feat!r}"
        " && document.activeElement?.matches('li.wt.current')"
    )
    await page.keyboard.press("j")
    await page.wait_for_function(f"() => window.lazyagent.store.selected.value === {feat!r}")
    await page.keyboard.press("Enter")
    await page.wait_for_function(f"() => ({focused})() === '{second['session_id']}'")

    # Ctrl+L opens the shell and gives it the keyboard; Ctrl+J goes back.
    await page.keyboard.press("Control+l")
    shell = await (
        await page.wait_for_function(
            "(path) => window.lazyagent.store.shells.value[path]", arg=feat
        )
    ).json_value()
    await page.wait_for_function(f"() => ({focused})() === '{shell}'")
    await page.keyboard.press("Control+j")
    await page.wait_for_function(f"() => ({focused})() === '{second['session_id']}'")
    assert list(problems) == []


async def test_a_foreign_resize_is_taken_back_once(fake_claude, host, browser, tmp_path):
    """Another client (here a bare v1 client standing in for the hosted
    TUI) takes the PTY's size while the page's terminal has the keyboard:
    the page claims it back once, and not again within a second, so two
    clients that insist cannot SIGWINCH the agent in a loop."""
    import asyncio

    feat = str(tmp_path / "repo-feat")
    page, problems = await open_page(await browser.new_context(), host.server.url)
    agent = await _agent(host, feat)
    await _shown(page, feat, agent)
    session = agent["session_id"]
    await page.locator(".agent-slot").click()
    mine = await page.evaluate(
        "(id) => window.lazyagent.terminals.sessions.get(id).proposed()", session
    )
    await page.wait_for_function(
        "([id, r, c]) => { const p = window.lazyagent.terminals.sessions.get(id).pty;"
        " return p.rows === r && p.cols === c; }",
        arg=[session, mine["rows"], mine["cols"]],
    )
    other = await Client.connect(host.server.engine_socket, client="cli")
    try:
        sizes = []
        await other.attach(session, on_event=lambda e: sizes.append((e.data["rows"], e.data["cols"])))
        await other.request("claim_size", session_id=session, rows=20, cols=70)
        await asyncio.sleep(0.3)
        await other.request("claim_size", session_id=session, rows=21, cols=71)
        await asyncio.sleep(1.2)
        # Ours back once; the second foreign claim, inside the second, stays.
        assert sizes == [(20, 70), (mine["rows"], mine["cols"]), (21, 71)]
        sizes.clear()
        await asyncio.sleep(1.0)
        assert sizes == []
    finally:
        await other.close()
    assert list(problems) == []
