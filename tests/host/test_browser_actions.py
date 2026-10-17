"""The page's own actions (U3), end to end against a real host: create a
worktree and launch an agent with an instruction, a custom create shown in
the job drawer, spawn with the TUI's accelerators, stop, remove with its
guards."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.browser import needs_playwright
from tests.host.test_browser import native_has, open_page, shown_session

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


def _agent_cmdlines() -> list[str]:
    """Command lines of the fake agents running now."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            line = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "fake_agent.py" in line and "/bin/sh" not in line:
            out.append(line)
    return out


async def _outside(page) -> None:
    """Put the keyboard outside any terminal (the sidebar's footer)."""
    await page.locator("#engine-state").click()


async def test_create_and_launch_with_an_instruction(fake_claude, host, browser, tmp_path):
    page, problems = await open_page(await browser.new_context(), host.server.url)
    await _outside(page)
    await page.keyboard.press("c")
    dialog = page.locator("#create-dialog")
    await dialog.wait_for()
    # The base is the main worktree's branch (the built-in default is not).
    assert await dialog.locator("input[name=base]").input_value() == "main"
    await dialog.locator("input[name=branch]").fill("u3-feat")
    await dialog.locator("input[name=launch]").check()
    await dialog.locator("textarea[name=instruction]").fill("U3INSTR fix the thing")
    await dialog.locator("textarea[name=instruction]").press("Control+Enter")
    await dialog.wait_for(state="detached", timeout=20000)

    created = page.locator("li.wt.current", has_text="u3-feat")
    await created.wait_for()
    agent = None
    for _ in range(100):
        agent = await shown_session(page)
        if agent:
            break
        await page.wait_for_timeout(100)
    assert agent
    await native_has(page, agent, "your turn>")
    # The agent was started with the instruction.
    assert any("U3INSTR fix the thing" in line for line in _agent_cmdlines())
    branches = subprocess.run(
        ["git", "branch", "--list", "u3-feat"],
        cwd=tmp_path / "repo",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "u3-feat" in branches
    assert list(problems) == []


async def test_spawn_accelerators_stop_and_remove(fake_claude, host, browser, tmp_path):
    page, problems = await open_page(await browser.new_context(), host.server.url)
    feat = str(tmp_path / "repo-feat")
    await page.locator("li.wt", has_text="repo-feat").click()
    await _outside(page)

    # s, then d (skip permissions) from outside the text fields.
    await page.keyboard.press("s")
    dialog = page.locator("#spawn-dialog")
    await dialog.wait_for()
    await dialog.locator("input[name=label]").fill("reviewer")
    await dialog.locator("fieldset").first.click()
    await page.keyboard.press("d")
    await dialog.wait_for(state="detached", timeout=20000)
    tab = page.locator(".tabs .tab[data-agent]", has_text="reviewer")
    await tab.wait_for()
    agent = await shown_session(page)
    await native_has(page, agent, "your turn>")
    assert any("--dangerously-skip-permissions" in line for line in _agent_cmdlines())

    # Remove is guarded while the agent runs.
    await _outside(page)
    await page.keyboard.press("d")
    remove = page.locator("#remove-dialog")
    await remove.wait_for()
    assert "stop it first" in await remove.inner_text()
    assert await remove.locator("button.danger").is_disabled()
    await page.keyboard.press("Escape")
    await remove.wait_for(state="detached")

    # x stops it after a confirm (y).
    await _outside(page)
    await page.keyboard.press("x")
    stop = page.locator("#stop-dialog")
    await stop.wait_for()
    assert "reviewer" in await stop.inner_text()
    await page.keyboard.press("y")
    await tab.wait_for(state="detached", timeout=20000)

    # Now remove works; f forces it past an uncommitted file.
    (Path(feat) / "scratch.txt").write_text("dirty\n")
    await _outside(page)
    await page.keyboard.press("d")
    await remove.wait_for()
    await page.keyboard.press("y")
    error = remove.locator(".form-error")
    await error.wait_for(timeout=20000)
    await page.keyboard.press("f")
    assert await remove.locator("input[name=force]").is_checked()
    await page.keyboard.press("y")
    await remove.wait_for(state="detached", timeout=20000)
    await page.locator("li.wt", has_text="repo-feat").wait_for(state="detached")
    assert not Path(feat).exists()
    # The selection fell back to the main worktree.
    assert await page.evaluate("() => window.lazyagent.store.selected.value") == str(
        tmp_path / "repo"
    )
    assert list(problems) == []


async def test_the_main_worktree_cannot_be_removed(host, browser, tmp_path):
    page, problems = await open_page(await browser.new_context(), host.server.url)
    await page.locator("li.wt", has_text="repo").first.click()
    assert await page.locator("#toolbar button[data-action=remove]").is_disabled()
    await _outside(page)
    await page.keyboard.press("d")
    remove = page.locator("#remove-dialog")
    await remove.wait_for()
    assert "main worktree cannot be removed" in await remove.inner_text()
    await page.keyboard.press("y")
    await page.wait_for_timeout(300)
    assert (tmp_path / "repo").exists()
    assert list(problems) == []


@pytest.fixture
def custom_create(repo):
    """A custom create command (it runs as an engine job) that prints, and
    fails for a branch called "nope"."""
    (repo / ".lazyagent.toml").write_text(
        "[worktree]\n"
        "create = \"test {branch} != nope && echo U3_JOB {branch} {extra}"
        " && git worktree add -q {path} -b {branch} {base} && echo U3_DONE\"\n"
    )


async def test_a_custom_create_shows_its_job(custom_create, fake_claude, host, browser, tmp_path):
    page, problems = await open_page(await browser.new_context(), host.server.url)
    await _outside(page)
    await page.keyboard.press("c")
    dialog = page.locator("#create-dialog")
    await dialog.wait_for()
    await dialog.locator("input[name=branch]").fill("u3-job")
    await dialog.locator("input[name=extra]").fill("--with-extra")
    await dialog.locator("button[type=submit]").click()
    await dialog.wait_for(state="detached", timeout=30000)
    drawer = page.locator("#jobs")
    await drawer.wait_for()
    job = drawer.locator("article.job").first
    await job.locator(".job-state.ok").wait_for()
    text = await job.inner_text()
    assert "Create worktree u3-job" in text
    assert "U3_JOB u3-job --with-extra" in text and "U3_DONE" in text
    await page.locator("li.wt.current", has_text="u3-job").wait_for()

    # A failing command: the dialog keeps the error, the job says why.
    await _outside(page)
    await page.keyboard.press("c")
    await dialog.wait_for()
    await dialog.locator("input[name=branch]").fill("nope")
    await dialog.locator("button[type=submit]").click()
    await dialog.locator(".form-error").wait_for(timeout=30000)
    assert "failed" in (await dialog.locator(".form-error").inner_text()).lower()
    await drawer.locator("article.job .job-state.failed").wait_for()
    # Problems: the failed create is answered with an error, nothing else.
    assert list(problems) == []


FORM_BENCH = """async (n) => {
  const out = [];
  const frame = () => new Promise((r) => requestAnimationFrame(() => r()));
  for (let i = 0; i < n; i++) {
    document.activeElement?.blur();
    const start = performance.now();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', bubbles: true }));
    // In the DOM (Preact renders in a microtask), then the frame that paints it.
    while (!document.getElementById('create-dialog')) await new Promise((r) => setTimeout(r, 0));
    await frame();
    out.push(performance.now() - start);
    window.lazyagent.actions.closeDialog();
    await frame();
  }
  return out;
}"""


@pytest.mark.perf
async def test_forms_open_fast(host, browser, record):
    """U3 budget: a form is on screen ≤ 50 ms after its key."""
    from tests.perf.stats import summary

    page, problems = await open_page(await browser.new_context(), host.server.url)
    times = await page.evaluate(FORM_BENCH, 30)
    result = summary(times[5:])
    record("U3 key → form painted (ms)", **result)
    assert result["p95"] <= 50
    assert list(problems) == []
