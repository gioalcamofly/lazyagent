"""The permission mode agents are spawned in ([agents] permission_mode): its
resolution, the providers' args, the config files, and the engine, IPC and
MCP paths that spawn and restore with it."""

from __future__ import annotations

import dataclasses
import logging
import shlex

import pytest

from lazyagent import agent_providers, mcp_server
from lazyagent.agent_providers import (
    PERMISSION_MODES,
    check_permission_mode,
    get_agent_provider,
    resolve_permission_mode,
)
from lazyagent.config import global_config_path, load_config
from lazyagent.engine.manifest import Entry
from tests.test_mcp_notes import engine  # noqa: F401  (fixture)


def _flags(command: str) -> list[str]:
    """The agent's own argv in a build_command / session command."""
    script = shlex.split(command)[2]
    return shlex.split(script.rpartition(" && exec ")[2])


# --- Resolution -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "skip", "configured", "default", "expected"),
    [
        # An explicit mode wins over everything.
        ("plan", True, "bypass", "bypass", "plan"),
        ("cli", None, "acceptEdits", "bypass", "cli"),
        # skip_permissions: true is bypass; false is "not bypass".
        (None, True, "plan", "cli", "bypass"),
        (None, False, "plan", "bypass", "plan"),
        (None, False, "bypass", "bypass", "cli"),
        (None, False, None, "bypass", "cli"),
        # Neither: the configured mode, else the path's default.
        (None, None, "dontAsk", "bypass", "dontAsk"),
        (None, None, "bypass", "cli", "bypass"),
        (None, None, None, "bypass", "bypass"),
        (None, None, None, "cli", "cli"),
    ],
)
def test_precedence(mode, skip, configured, default, expected):
    assert resolve_permission_mode(mode, skip, configured, default) == expected


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="Invalid permission_mode: 'yolo'"):
        resolve_permission_mode("yolo", None, None)
    with pytest.raises(ValueError, match="Invalid permission_mode"):
        check_permission_mode("acceptedits")
    assert check_permission_mode("auto") == "auto"


# --- Provider args --------------------------------------------------------


def test_claude_args():
    claude = get_agent_provider("claude")
    assert claude.permission_args("cli") == []
    assert claude.permission_args("bypass") == ["--dangerously-skip-permissions"]
    for mode in ("acceptEdits", "plan", "dontAsk", "auto", "default"):
        assert claude.permission_args(mode) == ["--permission-mode", mode]


@pytest.mark.parametrize(
    ("name", "flag"),
    [("codex", "--dangerously-bypass-approvals-and-sandbox"), ("gemini", "--approval-mode=yolo")],
)
def test_other_providers_only_bypass_or_not(name, flag, caplog, monkeypatch):
    monkeypatch.setattr(agent_providers, "_WARNED_MODES", set())
    provider = get_agent_provider(name)
    assert provider.permission_args("bypass") == [flag]
    assert provider.permission_args("cli") == []
    with caplog.at_level(logging.WARNING, logger="lazyagent.agent_providers"):
        assert provider.permission_args("plan") == []
        assert provider.permission_args("plan") == []
    warned = [r for r in caplog.records if "no permission mode 'plan'" in r.getMessage()]
    assert len(warned) == 1, "one warning per provider and mode"


def test_build_command_takes_the_mode_over_skip_permissions():
    claude = get_agent_provider("claude")
    context = agent_providers.ProviderRuntimeContext(
        provider_name="claude", worktree_path="/w", observation_mode=claude.observation_mode
    )

    def flags(**kw):
        return _flags(claude.build_command("/w", runtime_context=context, **kw))

    assert flags() == ["claude"]
    assert flags(skip_permissions=True) == ["claude", "--dangerously-skip-permissions"]
    assert flags(permission_mode="plan") == ["claude", "--permission-mode", "plan"]
    assert flags(skip_permissions=True, permission_mode="cli") == ["claude"]


# --- Config ---------------------------------------------------------------


def _global(text: str) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_config_default_is_not_configured(tmp_path):
    assert load_config(tmp_path).agent.permission_mode is None


def test_the_global_config_applies_without_a_repo_file(tmp_path):
    _global('[agents]\npermission_mode = "acceptEdits"\n')
    assert global_config_path().is_relative_to(tmp_path), "conftest isolates XDG_CONFIG_HOME"
    repo = tmp_path / "repo"
    repo.mkdir()
    assert load_config(repo).agent.permission_mode == "acceptEdits"
    (repo / ".lazyagent.toml").write_text("[notes]\nmode = \"free\"\n")
    assert load_config(repo).agent.permission_mode == "acceptEdits", "a repo file without the key"


def test_the_repo_wins(tmp_path):
    _global('[agents]\npermission_mode = "acceptEdits"\n')
    (tmp_path / ".lazyagent.toml").write_text('[agents]\npermission_mode = "bypass"\n')
    assert load_config(tmp_path).agent.permission_mode == "bypass"


