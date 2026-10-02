"""
Online gating tests against the live Latch Registry.

These tests are marked `requires_latch_registry` and xfail offline (see
`tests/conftest.py`). They run for real when a Latch session is available.
"""

from pathlib import Path

import pytest

from tests.constants import FULCRUM_WORKSPACE_ID
from tests.constants import MOCK_TABLE_SAMPLES_ID
from tests.constants import SOFT_DELETE_FIXTURE_TABLE_ID


@pytest.mark.requires_latch_registry
def test_small_real_table_exports(tmp_path: Path) -> None:
    from latch_registry_export.api import export
    from latch_registry_export.api import load_report
    from latch_registry_export.config import TableConfig

    out = tmp_path / "r.duckdb"
    export([TableConfig(id=MOCK_TABLE_SAMPLES_ID)], output=out, page_size=50)
    report = load_report(out)

    assert sum(report.row_counts.values()) > 0


@pytest.mark.requires_latch_registry
def test_soft_deleted_records_excluded() -> None:
    """
    `fetch_table_records` must not yield soft-deleted records (name-PK invariant).

    Table `SOFT_DELETE_FIXTURE_TABLE_ID` holds 4 records: `Sample 0` (straight
    soft-deleted), `Sample 1` (soft-deleted) and a second, live `Sample 1` that
    reuses the name, and live `Sample 2`. Only the two live records must
    surface, with the reused name deduped to its live record. This table is a
    fixed external fixture in the Fulcrum Latch workspace; editing it there
    would change the expected values below.
    """
    from fglatch.registry import fetch_table_records

    names = [
        record.get_name(load_if_missing=False)
        for record in fetch_table_records(SOFT_DELETE_FIXTURE_TABLE_ID)
    ]

    assert None not in names
    assert len(names) == len(set(names)), "duplicate names imply soft-deleted leakage"
    assert set(names) == {"Sample 1", "Sample 2"}


@pytest.mark.requires_latch_registry
def test_soft_delete_fixture_table_exports(tmp_path: Path) -> None:
    """Exporting the soft-delete fixture table writes only its 2 live rows."""
    from latch_registry_export.api import export
    from latch_registry_export.api import load_report
    from latch_registry_export.config import TableConfig

    out = tmp_path / "soft_delete_fixture.duckdb"
    export([TableConfig(id=SOFT_DELETE_FIXTURE_TABLE_ID)], output=out, overwrite=True)
    report = load_report(out)

    (row_count,) = report.row_counts.values()
    assert row_count == 2
    assert not report.issue_counts
    assert not report.degraded_links


@pytest.mark.requires_latch_registry
def test_generated_config_loads_and_lists_every_column(tmp_path: Path) -> None:
    """The Fulcrum workspace's config loads, lists each table's columns, and resolves."""
    from latch.registry.table import Table

    from latch_registry_export.config import load_table_configs
    from latch_registry_export.dependencies import resolve_dependencies
    from latch_registry_export.generate import discover_tables
    from latch_registry_export.generate import to_config_toml
    from latch_registry_export.schema_builder import build_schemas

    tables = discover_tables(workspace_id=FULCRUM_WORKSPACE_ID)
    path = tmp_path / "tables.toml"
    path.write_text(to_config_toml(tables, workspace_id=FULCRUM_WORKSPACE_ID), encoding="utf-8")
    configs = load_table_configs(path)

    assert [c.id for c in configs] == [t.id for t in tables]
    for config in configs:
        expected_keys = tuple(Table(id=config.id).get_columns() or {})
        assert (config.include_columns or ()) == expected_keys, config.id

    # `export` accepts the unedited config: names, column types, and link targets resolve.
    resolve_dependencies(build_schemas(configs))
