"""
Pytest fixtures, including online-API gating.

Online tests skip unless the selected Latch workspace is the Fulcrum workspace, and
xfail when that workspace is not reachable.
"""

from __future__ import annotations

import functools

import pytest

from tests.constants import FULCRUM_WORKSPACE_ID


@functools.cache
def _selected_workspace() -> str | None:
    try:
        from latch.utils import current_workspace

        return current_workspace()
    except Exception:  # noqa: BLE001 - any failure means "no selected workspace"
        return None


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
    """Gate tests marked `requires_latch_registry` on the Fulcrum workspace and a live registry."""
    if not request.node.get_closest_marker("requires_latch_registry"):
        return
    # Check the workspace first, so no Registry call is made from any other workspace.
    if _selected_workspace() != FULCRUM_WORKSPACE_ID:
        pytest.skip(
            f"online tests run only in the Fulcrum Latch workspace ({FULCRUM_WORKSPACE_ID}); "
            "select it with `latch workspace`"
        )
    if not _latch_registry_is_available():
        pytest.xfail("no Latch Registry connection")