def test_a_bad_value_is_ignored_with_a_warning(tmp_path, caplog):
    _global('[agents]\npermission_mode = "plan"\n')
    (tmp_path / ".lazyagent.toml").write_text('[agents]\npermission_mode = "yolo"\n')
    with caplog.at_level(logging.WARNING, logger="lazyagent.config"):
        assert load_config(tmp_path).agent.permission_mode == "plan", "the global one, then"
    assert "ignoring [agents] permission_mode 'yolo'" in caplog.text
    _global('[agents]\npermission_mode = 1\n')
    assert load_config(tmp_path).agent.permission_mode is None
    _global("not toml [")
    assert load_config(tmp_path).agent.permission_mode is None


def test_every_mode_parses(tmp_path):
    for mode in PERMISSION_MODES:
        (tmp_path / ".lazyagent.toml").write_text(f'[agents]\npermission_mode = "{mode}"\n')
        assert load_config(tmp_path).agent.permission_mode == mode


def test_a_manifest_entry_drops_an_unknown_mode():
    data = {"id": "e", "worktree_path": "/w", "agent_id": "a1", "kind": "agent", "provider": "claude"}
    assert Entry.from_dict({**data, "permission_mode": "plan"}).permission_mode == "plan"
    assert Entry.from_dict({**data, "permission_mode": "yolo"}).permission_mode is None
    assert Entry.from_dict(data).permission_mode is None


# --- Engine, IPC and MCP --------------------------------------------------

pytest_asyncio_mark = pytest.mark.asyncio


def _with_mode_flag(running) -> None:
    """The fake agent, taking Claude's --permission-mode."""
    provider = running._provider("claude")
    flagged = dataclasses.replace(provider, permission_mode_flag="--permission-mode")
    running._provider = lambda name: flagged


def _mode_flags(running, path, agent_id) -> list[str]:
    words = _flags(running.get_session(path, agent_id).command)
    out = [w for w in words if w in ("--paste", "--permission-mode")]
    if "--permission-mode" in words:
        out.append(words[words.index("--permission-mode") + 1])
    return out


async def _ipc(running, method, **params):
    return await mcp_server.IpcClient(running.socket_path).call(method, params)


@pytest_asyncio_mark
async def test_the_engine_spawns_in_the_configured_mode(engine):  # noqa: F811
    _with_mode_flag(engine)
    feat = engine.feat
    plain = await engine.spawn_agent(feat)
    assert plain.permission_mode == "cli"
    assert _mode_flags(engine, feat, plain.agent_id) == []
    engine.config.agent.permission_mode = "acceptEdits"
    configured = await engine.spawn_agent(feat)
    assert configured.permission_mode == "acceptEdits"
    assert _mode_flags(engine, feat, configured.agent_id) == ["--permission-mode", "acceptEdits"]
    bypass = await engine.spawn_agent(feat, skip_permissions=True)
    assert _mode_flags(engine, feat, bypass.agent_id) == ["--paste"]
    explicit = await engine.spawn_agent(feat, permission_mode="plan", skip_permissions=True)
    assert _mode_flags(engine, feat, explicit.agent_id) == ["--permission-mode", "plan"]
    # Recorded: the manifest (bypass also as the older skip_permissions) and
    # the agents payloads.
    entry = engine.manifest.get(feat, bypass.agent_id)
    assert (entry.permission_mode, entry.skip_permissions) == ("bypass", True)
    assert engine.manifest.get(feat, explicit.agent_id).skip_permissions is False
    modes = {a["agent_id"]: a["permission_mode"] for a in engine.agents_payload(feat)}
    assert modes == {plain.agent_id: "cli", configured.agent_id: "acceptEdits",
                     bypass.agent_id: "bypass", explicit.agent_id: "plan"}
    assert engine.recorded_permission_mode(feat) == "plan"
    assert engine.get_config().permission_mode == "acceptEdits"


@pytest_asyncio_mark
async def test_the_rpc_default_is_bypass_and_a_client_can_name_its_own(engine):  # noqa: F811
    feat = engine.feat
    result = await _ipc(engine, "spawn_agent", worktree_path=feat)
    listed = await _ipc(engine, "list_agents", worktree_path=feat)
    assert {a["agent_id"]: a["permission_mode"] for a in listed["agents"]} == {
        result["agent_id"]: "bypass"
    }, "no mode and none configured: today's RPC default"
    desktop = await _ipc(engine, "spawn_agent", worktree_path=feat, default_permission_mode="cli")
    assert engine.get_agent(feat, desktop["agent_id"]).permission_mode == "cli"
    engine.config.agent.permission_mode = "dontAsk"
    configured = await _ipc(engine, "spawn_agent", worktree_path=feat, default_permission_mode="cli")
    assert engine.get_agent(feat, configured["agent_id"]).permission_mode == "dontAsk"
    worktrees = await _ipc(engine, "list_worktrees")
    feat_row = next(w for w in worktrees if w["path"] == feat)
    assert {a["permission_mode"] for a in feat_row["agents"]} == {"bypass", "cli", "dontAsk"}


