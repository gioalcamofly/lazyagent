"""Fixtures for the protocol v1 contract suite.

Every test gets a ``harness`` (see harness.py) for each engine
implementation × transport in harness.IMPLEMENTATIONS / TRANSPORTS.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.contract.harness import IMPLEMENTATIONS, TRANSPORTS, NotSupported

pytestmark = pytest.mark.filterwarnings(
    "error::pytest.PytestUnraisableExceptionWarning"
)


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def implementation(request):
    return IMPLEMENTATIONS[request.param]


@pytest.fixture(params=TRANSPORTS)
def transport(request):
    return request.param


@pytest.fixture
def harness_options():
    """Overridden by tests that need engine options (e.g. queue limits)."""
    return {}


@pytest_asyncio.fixture
async def harness(implementation, transport, harness_options, tmp_path):
    h = implementation(transport, **harness_options)
    await h.start(tmp_path)
    try:
        yield h
    finally:
        await h.stop()


@pytest_asyncio.fixture
async def client(harness):
    try:
        return await harness.connect()
    except NotSupported as e:
        pytest.skip(str(e))
