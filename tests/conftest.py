"""Pytest fixtures, including online-API gating that xfails when unauthenticated."""

from __future__ import annotations

import functools

import pytest


@functools.cache
def _latch_registry_is_available() -> bool:
    try:
        from latch.registry.table import Table

        from tests.constants import MOCK_TABLE_SAMPLES_ID

        Table(id=MOCK_TABLE_SAMPLES_ID).get_columns()
    except Exception:  # noqa: BLE001 - any failure means "no live registry"
        return False
    return True


@pytest.fixture(autouse=True)
def check_latch_registry_connection(request: pytest.FixtureRequest) -> None:
    """Xfail tests marked `requires_latch_registry` when no live registry is reachable."""
    if (
        request.node.get_closest_marker("requires_latch_registry")
        and not _latch_registry_is_available()
    ):
        pytest.xfail("no Latch Registry connection")
