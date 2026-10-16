"""Engine: spawn/stop/exit, shells, worktree jobs, env, events and lifecycle.

Agent tests run tests/engine/fake_agent.py under a real PTY through a fake
provider (see tests/engine/fakes.py); nothing here needs Textual.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from lazyagent.config import Config, WorktreeConfig
from lazyagent.engine.engine import ORCHESTRATOR_KEY, Engine
from lazyagent.engine.events import EngineEvent
from lazyagent.engine.session import AgentSession, TerminalSession
from lazyagent.models import AgentStatus, GitStatus
from lazyagent.protocol import validation
from lazyagent.worktree_manager import WorktreeManagerError
from tests.engine.fakes import (
    World,
    add_session,
    fake_agent_provider,
    hooked_fake_agent_provider,
    hooks_dir,
    make_engine,
    no_pty,
    worktree,
)

pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


class Recorder:
    """Every event the engine emits, each validated against the v1 schema."""

    def __init__(self, engine: Engine) -> None:
        self.events: list[EngineEvent] = []
        engine.subscribe(self._record)

    def _record(self, event: EngineEvent) -> None:
        validation.validate_event(event.event, json.loads(json.dumps(event.data)), strict=True)
        self.events.append(event)

    def named(self, name: str) -> list[dict]:
        return [e.data for e in self.events if e.event == name]

    def names(self) -> list[str]:
        return [e.event for e in self.events]


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def feat(repo) -> Path:
    path = repo.parent / "repo-feat"
    path.mkdir()
    return path


@pytest.fixture
def engine(tmp_path, repo, feat):
    return make_engine(
        [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
        repo_root=str(repo),
        provider=fake_agent_provider(tmp_path),
    )


@pytest_asyncio.fixture
async def running(engine):
    """The engine, stopped (every session killed) after the test."""
    try:
        yield engine
    finally:
        await engine.stop()


def _shown(session: TerminalSession) -> str:
    session.flush()
    return session.screen_text()


class TestSpawn:
    @pytest.mark.asyncio
    async def test_spawn_registers_the_session_and_announces_it(self, running, feat):
        events = Recorder(running)

        info = await running.spawn_agent(str(feat))

        session = running.get_session(str(feat), info.agent_id)
        assert isinstance(session, AgentSession)
        assert session.running
        assert info.agent_id == "a1"
        assert info.label == "Agent 1"
        assert info.kind == "agent"
        assert session.session_id == info.session_id
        assert running.agent_ids(str(feat)) == ["a1"]
        assert running.get_agent(str(feat), "a1") == info
        assert running.list_agents(str(feat)) == [info]
        assert running.registry.label(str(feat), "a1") == "Agent 1"
        assert events.named("agent.spawned") == [info.to_dict()]
        # The first status is recorded without any client in between.
        assert running.agent_states(str(feat))["a1"].status == AgentStatus.RUNNING
        await _until(lambda: "your turn>" in _shown(session))

    @pytest.mark.asyncio
    async def test_spawned_comes_before_the_first_status(self, running, feat):
        events = Recorder(running)
        await running.spawn_agent(str(feat))
        names = events.names()
        assert names.index("agent.spawned") < names.index("agent.status")

    @pytest.mark.asyncio
    async def test_unknown_worktree_is_rejected(self, running, tmp_path):
        with pytest.raises(ValueError, match="Unknown worktree"):
            await running.spawn_agent(str(tmp_path / "nowhere"))

    @pytest.mark.asyncio
    async def test_ids_and_labels_are_per_worktree(self, running, repo, feat):
        first = await running.spawn_agent(str(feat))
        second = await running.spawn_agent(str(feat), label="fixer")
        other = await running.spawn_agent(str(repo))
        assert (first.agent_id, second.agent_id, other.agent_id) == ("a1", "a2", "a1")
        assert second.label == "fixer"

    @pytest.mark.asyncio
    async def test_orchestrator_runs_in_the_repo_root(self, running, repo, monkeypatch):
        no_pty(monkeypatch)
        events = Recorder(running)

        info = await running.spawn_agent("ignored", kind="orchestrator")

        session = running.get_session(ORCHESTRATOR_KEY)
        assert (info.worktree_path, info.agent_id, info.kind) == (ORCHESTRATOR_KEY, "", "orchestrator")
        assert f"cd {repo}" in session.command
        assert running.has_agent(ORCHESTRATOR_KEY)
        assert events.named("agent.spawned")[0]["kind"] == "orchestrator"
        with pytest.raises(ValueError, match="already running"):
            await running.spawn_agent("ignored", kind="orchestrator")

    @pytest.mark.asyncio
    async def test_orchestrator_gets_the_composed_system_prompt(self, tmp_path, repo, monkeypatch):
        no_pty(monkeypatch)
        engine = make_engine([worktree(str(repo))], repo_root=str(repo))
        try:
            await engine.spawn_agent(str(repo), kind="orchestrator")
            command = engine.get_session(ORCHESTRATOR_KEY).command
            assert "--append-system-prompt" in command
            assert "--name" in command and "repo-orchestrator" in command
        finally:
            await engine.stop()


class TestExitAndStop:
    @pytest.mark.asyncio
    async def test_exit_announces_and_forgets_the_agent(self, running, feat):
        events = Recorder(running)
        info = await running.spawn_agent(str(feat))
        session = running.get_session(str(feat), "a1")
        await _until(lambda: "your turn>" in _shown(session))

        await running.send_agent_input(str(feat), "a1", "exit 3")
        await _until(lambda: events.named("agent.exited"))

        assert events.named("agent.exited") == [
            {
                "worktree_path": str(feat),
                "agent_id": "a1",
                "session_id": info.session_id,
                "exit_code": 3,
                "stopped": False,
            }
        ]
        assert events.named("agent.status")[-1]["status"] == "exited"
        assert running.get_session(str(feat), "a1") is None
        assert running.agent_ids(str(feat)) == []
        assert running.agent_states(str(feat)) == {}
        assert running.registry.label(str(feat), "a1") == ""

    @pytest.mark.asyncio
    async def test_stop_kills_and_announces(self, running, feat):
        events = Recorder(running)
        await running.spawn_agent(str(feat))
        session = running.get_session(str(feat), "a1")
        pid = session.pid

        info = running.stop_agent(str(feat), "a1")

        assert info.agent_id == "a1"
        assert not session.running
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert [e["stopped"] for e in events.named("agent.exited")] == [True]
        assert running.agent_ids(str(feat)) == []
        with pytest.raises(ValueError):
            running.stop_agent(str(feat), "a1")

    @pytest.mark.asyncio
    async def test_read_and_send_go_through_the_session(self, running, feat):
        await running.spawn_agent(str(feat), skip_permissions=True)  # --paste
        session = running.get_session(str(feat), "a1")
        await _until(lambda: "your turn>" in _shown(session))

        await running.send_agent_input(str(feat), "a1", "hello")
        await _until(lambda: "got:" in _shown(session))

        lines, total = running.read_agent_output(str(feat), "a1", 50)
        assert "got: 'hello' paste=True" in lines
        assert total == session.total_lines
        with pytest.raises(ValueError):
            running.read_agent_output(str(feat), "a9", 5)
        with pytest.raises(ValueError, match="not ready"):
            await running.send_agent_input(str(feat), "a9", "x")


class TestObserverFiles:
    """An agent's hooks temp dir goes with it, however the session ends."""

    @pytest.fixture
    def hooked(self, tmp_path, repo, feat):
        return make_engine(
            [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
            repo_root=str(repo),
            provider=hooked_fake_agent_provider(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_an_explicit_stop_removes_the_hooks_dir(self, hooked, feat):
        try:
            await hooked.spawn_agent(str(feat))
            directory = hooks_dir(hooked.get_session(str(feat), "a1"))
            assert directory.is_dir()

            hooked.stop_agent(str(feat), "a1")

            assert not directory.exists()
        finally:
            await hooked.stop()

    @pytest.mark.asyncio
    async def test_exiting_removes_the_hooks_dir(self, hooked, feat):
        events = Recorder(hooked)
        try:
            await hooked.spawn_agent(str(feat))
            session = hooked.get_session(str(feat), "a1")
            directory = hooks_dir(session)
            await _until(lambda: "your turn>" in _shown(session))

            await hooked.send_agent_input(str(feat), "a1", "exit 0")
            await _until(lambda: events.named("agent.exited"))

            assert not directory.exists()
        finally:
            await hooked.stop()

    @pytest.mark.asyncio
    async def test_stopping_the_engine_removes_every_hooks_dir(self, hooked, repo, feat):
        await hooked.spawn_agent(str(feat))
        await hooked.spawn_agent(str(repo))
        directories = [
            hooks_dir(hooked.get_session(str(feat), "a1")),
            hooks_dir(hooked.get_session(str(repo), "a1")),
        ]

        await hooked.stop()

        assert [d.exists() for d in directories] == [False, False]

    def test_a_session_that_never_started_cleans_up_too(self, tmp_path):
        observer = MagicMock()
        AgentSession("unused", worktree_path=str(tmp_path), observer=observer).stop()
        observer.cleanup.assert_called_once_with()


class TestClientEnv:
    """The agent starts from the spawning client's environment (D5)."""

    VAR = "LAZYAGENT_TEST_CLIENT_VAR"

    @pytest.mark.asyncio
    async def test_the_client_env_reaches_the_child(self, running, feat, monkeypatch):
        monkeypatch.setenv(self.VAR, "from-engine")
        monkeypatch.setenv("LAZYAGENT_TEST_ENGINE_ONLY", "engine")
        client_env = {"PATH": os.environ["PATH"], self.VAR: "from-client"}

        await running.spawn_agent(
            str(feat), f"--env={self.VAR}", env=client_env
        )
        await running.spawn_agent(str(feat), "--env=LAZYAGENT_TEST_ENGINE_ONLY", env=client_env)

        first = running.get_session(str(feat), "a1")
        second = running.get_session(str(feat), "a2")
        await _until(lambda: "your turn>" in _shown(first) and "your turn>" in _shown(second))
        assert f"env {self.VAR}=from-client" in _shown(first)
        assert "env LAZYAGENT_TEST_ENGINE_ONLY=<unset>" in _shown(second)

    @pytest.mark.asyncio
    async def test_without_a_client_env_the_engine_env_is_used(self, running, feat, monkeypatch):
        monkeypatch.setenv(self.VAR, "from-engine")

        await running.spawn_agent(str(feat), f"--env={self.VAR}")

        session = running.get_session(str(feat), "a1")
        await _until(lambda: "your turn>" in _shown(session))
        assert f"env {self.VAR}=from-engine" in _shown(session)

    @pytest.mark.asyncio
    async def test_the_shell_gets_the_client_env_too(self, running, feat):
        shell = running.open_shell(
            str(feat), env={"PATH": os.environ["PATH"], self.VAR: "shell-client"}
        )
        await asyncio.sleep(0.3)
        shell.write(f"echo VALUE=${self.VAR}\n")
        await _until(lambda: "VALUE=shell-client" in _shown(shell))


class TestShells:
    @pytest.mark.asyncio
    async def test_open_shell_is_get_or_create(self, running, feat):
        shell = running.open_shell(str(feat))
        assert shell.kind == "shell"
        assert shell.running
        assert running.open_shell(str(feat)) is shell
        assert running.get_shell(str(feat)) is shell

        running.write_input(shell, "pwd\n")
        await _until(lambda: str(feat) in _shown(shell))

    @pytest.mark.asyncio
    async def test_an_exited_shell_is_replaced(self, running, feat):
        shell = running.open_shell(str(feat))
        running.write_input(shell, "exit\n")
        await _until(lambda: running.get_shell(str(feat)) is None)

        again = running.open_shell(str(feat))
        assert again is not shell and again.running


class TestRemoveWorktree:
    @pytest.mark.asyncio
    async def test_remove_cleans_up_agents_and_shell(self, running, feat):
        events = Recorder(running)
        shell = running.open_shell(str(feat))
        await running.spawn_agent(str(feat))
        agent = running.get_session(str(feat), "a1")
        # A finished agent does not block the removal.
        running.registry.update_status(
            str(feat), "a1", AgentStatus.COMPLETED, agent.confidence
        )

        assert await running.remove_worktree(str(feat), force=True) is None

        assert running.world.removed == [(str(feat), True)]
        assert not agent.running
        assert not shell.running
        assert running.get_shell(str(feat)) is None
        assert running.agent_ids(str(feat)) == []
        assert running.agent_states(str(feat)) == {}
        assert [e["agent_id"] for e in events.named("agent.exited")] == ["a1"]
        assert running.worktree(str(feat)) is None
        assert "worktrees.changed" in events.names()

    @pytest.mark.asyncio
    async def test_reports_agents_known_only_to_the_registry_as_exited(self, running, feat):
        running.registry.update_status(
            str(feat), "a1", AgentStatus.COMPLETED, running.registry.get(str(feat), "a1").confidence
        )
        cursor = running.events.cursor

        await running.remove_worktree(str(feat))

        events, _ = running.events.since(cursor)
        assert [(e.worktree_path, e.agent_id, e.status) for e in events] == [
            (str(feat), "a1", "exited")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [AgentStatus.RUNNING, AgentStatus.WAITING])
    async def test_an_active_agent_blocks_it(self, running, feat, status):
        running.registry.update_status(
            str(feat), "a1", status, running.registry.get(str(feat), "a1").confidence
        )
        with pytest.raises(ValueError, match="stop it first"):
            await running.remove_worktree(str(feat))
        assert running.world.removed == []

    @pytest.mark.asyncio
    async def test_main_and_unknown_are_rejected(self, running, repo, tmp_path):
        with pytest.raises(ValueError, match="main worktree"):
            await running.remove_worktree(str(repo))
        with pytest.raises(ValueError, match="Unknown worktree"):
            await running.remove_worktree(str(tmp_path / "nowhere"))


def _custom_engine(tmp_path, repo, feat, *, create=None, remove=None) -> Engine:
    world = World([worktree(str(repo)), worktree(str(feat), "feature/demo", is_main=False)])
    return make_engine(
        repo_root=str(repo),
        world=world,
        config=Config(worktree=WorktreeConfig(create=create, remove=remove)),
        provider=fake_agent_provider(tmp_path),
    )


class TestWorktreeJobs:
    """Custom create/remove commands run as engine jobs in the repo root."""

    @pytest.mark.asyncio
    async def test_custom_create_expands_the_template_and_streams_output(
        self, tmp_path, repo, feat
    ):
        engine = _custom_engine(
            tmp_path, repo, feat,
            create="echo create {branch} {name} {base} {path} {repo} {extra}; pwd",
        )
        events = Recorder(engine)

        created = await engine.create_worktree("feature/demo", "main", "--no-build --skip-hooks")

        predicted = str(repo.parent / "repo-feature/demo")
        assert (created.path, created.branch) == (predicted, "feature/demo")
        assert created.job_id == "j1"
        output = "".join(e["text"] for e in events.named("job.output"))
        assert output == (
            f"create feature/demo repo-feature/demo main {predicted} {repo} "
            f"--no-build --skip-hooks\n{repo}\n"
        )
        started = events.named("job.started")[0]
        assert started["title"] == "Create worktree feature/demo"
        assert events.named("job.finished") == [{"job_id": "j1", "exit_code": 0}]
        # Re-listed once the job is done.
        assert events.names().index("worktrees.changed") > events.names().index("job.finished")

    @pytest.mark.asyncio
    async def test_custom_create_without_extra(self, tmp_path, repo, feat):
        engine = _custom_engine(
            tmp_path, repo, feat, create="echo create {branch} {name} {base} {path} {repo} {extra}"
        )
        events = Recorder(engine)

        await engine.create_worktree("feature/demo", "main")

        predicted = str(repo.parent / "repo-feature/demo")
        assert "".join(e["text"] for e in events.named("job.output")) == (
            f"create feature/demo repo-feature/demo main {predicted} {repo}\n"
        )

    @pytest.mark.asyncio
    async def test_failing_create_reports_its_exit_code_and_output(self, tmp_path, repo, feat):
        engine = _custom_engine(
            tmp_path, repo, feat, create="echo partial; echo boom >&2; exit 3"
        )
        events = Recorder(engine)

        with pytest.raises(WorktreeManagerError, match="Custom create command failed: boom"):
            await engine.create_worktree("feat2")

        assert events.named("job.finished") == [{"job_id": "j1", "exit_code": 3}]
        assert {e["text"] for e in events.named("job.output")} == {"partial\n", "boom\n"}
        assert "worktrees.changed" not in events.names()

    @pytest.mark.asyncio
    async def test_custom_remove_expands_force_and_cleans_up(self, tmp_path, repo, feat):
        engine = _custom_engine(
            tmp_path, repo, feat, remove="echo remove {name} {path} {branch} {force}"
        )
        events = Recorder(engine)
        shell = engine.open_shell(str(feat))
        try:
            job_id = await engine.remove_worktree(str(feat), force=True)
        finally:
            await engine.stop()

        assert job_id == "j1"
        assert "".join(e["text"] for e in events.named("job.output")) == (
            f"remove repo-feat {feat} feature/demo --force\n"
        )
        assert events.named("job.finished") == [{"job_id": "j1", "exit_code": 0}]
        # A custom command removes the worktree its own way.
        assert engine.world.removed == []
        assert not shell.running

    @pytest.mark.asyncio
    async def test_custom_remove_without_force(self, tmp_path, repo, feat):
        engine = _custom_engine(tmp_path, repo, feat, remove="echo remove {name} {force}")
        events = Recorder(engine)

        await engine.remove_worktree(str(feat))

        assert "".join(e["text"] for e in events.named("job.output")) == "remove repo-feat\n"

    @pytest.mark.asyncio
    async def test_failing_remove_keeps_everything(self, tmp_path, repo, feat):
        engine = _custom_engine(tmp_path, repo, feat, remove="exit 4")
        events = Recorder(engine)
        shell = engine.open_shell(str(feat))
        try:
            with pytest.raises(WorktreeManagerError, match="exit code 4"):
                await engine.remove_worktree(str(feat))
            assert shell.running
            assert engine.worktree(str(feat)) is not None
        finally:
            await engine.stop()
        assert events.named("job.finished") == [{"job_id": "j1", "exit_code": 4}]

    @pytest.mark.asyncio
    async def test_a_client_env_reaches_the_job(self, tmp_path, repo, feat, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_TEST_JOB_VAR", "from-engine")
        engine = _custom_engine(
            tmp_path, repo, feat, create="echo var=$LAZYAGENT_TEST_JOB_VAR"
        )
        events = Recorder(engine)

        await engine.create_worktree(
            "a", env={"PATH": os.environ["PATH"], "LAZYAGENT_TEST_JOB_VAR": "from-client"}
        )
        await engine.create_worktree("b")

        assert [e["text"] for e in events.named("job.output")] == [
            "var=from-client\n",
            "var=from-engine\n",
        ]

    @pytest.mark.asyncio
    async def test_the_methods_pass_custom_and_the_client_env(self, tmp_path, repo, feat):
        """create_worktree / remove_worktree as clients call them: a remote
        TUI asks for the custom remove and sends its env, like the in-process
        one does."""
        from lazyagent.engine.methods import EngineMethods

        engine = _custom_engine(
            tmp_path, repo, feat,
            create="echo create=$LAZYAGENT_TEST_JOB_VAR",
            remove="echo remove=$LAZYAGENT_TEST_JOB_VAR {force}",
        )
        methods = EngineMethods(engine)
        events = Recorder(engine)
        env = {"PATH": os.environ["PATH"], "LAZYAGENT_TEST_JOB_VAR": "client"}

        created = await methods.create_worktree({"branch": "x", "env": env})
        removed = await methods.remove_worktree(
            {"worktree_path": str(feat), "force": True, "custom": True, "env": env}
        )

        assert created["job_id"] == "j1"
        assert removed == {"removed": str(feat), "job_id": "j2"}
        assert [e["text"] for e in events.named("job.output")] == [
            "create=client\n",
            "remove=client --force\n",
        ]
        validation.validate_result("remove_worktree", removed, strict=True)
        with pytest.raises(ValueError, match="custom must be a boolean"):
            await methods.remove_worktree({"worktree_path": str(feat), "custom": "yes"})

    @pytest.mark.asyncio
    async def test_custom_false_uses_git(self, tmp_path, repo, feat):
        engine = _custom_engine(tmp_path, repo, feat, remove="exit 4")
        await engine.remove_worktree(str(feat), custom=False)
        assert engine.world.removed == [(str(feat), False)]

    @pytest.mark.asyncio
    async def test_plain_create_uses_git(self, tmp_path, repo, feat):
        engine = _custom_engine(tmp_path, repo, feat)
        events = Recorder(engine)

        created = await engine.create_worktree(" feat2 ", "develop")

        assert engine.world.created == [("feat2", "develop")]
        assert created.job_id is None
        assert engine.worktree(created.path) is not None
        assert "job.started" not in events.names()

    @pytest.mark.asyncio
    async def test_a_job_that_cannot_start_still_finishes(self, tmp_path, repo, feat):
        engine = _custom_engine(tmp_path, repo, feat)
        events = Recorder(engine)

        with pytest.raises(OSError):
            await engine.run_job("true", cwd=str(tmp_path / "missing"))

        assert events.named("job.finished") == [{"job_id": "j1", "exit_code": None}]


class TestEvents:
    @pytest.mark.asyncio
    async def test_one_global_seq_and_list_events_sees_agent_status_only(
        self, running, feat
    ):
        events = Recorder(running)
        running.notice("information", "hello")
        await running.spawn_agent(str(feat))
        await running.run_job("echo hi")
        running.stop_agent(str(feat), "a1")
        running.refresh_worktrees()

        seqs = [e.seq for e in events.events]
        assert seqs == list(range(1, len(seqs) + 1))
        assert running.events.cursor == seqs[-1]
        assert set(events.names()) >= {
            "engine.notice", "agent.spawned", "agent.status", "agent.updated",
            "job.started", "job.output", "job.finished", "agent.exited",
            "worktrees.changed",
        }

        listed, truncated = running.events.since(0)
        assert not truncated
        assert [e.seq for e in listed] == [
            e.seq for e in events.events if e.event == "agent.status"
        ]
        assert [e.status for e in listed] == ["running", "exited"]
        assert [e.seq for e in running.events.history(0)] == seqs

    @pytest.mark.asyncio
    async def test_subscribe_filters_and_unsubscribes(self, running):
        seen = []
        unsubscribe = running.subscribe(seen.append, ["engine.notice"])
        running.notice("warning", "one")
        await running.run_job("true")
        unsubscribe()
        running.notice("error", "two")
        assert [e.data for e in seen] == [{"severity": "warning", "message": "one"}]

    def test_bad_severity_is_rejected(self, engine):
        with pytest.raises(ValueError):
            engine.notice("fatal", "nope")

    def test_a_failing_rescan_is_a_notice(self, engine):
        events = Recorder(engine)
        engine.world.list_error = WorktreeManagerError("not a git repository")

        assert engine.refresh_worktrees() is False

        assert events.named("engine.notice") == [
            {"severity": "error", "message": "not a git repository"}
        ]

    @pytest.mark.asyncio
    async def test_detail_only_changes_are_updates_not_transitions(self, running, feat, monkeypatch):
        no_pty(monkeypatch)
        events = Recorder(running)
        await running.spawn_agent(str(feat))
        session = running.get_session(str(feat), "a1")
        session._set_status(AgentStatus.RUNNING)
        session._set_status(AgentStatus.RUNNING, detail="Bash")
        session._set_status(AgentStatus.RUNNING, detail="Read")

        assert [e["detail"] for e in events.named("agent.updated")] == ["", "Bash", "Read"]
        assert [e["status"] for e in events.named("agent.status")] == ["running"]

    @pytest.mark.asyncio
    async def test_worktree_service_changes_become_protocol_events(self, engine):
        events = Recorder(engine)
        path = engine.worktrees[1].path
        engine.world.statuses[path] = GitStatus(dirty_count=2)

        await engine.refresh_git_status([path])

        assert events.named("git_status.updated") == [
            {
                path: {
                    "dirty_count": 2, "ahead": 0, "behind": 0,
                    "has_upstream": False, "last_commit_subject": "",
                }
            }
        ]


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_lists_loads_config_and_runs_the_timers(self, tmp_path, repo, feat):
        engine = make_engine(
            [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
            repo_root=str(repo),
            config=Config(default_branch="develop"),
        )
        engine._poll_interval = engine._hang_interval = 0.01
        session = MagicMock()
        session.worktree_path, session.agent_id = str(feat), "a1"
        session.status = AgentStatus.RUNNING
        add_session(engine, session)
        events = Recorder(engine)

        await engine.start(ipc=False)
        try:
            await _until(lambda: session.poll.called and session.check_hang.called)
            assert events.names()[0] == "worktrees.changed"
            assert engine.get_config().default_branch == "develop"
        finally:
            await engine.stop()

    @pytest.mark.asyncio
    async def test_stop_kills_everything_quietly(self, running, feat):
        await running.spawn_agent(str(feat))
        shell = running.open_shell(str(feat))
        agent = running.get_session(str(feat), "a1")
        events = Recorder(running)

        await running.stop()

        assert not agent.running and not shell.running
        assert "agent.exited" not in events.names()

    @pytest.mark.asyncio
    async def test_ipc_server_speaks_for_the_engine(self, tmp_path, repo, feat):
        engine = make_engine(
            [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
            repo_root=str(repo),
            provider=fake_agent_provider(tmp_path),
        )
        await engine.start()
        try:
            assert engine.socket_path and os.path.exists(engine.socket_path)

            async def call(method, **params):
                reader, writer = await asyncio.open_unix_connection(engine.socket_path)
                request = {"id": "1", "method": method, "params": params}
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                return response

            listed = await call("list_worktrees")
            assert [w["path"] for w in listed["result"]] == [str(repo), str(feat)]
            spawned = await call("spawn_agent", worktree_path=str(feat))
            assert spawned["result"]["agent_id"] == "a1"
            assert engine.get_session(str(feat), "a1").running
            stopped = await call("stop_agent", worktree_path=str(feat))
            assert stopped["result"]["status"] == "stopped"
        finally:
            socket_path = engine.socket_path
            await engine.stop()
        assert not os.path.exists(socket_path)
