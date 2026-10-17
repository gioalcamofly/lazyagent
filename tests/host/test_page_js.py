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


async def test_selection_follows_the_hosted_view(page):
    result = await page.evaluate(
        """async () => {
        const { selection } = await import('/components/sidebar.js');
        return [
          selection(null),
          selection({orchestrator: true, worktree_path: null, agent_id: '', focus: 'agent'}),
          selection({orchestrator: false, worktree_path: '/r/a', agent_id: '2', focus: 'shell'}),
          selection({orchestrator: false, worktree_path: '/r/a', agent_id: '2', focus: 'agent'}),
        ];
    }"""
    )
    assert result == [
        {"path": None, "agent": None},  # undefined comes back as None
        {"path": None, "agent": ""},
        {"path": "/r/a", "agent": None},
        {"path": "/r/a", "agent": "2"},
    ]


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
