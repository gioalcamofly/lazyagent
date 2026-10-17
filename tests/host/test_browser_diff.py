"""The diff viewer (U5), end to end against a real host: a worktree's diff
from its base branch, unified and split, the file list, comments kept in
the page, and a review that reaches the agent (now, or once it is idle)."""

from __future__ import annotations

import subprocess
import time

import pytest

from lazyagent.engine.client import Client

from tests.browser import needs_playwright
from tests.contract.helpers import screen_lines
from tests.host.test_browser import native_has, open_page, shown_session
from tests.host.test_browser_attention import _ask, _resume, _spawn

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


def _change(feat) -> None:
    """A commit on the branch, then edits in the working tree."""
    (feat / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(feat, "add", ".")
    _git(feat, "commit", "-qm", "calc")
    (feat / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    (feat / "notes.md").write_text("# notes\nreview me\n")


async def _comment(page, line: str, text: str) -> None:
    await page.locator(".drow", has_text=line).locator("button.ln").last.click()
    editor = page.locator("#comment-editor")
    await editor.locator("textarea").fill(text)
    await editor.locator("textarea").press("Control+Enter")
    await editor.wait_for(state="detached")


async def test_a_review_reaches_the_agent(fake_claude, host, browser, tmp_path):
    feat = tmp_path / "repo-feat"
    _change(feat)
    page, problems = await open_page(await browser.new_context(viewport={"width": 1400, "height": 860}), host.server.url)
    await page.locator("li.wt", has_text="repo-feat").click()

    # Spawned from the dialog: it closes as soon as the agent is listed,
    # even when the agent says nothing more after its prompt.
    await page.locator("#engine-state").click()
    await page.keyboard.press("s")
    dialog = page.locator("#spawn-dialog")
    await dialog.wait_for()
    assert await page.evaluate("() => document.activeElement.name") == "instruction"
    started = time.monotonic()
    await page.keyboard.press("Control+Enter")
    await dialog.wait_for(state="detached", timeout=20000)
    assert time.monotonic() - started < 5
    session = await shown_session(page)
    await native_has(page, session, "your turn>")

    # Ctrl+D from the agent's terminal: the diff against the base branch,
    # the commit and the working tree together.
    await page.keyboard.press("Control+d")
    await page.wait_for_selector(".diff-rows .drow")
    assert await page.input_value(".diff-head input.base") == "main"
    files = page.locator(".diff-files .dfile")
    assert await files.evaluate_all("(els) => els.map((e) => e.dataset.path)") == ["calc.py", "notes.md"]
    assert await page.locator(".drow.add", has_text="def mul(a, b):").count() == 1

    # Comments: one on an added line, one on the new file; kept in the page.
    await _comment(page, "return a * b", "use operator.mul")
    await files.nth(1).click()
    await _comment(page, "review me", "drop this file")
    assert await page.locator(".drow.comment").count() == 2
    assert "2 comments" in await page.inner_text(".diff-head")

    # Split view; the layout and the comments survive a reload.
    await page.get_by_role("button", name="Split").click()
    pair = page.locator(".drow.split", has_text="def mul(a, b):")
    halves = pair.locator(".half")
    assert await halves.nth(0).get_attribute("class") == "half none"
    assert "def mul" in await halves.nth(1).inner_text()
    await page.reload()
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.locator("li.wt", has_text="repo-feat").click()
    await page.locator("#engine-state").click()
    await page.keyboard.press("Control+d")
    await page.wait_for_selector(".diff-rows .drow.split")
    assert await page.locator(".drow.comment").count() == 2
    assert "2 comments" in await page.inner_text(".diff-head")

    # Submit: the agent gets one prompt with both comments.
    await page.click("#submit-review")
    review = page.locator("#review-dialog")
    await review.wait_for()
    assert "use operator.mul" in await review.locator(".prompt-preview").inner_text()
    await review.locator("button.primary").click()
    await review.wait_for(state="detached")
    await native_has(page, session, "got: 'Review comments on your changes.")
    await native_has(page, session, "notes.md:2")
    assert await page.evaluate("() => window.lazyagent.store.comments.value") == {str(feat): []}
    assert list(problems) == []


async def test_send_when_idle(fake_claude, host, browser, tmp_path):
    feat = tmp_path / "repo-feat"
    _change(feat)
    worker, log = await _spawn(host, str(feat), "worker")
    _resume(log)
    page, problems = await open_page(await browser.new_context(), host.server.url)
    await page.locator("li.wt", has_text="repo-feat").click()
    await page.wait_for_function(
        "(id) => window.lazyagent.store.agentsOf(id[0]).find(a => a.agent_id === id[1])?.status === 'running'",
        arg=[str(feat), worker["agent_id"]],
    )
    await page.locator("#engine-state").click()
    await page.keyboard.press("Control+d")
    await page.wait_for_selector(".diff-rows .drow")
    await _comment(page, "return a * b", "later is fine")

    # The agent is busy: the review waits for it.
    await page.click("#submit-review")
    review = page.locator("#review-dialog")
    assert "worker is running" in await review.inner_text()
    await review.get_by_role("button", name="Send when idle").click()
    await review.wait_for(state="detached")
    await page.wait_for_timeout(500)
    assert not any("later is fine" in line for line in await _screen(host, worker))
    _ask(log)
    await page.wait_for_function("() => Object.keys(window.lazyagent.store.pendingReviews.value).length === 0")
    deadline = time.monotonic() + 10
    while not any("later is fine" in line for line in await _screen(host, worker)):
        assert time.monotonic() < deadline, "the queued review never arrived"
        await page.wait_for_timeout(100)
    assert list(problems) == []


async def _screen(host, agent) -> list[str]:
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        return await screen_lines(driver, agent)
    finally:
        await driver.close()


async def test_the_base_and_the_working_tree(host, browser, tmp_path):
    feat = tmp_path / "repo-feat"
    _change(feat)
    page, problems = await open_page(await browser.new_context(), host.server.url)
    await page.locator("li.wt", has_text="repo-feat").click()
    await page.click("#toolbar button:has-text('Diff')")
    await page.wait_for_selector(".diff-rows .drow")
    base = page.locator(".diff-head input.base")

    # No base: only what is not committed yet.
    await base.fill("")
    await base.press("Enter")
    await page.wait_for_function("() => window.lazyagent.store.diff.value?.base === '' && window.lazyagent.store.diff.value.parsed")
    assert await page.locator(".diff-files .dfile").count() == 2
    assert await page.locator(".drow", has_text="def add(a, b):").count() == 1  # context, not an addition
    assert await page.locator(".drow.add", has_text="def add(a, b):").count() == 0

    # A base that does not exist says so.
    await base.fill("no-such-branch")
    await base.press("Enter")
    await page.locator(".diff-view .form-error").wait_for()

    # The main worktree diffs its working tree; no changes says so.
    await page.locator("li.wt", has_text="main").first.click()
    await page.locator(".diff-view .empty", has_text="No changes").wait_for()
    assert await page.input_value(".diff-head input.base") == ""
    assert await page.locator("#submit-review").is_disabled()
    assert list(problems) == []
