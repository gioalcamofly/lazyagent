"""The attention queue and the PR/CI panel (U4), end to end: fake agents
whose status comes from Claude hook events (written to the hook log of
this host's own agents only), a reply that reaches the agent, items that
clear themselves, dismiss and snooze, the badge, notifications, and the PR
panel with a failing check sent back to an agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio

from lazyagent.engine.client import Client
from tests.browser import needs_playwright
from tests.contract.helpers import wait_for_text
from tests.host.test_browser import native_has, open_page
from tests.perf.procstat import cmdline, descendants

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


def _hook_logs(host) -> set[Path]:
    """The hook logs of the fake agents this host's TUI started."""
    logs = set()
    for pid in descendants(host.tui.pid):
        words = cmdline(pid).split()
        if any(w.endswith("fake_agent.py") for w in words) and "--settings" in words:
            settings = Path(words[words.index("--settings") + 1])
            logs.add(settings.parent / "hook-events.jsonl")
    return logs


async def _spawn(host, worktree: str, label: str) -> tuple[dict, Path]:
    """A fake agent with a label, and its hook log."""
    before = _hook_logs(host)
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        agent = await driver.request("spawn_agent", worktree_path=worktree, label=label)
        await wait_for_text(driver, agent, "your turn>")
    finally:
        await driver.close()
    (log,) = _hook_logs(host) - before
    return agent, log


def _hook(log: Path, **event) -> None:
    with log.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _ask(log: Path) -> None:
    _hook(log, hook_event_name="Notification", notification_type="elicitation_dialog")


def _approval(log: Path) -> None:
    _hook(log, hook_event_name="Notification", notification_type="permission_prompt")


def _resume(log: Path) -> None:
    _hook(log, hook_event_name="UserPromptSubmit")


@pytest_asyncio.fixture
async def page_ctx(browser):
    context = await browser.new_context(viewport={"width": 1400, "height": 860})
    await context.grant_permissions(["notifications"])
    await context.add_init_script(
        "window.__notes = [];"
        "window.Notification = class { static permission = 'granted';"
        " constructor(title, options) { window.__notes.push(options.body); } };"
        "document.hasFocus = () => false;"
    )
    return context


async def test_the_queue_answers_and_clears(fake_claude, host, page_ctx, tmp_path):
    feat = str(tmp_path / "repo-feat")
    page, problems = await open_page(page_ctx, host.server.url)
    asker, asker_log = await _spawn(host, feat, "asker")
    gate, gate_log = await _spawn(host, feat, "gate")

    _ask(asker_log)
    _approval(gate_log)
    items = page.locator("#attention-list li.att")
    await page.click("#view-attention")
    await page.wait_for_function("() => document.querySelectorAll('#attention-list li.att').length === 2")
    # Most urgent first: the approval, then the question.
    assert await items.nth(0).get_attribute("data-kind") == "approval"
    assert await items.nth(1).get_attribute("data-kind") == "question"
    assert await page.title() == "(2) lazyagent — repo"
    assert await page.inner_text("#view-attention .count") == "2"
    assert await page.locator('link[rel="icon"]').get_attribute("href") != "favicon.svg"
    notes = await page.evaluate("window.__notes")
    assert any("gate (repo-feat) needs approval" in n for n in notes)
    assert any("asker (repo-feat) is waiting for input" in n for n in notes)

    # The question: its live terminal, and a reply that reaches the agent.
    await items.nth(1).click()
    await page.wait_for_function(
        "(id) => window.lazyagent.terminals.sessions.get(id)?.visible", arg=asker["session_id"]
    )
    await native_has(page, asker["session_id"], "your turn>")
    await page.fill(".reply textarea", "yes, use the second option")
    await page.locator(".reply textarea").press("Control+Enter")
    await native_has(page, asker["session_id"], "got: 'yes, use the second option'")
    # The agent goes back to work: its item clears by itself.
    _resume(asker_log)
    await page.wait_for_function("() => document.querySelectorAll('#attention-list li.att').length === 1")
    assert await page.title() == "(1) lazyagent — repo"

    # Dismiss is per page and survives a reload; the same condition coming
    # back later is a new item.
    await page.click(".att-pane .dismiss")
    await page.wait_for_selector("#attention-list .queue-empty")
    assert await page.title() == "lazyagent — repo"
    await page.reload()
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.click("#view-attention")
    await page.wait_for_selector("#attention-list .queue-empty")
    _resume(gate_log)
    await page.wait_for_function(
        "(id) => window.lazyagent.store.agentsOf(id[0]).find(a => a.agent_id === id[1])?.status === 'running'",
        arg=[feat, gate["agent_id"]],
    )
    _approval(gate_log)
    await page.wait_for_selector("#attention-list li.att[data-kind=approval]")

    # Snooze hides it (15 min).
    await page.click(".att-pane button[title^='Hide']")
    await page.wait_for_selector("#attention-list .queue-empty")
    assert list(problems) == []


