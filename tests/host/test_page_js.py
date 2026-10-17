"""Unit tests of the page's JavaScript modules, run in a real browser page
through Playwright ``evaluate`` (no Node). The modules come from the static
harness in tests/browser.py: no host, no WebSocket."""

from __future__ import annotations

import pytest
import pytest_asyncio
from lazyagent.app import LazyAgent
from lazyagent.widgets.confirm_modal import ConfirmModal
from lazyagent.widgets.create_worktree_modal import CreateWorktreeModal
from lazyagent.widgets.prompt_modal import SpawnModal
from lazyagent.widgets.remove_worktree_modal import RemoveWorktreeModal
from lazyagent.widgets.worktree_list import WorktreeList
from textual.binding import Binding

from tests.browser import module_page, needs_playwright

pytestmark = [needs_playwright, pytest.mark.asyncio, pytest.mark.e2e]


@pytest_asyncio.fixture
async def page(browser):
    page, problems = await module_page(browser)
    yield page
    assert list(problems) == []


def wt(path, agents=(), **extra):
    return {
        "path": path,
        "branch": path.rsplit("/", 1)[-1],
        "head": "0123456789",
        "is_main": False,
        "agents": list(agents),
        **extra,
    }


def agent(agent_id, status="running", **extra):
    return {"agent_id": agent_id, "status": status, "detail": "", "label": "", **extra}


STORE = """
const { createStore } = await import('/lib/store.js');
const store = createStore();
const toasts = [], notes = [];
store.toast = (text, severity) => toasts.push([text, severity]);
store.notify = (title, body) => notes.push(body);
"""


async def test_format_helpers(page):
    result = await page.evaluate(
        """async () => {
        const f = await import('/lib/format.js');
        return {
          none: f.ciSummary([]),
          fail: f.ciSummary([{status: 'COMPLETED', conclusion: 'SUCCESS'}, {status: 'COMPLETED', conclusion: 'FAILURE'}]),
          pending: f.ciSummary([{status: 'IN_PROGRESS', conclusion: null}]),
          pass: f.ciSummary([{status: 'COMPLETED', conclusion: 'SUCCESS'}]),
          base: f.basename('/a/b/repo-feat/'),
          label: [f.agentLabel({agent_id: '3', label: ''}), f.agentLabel({agent_id: '3', label: 'fix'})],
          place: [f.placeName('__orchestrator__'), f.placeName('/x/repo-feat')],
        };
    }"""
    )
    assert result == {
        "none": None,
        "fail": "fail",
        "pending": "pending",
        "pass": "pass",
        "base": "repo-feat",
        "label": ["Agent 3", "fix"],
        "place": ["orchestrator", "repo-feat"],
    }


async def test_agent_update_copies_only_its_worktree(page):
    result = await page.evaluate(
        "async ([a, b]) => {" + STORE + """
        store.applySidebar({worktrees: [a, b], orchestrator: {agents: []}, pr_info: {}, view: null, cursor: 0});
        const [oldA, oldB] = store.worktrees.value;
        const refresh = store.applyEvent('agent.updated', {worktree_path: '/r/b', agent_id: '2', status: 'waiting', detail: 'hm'});
        const [newA, newB] = store.worktrees.value;
        const unknown = store.applyEvent('agent.updated', {worktree_path: '/r/b', agent_id: '9', status: 'running', detail: ''});
        return {refresh, unknown, sameA: oldA === newA, sameB: oldB === newB,
                agent: newB.agents[0], oldAgent: oldB.agents[0]};
    }""",
        [wt("/r/a", [agent("1")]), wt("/r/b", [agent("2")])],
    )
    assert result["refresh"] is False and result["unknown"] is True
    assert result["sameA"] is True and result["sameB"] is False
    assert result["agent"]["status"] == "waiting" and result["agent"]["detail"] == "hm"
    assert result["oldAgent"]["status"] == "running"


