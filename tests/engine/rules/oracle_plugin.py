"""``pytest -p tests.engine.rules.oracle_plugin``: every ``status.derive``
call the run makes is checked against the oracle (the old ``derive``). A
mismatch fails the session and is written, with the call count, to
``$LAZYAGENT_ORACLE_REPORT`` (JSON) when set."""

from __future__ import annotations

import json
import os

_seen = {"calls": 0, "mismatches": []}


def pytest_configure(config):
    from lazyagent.engine import status
    from tests.engine.rules import oracle

    real = status.derive

    def checked(facts, now, last_change, *, stalled_hours):
        new = real(facts, now, last_change, stalled_hours=stalled_hours)
        old = oracle.derive(facts, now, last_change, stalled_hours=stalled_hours)
        _seen["calls"] += 1
        if new != old and len(_seen["mismatches"]) < 20:
            _seen["mismatches"].append({"facts": repr(facts), "new": repr(new), "old": repr(old)})
        return new

    status.derive = checked


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("LAZYAGENT_ORACLE_REPORT")
    if path:
        with open(path, "w") as f:
            json.dump(_seen, f, indent=1)
    if _seen["mismatches"]:
        session.exitstatus = 1
