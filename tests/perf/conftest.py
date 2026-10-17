"""Performance tests (marker ``perf``): local only, never in CI, and out of
the default run (``pyproject.toml`` deselects them). Run them with
``pytest -m perf``; results go to stdout and to ``.perf/results.jsonl``
(the ``record`` fixture, in tests/conftest.py).

Budgets are ui-plan.md §7d-1 (B1-B13); thresholds keep the headroom
§7d-7 gives them, since this machine is shared and noisy.
"""
