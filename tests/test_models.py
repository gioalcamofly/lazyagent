from __future__ import annotations

from lazyagent.models import AgentState, AgentStatus, CiCheck, GitStatus, PrInfo, WorktreeInfo


def _make_worktree(**kwargs) -> WorktreeInfo:
    defaults = dict(
        path="/home/user/repo",
        head="abcdef1234567890abcdef1234567890abcdef12",
        branch="main",
        is_main=False,
        is_bare=False,
    )
    defaults.update(kwargs)
    return WorktreeInfo(**defaults)


class TestName:
    def test_returns_dirname(self):
        wt = _make_worktree(path="/home/user/repo-PROJ-10761")
        assert wt.name == "repo-PROJ-10761"

    def test_returns_dirname_for_root(self):
        wt = _make_worktree(path="/home/user/repo")
        assert wt.name == "repo"


class TestTicketId:
    def test_extracts_ticket_from_branch(self):
        wt = _make_worktree(branch="PROJ-11067-Remove-Feature-Flags")
        assert wt.ticket_id == "PROJ-11067"

    def test_extracts_ticket_with_prefix(self):
        wt = _make_worktree(branch="feat/PROJ-12413-some-feature")
        assert wt.ticket_id == "PROJ-12413"

    def test_no_ticket_in_branch(self):
        wt = _make_worktree(branch="fix-prompt-fixture-reference-date-ordering")
        assert wt.ticket_id is None

    def test_no_ticket_when_detached(self):
        wt = _make_worktree(branch=None)
        assert wt.ticket_id is None


class TestDisplayLabel:
    def test_main_worktree(self):
        wt = _make_worktree(is_main=True, branch="fix-something")
        assert wt.display_label == "(main)"

    def test_ticket_branch(self):
        wt = _make_worktree(branch="PROJ-10761-fix-nested-validators")
        assert wt.display_label == "PROJ-10761"

    def test_no_ticket_branch(self):
        wt = _make_worktree(branch="fix-service-choice-playbook-switch")
        assert wt.display_label == "fix-service-choice-playbook-switch"

    def test_detached_head(self):
        wt = _make_worktree(branch=None, path="/home/user/my-worktree")
        assert wt.display_label == "my-worktree"


class TestDisplayBranch:
    def test_short_branch(self):
        wt = _make_worktree(branch="main")
        assert wt.display_branch == "main"

    def test_long_branch_truncated(self):
        long_name = "PROJ-12413-AIR-Instructions-for-New-Customers-updated-event-is-missing"
        wt = _make_worktree(branch=long_name)
        assert len(wt.display_branch) == 40
        assert wt.display_branch.endswith("...")

    def test_exactly_40_chars_not_truncated(self):
        branch = "a" * 40
        wt = _make_worktree(branch=branch)
        assert wt.display_branch == branch

    def test_detached_head(self):
        wt = _make_worktree(branch=None)
        assert wt.display_branch == "(detached)"


class TestShortHead:
    def test_returns_12_chars(self):
        wt = _make_worktree(head="6e23555e0c62e491fa146ee27457d6999c868b7d")
        assert wt.short_head == "6e23555e0c62"


class TestAgentStatusEnum:
    def test_values(self):
        assert AgentStatus.NO_AGENT.value == "no_agent"
        assert AgentStatus.RUNNING.value == "running"
        assert AgentStatus.WAITING.value == "waiting"
        assert AgentStatus.WAITING_FOR_USER.value == "waiting_for_user"
        assert AgentStatus.WAITING_FOR_APPROVAL.value == "waiting_for_approval"
        assert AgentStatus.COMPLETED.value == "completed"
        assert AgentStatus.FAILED.value == "failed"
        assert AgentStatus.INTERRUPTED.value == "interrupted"
        assert AgentStatus.POSSIBLY_HANGED.value == "possibly_hanged"

    def test_has_expected_members(self):
        assert len(AgentStatus) == 9


class TestAgentState:
    def test_defaults(self):
        state = AgentState()
        assert state.status == AgentStatus.NO_AGENT
        assert state.last_output_time is None

    def test_custom_values(self):
        state = AgentState(status=AgentStatus.RUNNING, last_output_time=123.456)
        assert state.status == AgentStatus.RUNNING
        assert state.last_output_time == 123.456


class TestCiCheck:
    def test_basic_construction(self):
        check = CiCheck(name="build", status="COMPLETED", conclusion="success")
        assert check.name == "build"
        assert check.status == "COMPLETED"
        assert check.conclusion == "success"


class TestPrInfo:
    def test_checks_summary_all_pass(self):
        checks = [
            CiCheck("build", "COMPLETED", "success"),
            CiCheck("lint", "COMPLETED", "success"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.checks_summary == "2/2 passed"

    def test_checks_summary_with_failures(self):
        checks = [
            CiCheck("build", "COMPLETED", "success"),
            CiCheck("lint", "COMPLETED", "failure"),
            CiCheck("test", "COMPLETED", "success"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.checks_summary == "2/3 passed"

    def test_checks_summary_with_pending(self):
        checks = [
            CiCheck("build", "IN_PROGRESS", ""),
            CiCheck("lint", "COMPLETED", "success"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.checks_summary == "1/2 passed"

    def test_checks_summary_no_checks(self):
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=[])
        assert pr.checks_summary == "no checks"

    def test_overall_status_pass(self):
        checks = [
            CiCheck("build", "COMPLETED", "success"),
            CiCheck("lint", "COMPLETED", "success"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.overall_status == "pass"

    def test_overall_status_fail(self):
        checks = [
            CiCheck("build", "COMPLETED", "success"),
            CiCheck("lint", "COMPLETED", "failure"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.overall_status == "fail"

    def test_overall_status_pending(self):
        checks = [
            CiCheck("build", "IN_PROGRESS", ""),
            CiCheck("lint", "COMPLETED", "success"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.overall_status == "pending"

    def test_overall_status_none(self):
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=[])
        assert pr.overall_status == "none"

    def test_checks_summary_uppercase_github_values(self):
        checks = [
            CiCheck("build", "COMPLETED", "SUCCESS"),
            CiCheck("lint", "COMPLETED", "FAILURE"),
            CiCheck("test", "COMPLETED", "SUCCESS"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.checks_summary == "2/3 passed"
        assert pr.overall_status == "fail"

    def test_overall_status_skipped_and_empty_status(self):
        """Real GitHub data: SKIPPED conclusion, and StatusContext with empty status."""
        checks = [
            CiCheck("claude", "COMPLETED", "SKIPPED"),
            CiCheck("build", "COMPLETED", "SUCCESS"),
            CiCheck("ci/jenkins", "", "SUCCESS"),
        ]
        pr = PrInfo(number=42, title="Fix bug", state="OPEN", checks=checks)
        assert pr.checks_summary == "2/3 passed"
        assert pr.overall_status == "pass"


class TestGitStatus:
    def test_defaults(self):
        gs = GitStatus()
        assert gs.dirty_count == 0
        assert gs.ahead == 0
        assert gs.behind == 0
        assert gs.has_upstream is False
        assert gs.last_commit_subject == ""

    def test_custom_values(self):
        gs = GitStatus(
            dirty_count=3,
            ahead=2,
            behind=1,
            has_upstream=True,
            last_commit_subject="Fix bug",
        )
        assert gs.dirty_count == 3
        assert gs.ahead == 2
        assert gs.behind == 1
        assert gs.has_upstream is True
        assert gs.last_commit_subject == "Fix bug"
