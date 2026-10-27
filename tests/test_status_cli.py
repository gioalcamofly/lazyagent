"""``lazyagent status`` without a real engine: its dispatch, output and the
errors it explains (the real-process run is in test_engine_cli.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazyagent import cli
from lazyagent.engine import cli as engine_cli
from lazyagent.engine import launcher
from lazyagent.engine.client import RequestError
from lazyagent.protocol.envelope import ErrorInfo

RESULT = {
    "rows": [
        {
            "key": "repo:feat", "branch": "feat", "level": None, "ball": "agent",
            "state": "working", "changed_at": 1_700_000_000.0, "pr": None,
        }
    ],
    "counts": {"now": 0, "round": 0, "in_flight": 1, "done": 0, "stale": 0, "not_started": 0},
    "generated_at": 1_700_000_000.0,
}


@pytest.fixture
def engine_at(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "resolve_repo_root", lambda repo: str(tmp_path))
    monkeypatch.setattr(launcher, "find", lambda root: Path(tmp_path / "engine.sock"))
    calls = []

    def answer(response):
        async def request(socket, method, **params):
            calls.append((method, params))
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(engine_cli, "_request", request)

    answer.calls = calls
    return answer


def test_top_level_status_runs_the_status_command(engine_at, capsys):
    engine_at(RESULT)
    with pytest.raises(SystemExit) as code:
        cli.main(["status", "--all"])
    assert code.value.code == 0
    assert engine_at.calls == [("status_list", {"include_done": True})]
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("NOW 0   ROUND 0   IN FLIGHT 1")
    assert out[2].split()[:4] == ["-", "feat", "agent", "working"]


def test_json_prints_the_engine_answer(engine_at, capsys):
    engine_at(RESULT)
    assert engine_cli.status_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == RESULT
    assert engine_at.calls == [("status_list", {"include_done": False})]


def test_an_engine_without_status_list_is_told_to_restart(engine_at, capsys):
    engine_at(RequestError(ErrorInfo("UNKNOWN_METHOD", "Unknown method: status_list")))
    assert engine_cli.status_main([]) == 1
    assert "restart it" in capsys.readouterr().err


def test_no_engine_exits_3(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, "resolve_repo_root", lambda repo: str(tmp_path))
    monkeypatch.setattr(launcher, "find", lambda root: None)
    assert engine_cli.status_main([]) == engine_cli.NOT_RUNNING == 3
    assert "no engine running" in capsys.readouterr().err


APPROVAL = {"key": "repo:feat", "branch": "feat", "pr": 7, "sha": "a" * 40, "at": 1.0,
            "source": "cli", "note": "", "agent_id": None, "has_previous": False}


def test_pr_approve_asks_owner_approve_from_the_cli(engine_at, capsys):
    engine_at(APPROVAL)
    with pytest.raises(SystemExit) as code:
        cli.main(["pr", "approve", "#7", "--sha", "aaaaaaa"])
    assert code.value.code == 0
    assert engine_at.calls == [("owner_approve", {"key": "#7", "source": "cli", "note": "",
                                                  "sha": "aaaaaaa"})]
    assert capsys.readouterr().out == "repo:feat: approved #7 at aaaaaaa\n"


def test_pr_approve_revoke_and_its_errors(engine_at, capsys):
    engine_at({**APPROVAL, "revoked": True, "standing": None})
    assert engine_cli.pr_main(["approve", "feat", "--revoke"]) == 0
    assert engine_at.calls[-1] == ("owner_approval_revoke", {"key": "feat"})
    assert "approval of aaaaaaa taken back" in capsys.readouterr().out
    engine_at(RequestError(ErrorInfo("VALIDATION_ERROR", "repo:feat has no approval")))
    assert engine_cli.pr_main(["approve", "feat", "--revoke"]) == 1
    assert "lazyagent pr: repo:feat has no approval" in capsys.readouterr().err
    assert engine_cli.pr_main([]) == 2


def test_why_shows_the_standing_approval(engine_at, capsys):
    from lazyagent.engine.rules.export import format_explain

    result = {"key": "repo:feat", "rule": None, "above": [], "facts": []}
    assert "owner" not in format_explain(result)
    result = {"key": "repo:feat", "rule": {"id": "x", "origin": "global", "when": "true",
                                           "file": "rules.toml", "line": 3},
              "then": {"ball": "you", "state": "s"}, "above": [], "facts": [],
              "owner_approval": {**APPROVAL, "text": "you approved aaaaaaa (cli): the PR's head"}}
    text = format_explain(result)
    assert "  owner  you approved aaaaaaa (cli): the PR's head" in text
    assert "lazyagent pr approve repo:feat --revoke" in text
