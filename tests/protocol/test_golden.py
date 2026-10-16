"""v1 is a superset of today's IPC protocol.

The golden fixtures are real responses of the legacy ``IpcServer`` (see
golden_cases.py). Each one, with ``type`` added, must be a valid v1
envelope, and its result must match the method's v1 result schema in
strict mode: every field today's server sends is declared and typed.
"""

from __future__ import annotations

import pytest

from lazyagent.ipc import IpcServer
from lazyagent.protocol import ERROR_CODES, LEGACY_METHODS, envelope, validation
from tests.protocol import golden_cases
from tests.test_ipc import _make_app

FIXTURES = golden_cases.load_fixtures()
SUCCESSES = [f for f in FIXTURES if "result" in f["response"]]
ERRORS = [f for f in FIXTURES if "error" in f["response"]]

# Cases whose params break the schema itself (other validation errors, like
# an unknown worktree, are semantic and pass the schema).
SCHEMA_INVALID_PARAMS = {
    "create_worktree/error_empty_branch",
    "list_events/error_bad_cursor",
}


def _ids(fixtures):
    return [f["case"] for f in fixtures]


def test_fixtures_exist_for_every_case():
    assert sorted(_ids(FIXTURES)) == sorted(golden_cases.CASES)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(golden_cases.CASES))
async def test_fixture_matches_current_ipc(name):
    """Regenerate with `uv run python -m tests.protocol.golden_cases`."""
    stored = golden_cases.fixture_path(name).read_text()
    assert golden_cases.dump(await golden_cases.capture(name)) == stored


def test_every_legacy_method_has_a_success_fixture():
    assert {f["method"] for f in SUCCESSES} == set(LEGACY_METHODS)


def test_legacy_methods_match_ipc_and_schemas():
    server = IpcServer(_make_app(), "/tmp/test.sock")
    assert set(LEGACY_METHODS) == set(server._handlers)
    assert set(LEGACY_METHODS) == set(validation.method_names())


@pytest.mark.parametrize("fixture", SUCCESSES, ids=_ids(SUCCESSES))
def test_success_response_is_valid_v1(fixture):
    message = {"type": "res", **fixture["response"]}
    validation.validate_message(message, strict=True)
    parsed = envelope.parse_message(message)
    assert parsed.ok
    validation.validate_result(fixture["method"], parsed.result, strict=True)


@pytest.mark.parametrize("fixture", ERRORS, ids=_ids(ERRORS))
def test_error_response_is_valid_v1(fixture):
    message = {"type": "res", **fixture["response"]}
    validation.validate_message(message, strict=True)
    parsed = envelope.parse_message(message)
    assert parsed.error is not None
    assert parsed.error.code in ERROR_CODES


@pytest.mark.parametrize(
    "fixture",
    [f for f in FIXTURES if f["method"] in LEGACY_METHODS],
    ids=_ids([f for f in FIXTURES if f["method"] in LEGACY_METHODS]),
)
def test_request_is_valid_v1(fixture):
    """Requests match the params schemas, and the schemas already reject the
    params the server answers with a VALIDATION_ERROR for their shape."""
    request = envelope.Request("req-1", fixture["method"], fixture["params"])
    validation.validate_message(request.to_dict(), strict=True)
    if fixture["case"] in SCHEMA_INVALID_PARAMS:
        with pytest.raises(validation.SchemaValidationError):
            validation.validate_params(fixture["method"], fixture["params"])
    else:
        validation.validate_params(fixture["method"], fixture["params"])


def test_error_fixtures_cover_every_legacy_code():
    codes = {f["response"]["error"]["code"] for f in ERRORS}
    assert codes == {
        "UNKNOWN_METHOD",
        "WORKTREE_ERROR",
        "VALIDATION_ERROR",
        "TIMEOUT",
        "INTERNAL_ERROR",
    }


def test_list_events_entries_are_valid_agent_status_events():
    events = [
        e
        for f in SUCCESSES
        if f["method"] == "list_events"
        for e in f["response"]["result"]["events"]
    ]
    assert events, "no list_events fixture carries events"
    for event in events:
        validation.validate_event("agent.status", event, strict=True)


def test_list_worktrees_result_is_a_valid_worktrees_changed_event():
    for f in SUCCESSES:
        if f["method"] == "list_worktrees":
            validation.validate_event("worktrees.changed", f["response"]["result"], strict=True)