async def test_git_pr_and_worktree_events(page):
    result = await page.evaluate(
        "async ([a, b]) => {" + STORE + """
        store.applySidebar({worktrees: [a, b], orchestrator: {agents: []}, pr_info: {'/r/a': {number: 1}}, view: null, cursor: 0});
        const before = store.worktrees.value;
        store.applyEvent('git_status.updated', {'/r/b': {dirty_count: 2, ahead: 1, behind: 0}});
        const afterGit = store.worktrees.value;
        store.applyEvent('pr_info.updated', {path: '/r/b', pr_info: {number: 7}});
        store.applyEvent('worktrees.changed', [a]);
        return {sameA: before[0] === afterGit[0], git: afterGit[1].git_status,
                pr: store.prInfo.value, paths: store.worktrees.value.map(w => w.path)};
    }""",
        [wt("/r/a"), wt("/r/b")],
    )
    assert result["sameA"] is True
    assert result["git"] == {"dirty_count": 2, "ahead": 1, "behind": 0}
    assert result["pr"] == {"/r/a": {"number": 1}, "/r/b": {"number": 7}}
    assert result["paths"] == ["/r/a"]


async def test_alerts(page):
    result = await page.evaluate(
        "async ([a]) => {" + STORE + """
        store.applySidebar({worktrees: [a], orchestrator: {agents: [{agent_id: '1', label: 'boss', status: 'running'}]},
                            pr_info: {}, view: null, cursor: 0});
        const base = {worktree_path: '/r/repo-feat', agent_id: '4', label: 'fix'};
        store.applyEvent('agent.status', {...base, status: 'waiting_for_approval', previous_status: 'running'});
        store.applyEvent('agent.status', {...base, status: 'waiting_for_approval', previous_status: 'waiting_for_approval'});
        store.applyEvent('agent.status', {...base, status: 'running', previous_status: 'waiting'});
        store.applyEvent('agent.status', {worktree_path: '__orchestrator__', agent_id: '1', status: 'completed', previous_status: 'running'});
        const stopped = store.applyEvent('agent.exited', {...base, stopped: true, exit_code: 0});
        const exited = store.applyEvent('agent.exited', {worktree_path: '/r/repo-feat', agent_id: '4', exit_code: 3});
        store.applyEvent('engine.notice', {severity: 'warning', message: 'careful'});
        return {toasts, notes, stopped, exited};
    }""",
        [wt("/r/repo-feat", [agent("4", label="fix")])],
    )
    assert result["toasts"] == [
        ["fix (repo-feat) needs approval", "warning"],
        ["Agent 1 (orchestrator) completed", "success"],
        ["fix (repo-feat) exited (code 3)", "error"],
        ["careful", "warning"],
    ]
    # An agent.status carries no label: the orchestrator's comes out as
    # "Agent 1", as in H2.
    assert result["notes"] == [t for t, _ in result["toasts"][:3]]
    assert result["stopped"] is True and result["exited"] is True


async def test_queue_applies_in_order_in_one_batch(page):
    result = await page.evaluate(
        "async ([a, b]) => {" + STORE + """
        const { effect } = await import('/lib/html.js');
        let runs = 0;
        effect(() => { store.worktrees.value; store.prInfo.value; runs++; });
        runs = 0;
        store.enqueue(() => store.applySidebar({worktrees: [a, b], orchestrator: {agents: []}, pr_info: {}, view: null, cursor: 0}));
        store.enqueue(() => store.applyEvent('worktrees.changed', [b]));
        store.enqueue(() => store.applyEvent('pr_info.updated', {path: '/r/b', pr_info: {number: 2}}));
        const before = store.worktrees.value.length;
        await new Promise(r => requestAnimationFrame(() => setTimeout(r)));
        return {before, runs, paths: store.worktrees.value.map(w => w.path)};
    }""",
        [wt("/r/a"), wt("/r/b")],
    )
    assert result == {"before": 0, "runs": 1, "paths": ["/r/b"]}