async def test_the_pr_panel_and_a_failing_check(fake_claude, host, page_ctx, tmp_path):
    feat = str(tmp_path / "repo-feat")
    page, problems = await open_page(page_ctx, host.server.url)
    fixer, _ = await _spawn(host, feat, "fixer")
    await page.locator("li.wt", has_text="repo-feat").click()

    # No gh in the test environment: the panel says so.
    await page.keyboard.press("Control+.")
    panel = page.locator("#pr-panel")
    await panel.wait_for()
    await page.wait_for_function(
        "() => /GitHub CLI not available|No PR/.test(document.getElementById('pr-panel').innerText)",
        timeout=30000,
    )

    # A PR whose CI goes red (the event as the engine sends it).
    pr = {
        "number": 12,
        "title": "Fix the parser",
        "state": "OPEN",
        "url": "https://github.com/o/r/pull/12",
        "review_decision": "REVIEW_REQUIRED",
        "mergeable": "MERGEABLE",
        "checks": [
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS", "details_url": "https://ci/lint"},
            {"name": "tests", "status": "IN_PROGRESS", "conclusion": "", "details_url": ""},
        ],
    }
    apply = "(e) => { window.lazyagent.store.applyEvent('pr_info.updated', e); }"
    await page.evaluate(apply, {"path": feat, "pr_info": pr})
    await panel.locator(".pr-summary", has_text="Fix the parser").wait_for()
    assert await panel.locator("a[href='https://ci/lint']").count() == 1
    assert await page.locator("li.wt.current .ci.pending").count() == 1
    red = {**pr, "checks": [pr["checks"][0], {**pr["checks"][1], "status": "COMPLETED", "conclusion": "FAILURE", "details_url": "https://ci/tests"}]}
    await page.evaluate(apply, {"path": feat, "pr_info": red})
    await page.locator(".toast", has_text="CI failed on #12").wait_for()
    assert any("CI failed on #12" in n for n in await page.evaluate("window.__notes"))
    assert await panel.locator(".check.fail").count() == 1

    # The queue has it; its button sends the failures to the worktree's agent.
    await page.click("#view-attention")
    item = page.locator("#attention-list li.att[data-kind=ci]")
    await item.click()
    await page.click(".att-pr button.primary")
    await native_has(page, fixer["session_id"], "CI is failing on PR #12")
    assert list(problems) == []


async def test_notification_settings(host, page_ctx):
    page, problems = await open_page(page_ctx, host.server.url)
    await page.click("#settings-button")
    box = page.locator("#settings input[data-kind=done]")
    assert await box.is_checked()
    await box.uncheck()
    await page.reload()
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.click("#settings-button")
    assert not await page.locator("#settings input[data-kind=done]").is_checked()
    store = "window.lazyagent.store"
    assert await page.evaluate(f"() => [{store}.notifies('done'), {store}.notifies('approval')]") == [False, True]
    assert list(problems) == []