@pytest_asyncio_mark
async def test_the_rpc_refuses_bad_values(engine):  # noqa: F811
    feat = engine.feat
    with pytest.raises(RuntimeError, match="Invalid permission_mode"):
        await _ipc(engine, "spawn_agent", worktree_path=feat, permission_mode="yolo")
    with pytest.raises(RuntimeError, match="Invalid permission_mode"):
        await _ipc(engine, "spawn_agent", worktree_path=feat, default_permission_mode="nope")
    with pytest.raises(RuntimeError, match="skip_permissions must be a boolean"):
        await _ipc(engine, "spawn_agent", worktree_path=feat, skip_permissions="yes")
    with pytest.raises(RuntimeError, match="Invalid permission_mode"):
        await _ipc(engine, "restore_agents", permission_mode="yolo")
    assert engine.agents_payload(feat) == []


@pytest_asyncio_mark
async def test_mcp_spawn_keeps_bypass_by_default_but_follows_the_config(engine):  # noqa: F811
    feat = engine.feat
    first = await mcp_server.spawn_agent(feat)
    assert engine.get_agent(feat, first["agent_id"]).permission_mode == "bypass"
    normal = await mcp_server.spawn_agent(feat, skip_permissions=False)
    assert engine.get_agent(feat, normal["agent_id"]).permission_mode == "cli"
    named = await mcp_server.spawn_agent(feat, permission_mode="acceptEdits")
    assert engine.get_agent(feat, named["agent_id"]).permission_mode == "acceptEdits"
    engine.config.agent.permission_mode = "plan"
    configured = await mcp_server.spawn_agent(feat)
    assert engine.get_agent(feat, configured["agent_id"]).permission_mode == "plan"
    with pytest.raises(RuntimeError, match="Invalid permission_mode"):
        await mcp_server.spawn_agent(feat, permission_mode="yolo")


def _ended(running, path, *infos) -> list[Entry]:
    """Stop the agents and offer them, as an engine that ended under them
    would (a stop on purpose drops them from the manifest)."""
    entries = [running.manifest.get(path, info.agent_id) for info in infos]
    for info, entry in zip(infos, entries):
        running.stop_agent(path, info.agent_id)
        # Each its own conversation: none would resume the worktree's last.
        entry.conversation_id = f"conv-{info.agent_id}"
        running.manifest.restorable[entry.id] = entry
    return entries


@pytest_asyncio_mark
async def test_a_restore_reuses_the_recorded_mode(engine):  # noqa: F811
    _with_mode_flag(engine)
    feat = engine.feat
    planned = await engine.spawn_agent(feat, permission_mode="plan")
    bypass = await engine.spawn_agent(feat, skip_permissions=True)
    manifest = engine.manifest
    _ended(engine, feat, planned, bypass)
    # And one an older engine wrote: only "not bypass" on record.
    old = Entry(id="old", worktree_path=feat, agent_id="a9", kind="agent", provider="claude",
                skip_permissions=False, conversation_id="conv-old", started_at=0.0)
    manifest.restorable[old.id] = old
    engine.config.agent.permission_mode = "acceptEdits"
    offered = {e["agent_id"]: e["permission_mode"]
               for e in (await _ipc(engine, "list_restorable"))["agents"]}
    assert offered == {planned.agent_id: "plan", bypass.agent_id: "bypass", "a9": "acceptEdits"}

    result = await engine.restore_agents(env=None)
    assert result["failed"] == []
    modes = {r["agent_id"]: r["permission_mode"] for r in result["restored"]}
    assert modes == {planned.agent_id: "plan", bypass.agent_id: "bypass", "a9": "acceptEdits"}
    assert _mode_flags(engine, feat, planned.agent_id) == ["--permission-mode", "plan"]
    assert _mode_flags(engine, feat, bypass.agent_id) == ["--paste"]
    assert manifest.get(feat, "a9").permission_mode == "acceptEdits"


@pytest_asyncio_mark
async def test_a_restore_can_name_the_mode_for_every_one(engine):  # noqa: F811
    feat = engine.feat
    agent = await engine.spawn_agent(feat, skip_permissions=True)
    (entry,) = _ended(engine, feat, agent)
    result = await mcp_server.restore_agents(ids=[entry.id], permission_mode="cli")
    assert [r["permission_mode"] for r in result["restored"]] == ["cli"]
    assert engine.get_agent(feat, agent.agent_id).permission_mode == "cli"