async def test_selection_is_the_pages_own(page):
    result = await page.evaluate(
        "async ([a, b]) => {" + STORE + """
        const { selection } = await import('/components/sidebar.js');
        store.applySidebar({worktrees: [a, b], orchestrator: {agents: [{agent_id: '9', session_id: 's9'}]},
                            pr_info: {}, view: null, cursor: 0});
        const out = [selection(store)];
        store.selected.value = '/r/b';
        out.push(selection(store));            // the last agent by default
        store.activeAgent.value = {'/r/b': '2'};
        out.push(selection(store));
        store.activeAgent.value = {'/r/b': 'gone'};
        out.push(selection(store));            // a chosen agent that left
        store.selected.value = null;
        out.push(selection(store));            // the orchestrator
        store.selected.value = '/r/a';
        out.push(selection(store));            // no agent there
        return out;
    }""",
        [wt("/r/a"), wt("/r/b", [agent("2"), agent("3")])],
    )
    assert result == [
        {"path": None, "agent": None},  # undefined comes back as None
        {"path": "/r/b", "agent": "3"},
        {"path": "/r/b", "agent": "2"},
        {"path": "/r/b", "agent": "3"},
        {"path": None, "agent": "9"},
        {"path": "/r/a", "agent": None},
    ]


async def test_stream_frames(page):
    result = await page.evaluate(
        """async () => {
        const t = await import('/lib/terminals.js');
        const out = new Uint8Array([1, 0, 0, 0, 7, 0, 0, 0, 1, 0, 0, 0, 5, 104, 105]);
        const parsed = t.parseFrame(out.buffer);
        const input = t.inputFrame(7, 'é');
        const binary = t.inputFrame(0, t.binaryBytes('\x1b[M\xff'));
        return {type: parsed.type, stream: parsed.streamId, offset: parsed.offset,
                data: [...parsed.data], shared: parsed.data.buffer === out.buffer,
                input: [...input], binary: [...binary]};
    }"""
    )
    assert result == {
        "type": 1,
        "stream": 7,
        "offset": 2**32 + 5,
        "data": [104, 105],
        "shared": True,  # a view, not a copy
        "input": [2, 0, 0, 0, 7, 0xC3, 0xA9],
        "binary": [2, 0, 0, 0, 0, 0x1B, 0x5B, 0x4D, 0xFF],
    }


async def test_writer_batches_while_a_write_is_pending(page):
    result = await page.evaluate(
        """async () => {
        const { Writer } = await import('/lib/terminals.js');
        const writes = [];
        let finish = null;
        const writer = new Writer((data, done) => { writes.push([...data]); finish = done; });
        writer.push(new Uint8Array([1]));          // written at once
        writer.push(new Uint8Array([2]));          // queued
        writer.push(new Uint8Array([3, 4]));       // queued
        const before = writes.length;
        await new Promise(r => requestAnimationFrame(() => setTimeout(r)));
        const stillOne = writes.length;            // the first is still pending
        finish();
        await new Promise(r => requestAnimationFrame(() => setTimeout(r)));
        finish();
        writer.push(new Uint8Array([5]));          // idle again: at once
        return {before, stillOne, writes};
    }"""
    )
    assert result == {"before": 1, "stillOne": 1, "writes": [[1], [2, 3, 4], [5]]}


async def test_software_webgl_is_not_used(page):
    result = await page.evaluate(
        """async () => {
        const { isSoftwareRenderer } = await import('/lib/terminals.js');
        return ['ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)',
                'llvmpipe (LLVM 17.0.6, 256 bits)', 'Microsoft Basic Render Driver',
                'Mesa Intel(R) Graphics (ADL GT2)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060)', '']
               .map(isSoftwareRenderer);
    }"""
    )
    assert result == [True, True, True, False, False, False]


def _binding_keys(bindings) -> set[str]:
    keys = set()
    for binding in bindings:
        if isinstance(binding, Binding):
            keys.update(k.strip() for k in binding.key.split(","))
        else:
            keys.add(binding[0])
    return keys


