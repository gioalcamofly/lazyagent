"""The v1 schemas and the stdlib validator."""

from __future__ import annotations

import pytest

from lazyagent.protocol import EVENTS, validation
from lazyagent.protocol.validation import SchemaValidationError, validate

# One representative payload per §4e event.
EVENT_SAMPLES = {
    "agent.status": {
        "seq": 3, "time": 1700000000.5, "worktree_path": "/r", "agent_id": "a1",
        "label": "Agent 1", "status": "waiting", "previous_status": "running", "detail": "",
    },
    "agent.updated": {
        "worktree_path": "/r", "agent_id": "a1", "status": "running", "confidence": "high",
        "detail": "Bash",
    },
    "agent.spawned": {
        "worktree_path": "/r", "agent_id": "a2", "session_id": "s-7", "label": "Agent 2",
        "session_name": "r-a2", "kind": "agent",
    },
    "agent.exited": {
        "worktree_path": "/r", "agent_id": "a2", "session_id": "s-7", "exit_code": 0,
        "stopped": False,
    },
    "worktrees.changed": [
        {
            "path": "/r", "branch": None, "head": "abc", "is_main": True, "is_bare": False,
            "agent_status": "no_agent", "agents": [],
        }
    ],
    "git_status.updated": {
        "/r": {"dirty_count": 1, "ahead": 0, "behind": 2, "has_upstream": True,
               "last_commit_subject": "fix"},
    },
    "pr_info.updated": {
        "path": "/r",
        "pr_info": {
            "number": 12, "title": "t", "state": "OPEN", "url": "https://x/12",
            "review_decision": "", "mergeable": "MERGEABLE",
            "checks": [{"name": "ci", "status": "COMPLETED", "conclusion": "success"}],
        },
    },
    "stream.resized": {"stream_id": 1, "rows": 50, "cols": 200, "offset": 4096},
    "stream.reset": {"stream_id": 1, "rows": 50, "cols": 200, "offset": 8192},
    "engine.notice": {"severity": "warning", "message": "gh is not installed"},
    "job.started": {"job_id": "j1", "title": "Create worktree feat", "command": "make wt"},
    "job.output": {"job_id": "j1", "text": "Preparing worktree\n"},
    "job.finished": {"job_id": "j1", "exit_code": None},
    "engine.shutdown": {"reason": "lazyagent engine stop"},
}


def test_all_schema_files_load_with_supported_keywords():
    assert validation.schema_files() == [
        "envelope.json", "events.json", "handshake.json", "methods.json",
    ]
    for name in validation.schema_files():
        validation.load_schema(name)


def test_events_constant_matches_schema():
    assert set(EVENTS) == set(validation.event_names()) == set(EVENT_SAMPLES)


@pytest.mark.parametrize("event", sorted(EVENT_SAMPLES))
def test_event_samples_are_valid(event):
    validation.validate_event(event, EVENT_SAMPLES[event], strict=True)


def test_pr_info_may_be_null():
    validation.validate_event("pr_info.updated", {"path": "/r", "pr_info": None}, strict=True)


@pytest.mark.parametrize(
    ("event", "data", "match"),
    [
        ("agent.status", {**EVENT_SAMPLES["agent.status"], "status": "dancing"}, "anyOf"),
        ("agent.exited", {"worktree_path": "/r", "agent_id": "a"}, "session_id"),
        ("git_status.updated", {"/r": {"dirty_count": "1", "ahead": 0, "behind": 0}}, "integer"),
        ("stream.resized", {"stream_id": 1, "rows": 0, "cols": 80, "offset": 0}, "minimum"),
        ("engine.notice", {"severity": "loud", "message": ""}, "not one of"),
    ],
)
def test_invalid_events_are_rejected(event, data, match):
    with pytest.raises(SchemaValidationError, match=match):
        validation.validate_event(event, data, strict=True)


def test_errors_point_at_the_offending_field():
    with pytest.raises(SchemaValidationError) as info:
        validation.validate_result(
            "list_agents",
            {"worktree_path": "/r", "agents": [{"agent_id": "a1", "label": "", "session_name": "",
                                                 "status": "running", "confidence": "sure", "detail": ""}]},
            strict=True,
        )
    assert info.value.path == "$.agents[0].confidence"


def test_lenient_mode_ignores_unknown_fields_strict_mode_does_not():
    data = {"worktree_path": "/r", "agents": [], "new_field": 1}
    validation.validate_result("list_agents", data)
    with pytest.raises(SchemaValidationError, match="not declared"):
        validation.validate_result("list_agents", data, strict=True)


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("get_agent_status", {"worktree_path": "/r", "agent_id": "a1", "status": "dancing",
                              "confidence": "sure", "detail": ""}),
        ("send_agent_input", {"worktree_path": "/r", "agent_id": "a1", "status": "sent",
                              "status_before": "dancing", "cursor": 0, "delivery": "queued"}),
    ],
)
def test_open_enums_take_any_string_on_the_wire_but_not_in_strict_mode(method, result):
    validation.validate_result(method, result)
    with pytest.raises(SchemaValidationError, match="not a known value"):
        validation.validate_result(method, result, strict=True)


def test_open_enums_still_require_a_string():
    with pytest.raises(SchemaValidationError, match="string"):
        validation.validate_result(
            "get_agent_status",
            {"worktree_path": "/r", "agent_id": "a1", "status": 3, "detail": ""},
        )


def test_unknown_event_status_is_tolerated_on_the_wire():
    validation.validate_event("agent.status", {**EVENT_SAMPLES["agent.status"], "status": "dancing"})


def test_v1_additions_are_optional_and_typed():
    validation.validate_params(
        "spawn_agent",
        {"worktree_path": "/r", "kind": "orchestrator", "env": {"PATH": "/bin"}},
        strict=True,
    )
    with pytest.raises(SchemaValidationError, match="string"):
        validation.validate_params("spawn_agent", {"worktree_path": "/r", "env": {"N": 1}})


@pytest.mark.parametrize(
    ("value", "schema", "ok"),
    [
        (True, {"type": "integer"}, False),
        (1, {"type": "number"}, True),
        (1.0, {"const": 1}, True),
        (True, {"const": 1}, False),
        (None, {"type": ["string", "null"]}, True),
        ([1, "a"], {"type": "array", "items": {"type": "integer"}}, False),
        ([], {"type": "array", "minItems": 1}, False),
        ("x", {"oneOf": [{"type": "string"}, {"minLength": 1}]}, False),
        (3, {"oneOf": [{"type": "string"}, {"type": "integer"}]}, True),
        ({"a": 1}, {"additionalProperties": {"type": "string"}}, False),
        ({}, False, False),
    ],
)
def test_validator_keywords(value, schema, ok):
    if ok:
        validate(value, schema, doc="methods.json")
    else:
        with pytest.raises(SchemaValidationError):
            validate(value, schema, doc="methods.json")


def test_unsupported_keywords_are_refused(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text('{"$defs": {"x": {"type": "string", "pattern": "^a"}}}')
    monkeypatch.setattr(validation, "SCHEMA_DIR", tmp_path)
    validation.load_schema.cache_clear()
    try:
        with pytest.raises(ValueError, match="unsupported schema keywords.*pattern"):
            validation.load_schema("bad.json")
    finally:
        validation.load_schema.cache_clear()
