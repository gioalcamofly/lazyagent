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


async def test_idle_cpu_with_the_page_open(fake_claude, host, browser, record, tmp_path):
    """B6 with one agent on screen in a native terminal."""
    page = await browser.new_page(viewport={"width": 1400, "height": 820})
    problems = Problems()
    problems.watch(page)
    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    await page.wait_for_function("() => document.querySelectorAll('#worktrees li.wt').length === 3")
    await _native_agent(host, page, tmp_path)
    # Not focused: a blinking cursor would be work of its own.
    await page.locator("#engine-state").click()
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


ECHO_BENCH = """async ({ session, n, warm }) => {
  const t = window.lazyagent.terminals.sessions.get(session);
  const enc = new TextEncoder();
  const recv = [], paint = [];
  for (let i = 0; i < n + warm; i++) {
    const ch = 'abcdefghijklmnopqrstuvwxyz'[i % 26];
    const code = ch.charCodeAt(0);
    const original = t.output;
    let arrived = null;
    const got = new Promise((resolve) => {
      t.output = (offset, data) => {
        original.call(t, offset, data);
        if (arrived === null && data.includes(code)) { arrived = performance.now(); resolve(); }
      };
    });
    const start = performance.now();
    t.input(enc.encode(ch));
    await got;
    t.output = original;
    // Parsed, then the frame xterm.js paints it in.
    await new Promise((r) => t.term.write(new Uint8Array(0), r));
    await new Promise((r) => requestAnimationFrame(() => r()));
    if (i >= warm) { recv.push(arrived - start); paint.push(performance.now() - start); }
    await new Promise((r) => setTimeout(r, 8));
  }
  return { recv, paint, renderer: t.renderer };
}"""

SPEW_BENCH = """async ({ session, bytes }) => {
  const t = window.lazyagent.terminals.sessions.get(session);
  const frames = [];
  const longTasks = [];
  const observer = new PerformanceObserver((list) => {
    for (const e of list.getEntries()) longTasks.push(e.duration);
  });
  observer.observe({ type: 'longtask', buffered: false });
  let last = performance.now(), running = true;
  const tick = (now) => { frames.push(now - last); last = now; if (running) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  const startOffset = t.received;
  const start = performance.now();
  t.input(new TextEncoder().encode(`spew ${bytes}\\r`));
  const text = () => {
    const b = t.term.buffer.active;
    let s = '';
    for (let i = Math.max(0, b.length - 4); i < b.length; i++) s += b.getLine(i).translateToString(true) + '\\n';
    return s;
  };
  while (!(t.received - startOffset > bytes && text().includes('done'))) {
    await new Promise((r) => setTimeout(r, 20));
  }
  const elapsed = performance.now() - start;
  running = false;
  observer.disconnect();
  return { frames: frames.slice(1), longTasks, elapsed, received: t.received - startOffset,
           heap: performance.memory ? performance.memory.usedJSHeapSize : null };
}"""


async def _native_agent(host, page, tmp_path):
    from lazyagent.engine.client import Client
    from tests.contract.helpers import wait_for_text
    from tests.host.test_browser import native_has

    feat = str(tmp_path / "repo-feat")
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        agent = await driver.request("spawn_agent", worktree_path=feat)
        await wait_for_text(driver, agent, "your turn>")
    finally:
        await driver.close()
    await page.locator("li.wt", has_text="repo-feat").click()
    await native_has(page, agent["session_id"], "your turn>")
    await page.locator(".agent-slot").click()
    return agent


@pytest.mark.parametrize("renderer", ["webgl", "dom"])
async def test_keystroke_to_glyph(fake_claude, host, browser, record, renderer, tmp_path):
    """B1: key → bytes back in the page, and key → the frame that paints them."""
    page = await browser.new_page(viewport={"width": 1400, "height": 820})
    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    if renderer == "dom":
        await page.evaluate("() => { window.lazyagent.terminals.forceDom = true; }")
    agent = await _native_agent(host, page, tmp_path)
    result = await page.evaluate(ECHO_BENCH, {"session": agent["session_id"], "n": 200, "warm": 20})
    recv, paint = summary(result["recv"]), summary(result["paint"])
    record(f"B1 key→recv, {result['renderer']} (ms)", **recv)
    record(f"B1 key→painted frame, {result['renderer']} (ms)", **paint)
    await page.close()
    assert result["renderer"] == renderer or renderer == "webgl"
    assert recv["p95"] <= 16
    # Headless and a shared machine: 1.5× headroom (§7d-7).
    assert paint["p95"] <= 16 * 1.5


@pytest.mark.parametrize("tui", ["blank", "shown"])
async def test_spew_frames_and_the_tui_at_rest(fake_claude, host, browser, record, tmp_path, tui):
    """B4/B5 while the agent prints 5 MB natively, B8 right after, B9; and
    the hosted TUI, blank, does no work for it ("shown": the TUI tab is in
    front and renders the agent too, for comparison)."""
    from tests.perf.procstat import cpu_seconds

    page = await browser.new_page(viewport={"width": 1400, "height": 820})
    await page.goto(host.server.url)
    await page.wait_for_selector("#conn.ok", timeout=30000)
    agent = await _native_agent(host, page, tmp_path)
    if tui == "shown":
        await page.click(".tui-tab")
        await page.wait_for_selector(".center.tui-shown")
        await asyncio.sleep(1)
    tui_pid = host.tui.pid
    before = cpu_seconds([tui_pid])[tui_pid]
    result = await page.evaluate(SPEW_BENCH, {"session": agent["session_id"], "bytes": 5_000_000})
    tui_cpu = cpu_seconds([tui_pid])[tui_pid] - before
    frames = summary(result["frames"])
    seconds = result["elapsed"] / 1000
    record(
        f"B4 frames during a 5 MB spew, TUI {tui} (ms)",
        long_tasks_ms=round(sum(result["longTasks"]), 1),
        **frames,
    )
    record(
        f"B5 5 MB end to end, TUI {tui}",
        seconds=round(seconds, 2),
        mb_s=round(result["received"] / seconds / 1e6, 2),
        tui_and_engine_cpu_s=round(tui_cpu, 2),
        heap_mb=round((result["heap"] or 0) / 1e6, 1),
    )
    attach = await page.evaluate(
        """async (id) => {
          const times = [];
          for (let i = 0; i < 10; i++) {
            const t0 = performance.now();
            const r = await window.lazyagent.conn.request('attach_stream', { session_id: id });
            times.push(performance.now() - t0);
            await window.lazyagent.conn.request('detach_stream', { stream_id: r.stream_id });
          }
          return times;
        }""",
        agent["session_id"],
    )
    record(f"B8 attach with snapshot after the burst, TUI {tui} (ms)", **summary(attach))
    await page.close()
    if tui == "shown":
        return
    assert frames["p95"] <= 17 * 1.5
    assert sum(result["longTasks"]) <= 100 * 1.5