async def test_keymap_lists_every_tui_binding(page):
    tui_keys = await page.evaluate(
        """async () => {
        const { KEYMAP, SCOPES } = await import('/lib/keymap.js');
        for (const row of KEYMAP) if (!(row.scope in SCOPES)) throw new Error(row.scope);
        return KEYMAP.flatMap(row => row.tui);
    }"""
    )
    app_keys = _binding_keys(LazyAgent.BINDINGS)
    assert app_keys - set(tui_keys) == set()
    widget_keys = _binding_keys(
        [
            *WorktreeList.BINDINGS,
            *SpawnModal.BINDINGS,
            *ConfirmModal.BINDINGS,
            *RemoveWorktreeModal.BINDINGS,
            *CreateWorktreeModal.BINDINGS,
        ]
    )
    assert widget_keys - set(tui_keys) == set()


async def test_an_unchanged_worktree_keeps_its_object(page):
    result = await page.evaluate(
        "async ([a, b, b2]) => {" + STORE + """
        store.applySidebar({worktrees: [a, b], orchestrator: {agents: []}, pr_info: {}, view: null, cursor: 0});
        const before = store.worktrees.value;
        store.applyEvent('worktrees.changed', [structuredClone(a), structuredClone(b)]);
        const same = store.worktrees.value === before;
        store.applyEvent('worktrees.changed', [structuredClone(a), b2]);
        const after = store.worktrees.value;
        return {same, keptA: after[0] === before[0], newB: after[1] !== before[1], status: after[1].agents[0].status};
    }""",
        [wt("/r/a", [agent("1")]), wt("/r/b", [agent("2")]), wt("/r/b", [agent("2", "waiting")])],
    )
    assert result == {"same": True, "keptA": True, "newB": True, "status": "waiting"}


async def test_remove_guard_and_default_base(page):
    result = await page.evaluate(
        """async () => {
        const d = await import('/components/dialogs.js');
        const wt = (extra) => ({path: '/r/x', branch: 'x', is_main: false, agents: [], ...extra});
        return {
          none: d.removeGuard(undefined),
          main: d.removeGuard(wt({is_main: true})),
          running: d.removeGuard(wt({agents: [{status: 'running'}]})),
          waiting: d.removeGuard(wt({agents: [{status: 'waiting_for_user'}]})),
          done: d.removeGuard(wt({agents: [{status: 'completed'}, {status: 'failed'}]})),
          ok: d.removeGuard(wt()),
          base: [
            d.defaultBase({default_branch: 'master'}, [{is_main: true, branch: 'main'}]),
            d.defaultBase({default_branch: 'develop'}, [{is_main: true, branch: 'main'}, {branch: 'develop'}]),
            d.defaultBase(null, []),
          ],
        };
    }"""
    )
    assert result["none"] and "main worktree" in result["main"]
    assert "stop it first" in result["running"] and "stop it first" in result["waiting"]
    assert result["done"] is None and result["ok"] is None
    assert result["base"] == ["main", "develop", "main"]


async def test_job_events(page):
    result = await page.evaluate(
        "async () => {" + STORE + """
        const { JOB_OUTPUT_LIMIT } = await import('/lib/store.js');
        store.applyEvent('job.started', {job_id: 'j1', title: 'Create worktree x', command: 'make'});
        const opened = store.drawerOpen.value;
        store.applyEvent('job.output', {job_id: 'j1', text: 'a'.repeat(JOB_OUTPUT_LIMIT)});
        store.applyEvent('job.output', {job_id: 'j1', text: 'END'});
        store.applyEvent('job.finished', {job_id: 'j1', exit_code: 2});
        const job = store.jobs.value[0];
        return {opened, length: job.text.length, tail: job.text.slice(-3), exit: job.exitCode, toasts};
    }"""
    )
    assert result["opened"] is True
    assert result["length"] == 256 * 1024 and result["tail"] == "END"
    assert result["exit"] == 2
    assert result["toasts"] == [["Create worktree x: failed (2)", "error"]]


