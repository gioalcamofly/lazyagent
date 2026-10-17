"""Browser-side budgets of the page (markers ``perf`` and ``e2e``).

- B12: the sidebar re-renders 100 worktrees in ≤ 4 ms (§7d-1).
- Idle CPU with Preact loaded: no worse than H2's page (Chrome 1.3% in
  §7d-1), everything ≤ 2% (B6), asserted with 1.5× headroom.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from tests.browser import Problems, module_page, needs_playwright
from tests.perf.procstat import cpu_seconds, descendants, group
from tests.perf.stats import summary

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.perf, pytest.mark.e2e]

SIDEBAR_BENCH = """async (count) => {
  const { html, render } = await import('/lib/html.js');
  const { createStore } = await import('/lib/store.js');
  const { WorktreeList } = await import('/components/sidebar.js');
  const store = createStore();
  const actions = { select() {}, focusAgent() {} };
  const make = (i, status) => ({
    path: `/r/wt-${i}`, branch: `feature/branch-${i}`, head: '0123456789', is_main: i === 0,
    git_status: { dirty_count: i % 3, ahead: i % 2, behind: 0 },
    agents: [
      { agent_id: `${i}a`, status, detail: 'working', label: '' },
      { agent_id: `${i}b`, status: 'waiting_for_user', detail: 'question', label: 'reviewer' },
    ],
  });
  const host = document.createElement('div');
  document.body.append(host);
  const settle = () => new Promise((r) => { const c = new MessageChannel(); c.port1.onmessage = r; c.port2.postMessage(0); });
  const pr = {};
  for (let i = 0; i < count; i++) pr[`/r/wt-${i}`] = { number: i, state: 'OPEN', title: 't', checks: [{ status: 'COMPLETED', conclusion: 'SUCCESS' }] };
  store.applySidebar({ worktrees: Array.from({ length: count }, (_, i) => make(i, 'running')),
                       orchestrator: { agents: [] }, pr_info: pr, view: null, cursor: 0 });
  let t = performance.now();
  render(html`<${WorktreeList} store=${store} actions=${actions} />`, host);
  const mount = performance.now() - t;
  const rows = host.querySelectorAll('li.wt').length;
  const full = [], one = [];
  for (let n = 0; n < 40; n++) {
    const status = n % 2 ? 'running' : 'waiting';
    t = performance.now();
    store.applyEvent('worktrees.changed', Array.from({ length: count }, (_, i) => make(i, status)));
    await settle();
    full.push(performance.now() - t);
    t = performance.now();
    store.applyEvent('agent.updated', { worktree_path: `/r/wt-${n}`, agent_id: `${n}a`, status: 'completed', detail: 'done' });
    await settle();
    one.push(performance.now() - t);
  }
  store.applyEvent('agent.updated', { worktree_path: '/r/wt-3', agent_id: '3a', status: 'completed', detail: 'done' });
  await settle();
  const ok = host.querySelector('li.wt[data-path="/r/wt-3"] li.agent.s-completed') !== null;
  return { mount, rows, full, one, ok };
}"""


async def test_sidebar_rerender_100_worktrees(browser, record):
    page, problems = await module_page(browser)
    result = await page.evaluate(SIDEBAR_BENCH, 100)
    assert result["rows"] == 101 and result["ok"]
    full, one = summary(result["full"][5:]), summary(result["one"][5:])
    record("B12 sidebar, 100 worktrees: full replace (ms)", mount=round(result["mount"], 2), **full)
    record("B12 sidebar, 100 worktrees: one agent (ms)", **one)
    assert full["p50"] <= 4.0
    assert one["p50"] <= 1.0
    assert list(problems) == []


async def test_idle_cpu_with_the_page_open(host, browser, record):
    page = await browser.new_page(viewport={"width": 1400, "height": 820})
    problems = Problems()
    problems.watch(page)
    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.wait_for_function("document.querySelectorAll('#worktrees li.wt').length === 3")
    await asyncio.sleep(3)
    pids = descendants(os.getpid())
    before, start = cpu_seconds(pids), time.monotonic()
    await asyncio.sleep(10)
    after, elapsed = cpu_seconds(pids), time.monotonic() - start
    usage: dict[str, float] = {}
    for pid, seconds in after.items():
        if pid in before:
            usage[group(pid)] = usage.get(group(pid), 0) + seconds - before[pid]
    percent = {name: round(100 * s / elapsed, 2) for name, s in sorted(usage.items())}
    page_cpu = sum(v for k, v in percent.items() if k.startswith("chrome"))
    total = page_cpu + percent.get("lazyagent (TUI + engine)", 0)
    record("idle CPU, % of one core", page=round(page_cpu, 2), total=round(total, 2), **percent)
    await page.close()
    # Chrome's browser and GPU processes wake up on their own: the renderer
    # alone is 0.2-0.5% with H2's page and with this one alike.
    assert percent.get("chrome renderer", 0) <= 1.0
    assert total <= 2.0 * 1.5
    assert list(problems) == []