ATTENTION = """
const a = await import('/lib/attention.js');
const wt = (path, agents, extra) => ({path, branch: 'b', head: '0', is_main: false, agents, ...extra});
const ag = (id, status, extra) => ({agent_id: id, status, detail: status, label: '', session_id: 's' + id, ...extra});
const base = {worktrees: [], orchestrator: [], prInfo: {}, exits: [], jobs: [], dismissed: {}, snoozed: {}};
"""


async def test_attention_ranks_and_clears(page):
    result = await page.evaluate(
        "async () => {" + ATTENTION + """
        const since = {};
        const world = {...base, since, now: 1000,
          worktrees: [wt('/r/a', [ag('1', 'running'), ag('2', 'waiting_for_user')]),
                      wt('/r/b', [ag('3', 'completed'), ag('4', 'waiting_for_approval')])],
          orchestrator: [ag('9', 'possibly_hanged')],
          prInfo: {'/r/a': {number: 7, title: 'T', state: 'OPEN', checks: [{name: 'ci', status: 'COMPLETED', conclusion: 'FAILURE', details_url: 'https://ci/1'}],
                            review_decision: 'CHANGES_REQUESTED', mergeable: 'CONFLICTING'},
                   '/r/b': {number: 8, title: 'M', state: 'MERGED', checks: [{name: 'ci', status: 'COMPLETED', conclusion: 'FAILURE'}]}},
          exits: [{key: '/r/a', agent_id: '5', label: 'Agent 5', exit_code: 2, at: 900}],
          jobs: [{id: 'j', title: 'Create x', exitCode: 1, text: ''}, {id: 'k', title: 'ok', exitCode: 0, text: ''}]};
        const first = a.attentionItems(world).map(i => i.kind + ':' + i.where);
        // Later, the question was answered and the approval granted.
        world.now = 5000;
        world.worktrees = [wt('/r/a', [ag('1', 'running'), ag('2', 'running')]), wt('/r/b', [ag('3', 'completed'), ag('4', 'waiting_for_approval')])];
        const later = a.attentionItems(world);
        const prompt = a.ciFailurePrompt(later.find(i => i.kind === 'ci'));
        return {first, later: later.map(i => i.kind), kept: since['approval:/r/b/4'], gone: 'question:/r/a/2' in since, prompt};
    }"""
    )
    assert result["first"] == [
        "approval:b",
        "question:a",
        "hanged:orchestrator",
        "exited:a",
        "ci:a",
        "review:a",
        "conflict:a",
        "done:b",
        "job:job",
    ]
    assert "question" not in result["later"] and result["later"][0] == "approval"
    assert result["kept"] == 1000 and result["gone"] is False
    assert "PR #7" in result["prompt"] and "- ci: https://ci/1" in result["prompt"]


async def test_attention_dismiss_and_snooze(page):
    result = await page.evaluate(
        "async () => {" + ATTENTION + """
        const since = {};
        const world = {...base, since, now: 1000, worktrees: [wt('/r/a', [ag('1', 'waiting_for_user'), ag('2', 'failed')])]};
        const [q, f] = a.attentionItems(world).sort((x, y) => x.rank - y.rank);
        world.dismissed = {[q.occurrence]: 1};
        world.snoozed = {[f.id]: 2000};
        const hidden = a.attentionItems(world).length;
        world.now = 2500;                      // the snooze ran out
        const back = a.attentionItems(world).map(i => i.kind);
        // The question clears, then comes back: a new occurrence shows again.
        world.worktrees = [wt('/r/a', [ag('1', 'running'), ag('2', 'failed')])];
        a.attentionItems(world);
        world.now = 3000;
        world.worktrees = [wt('/r/a', [ag('1', 'waiting_for_user'), ag('2', 'failed')])];
        const again = a.attentionItems(world).map(i => i.kind);
        return {hidden, back, again};
    }"""
    )
    assert result == {"hidden": 0, "back": ["failed"], "again": ["question", "failed"]}
