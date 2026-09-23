"""Tests for `export()`, `ExportReport`, and `load_report()`."""

import datetime as dt
from collections.abc import Iterator
from collections.abc import Mapping
from pathlib import Path

import duckdb
import polars as pl
import pytest

from latch_registry_export.api import export
from latch_registry_export.api import load_report
from latch_registry_export.config import TableConfig
from latch_registry_export.errors import OutputExistsError
from latch_registry_export.schema import ColumnSchema
from latch_registry_export.schema import TableSchema


class _FakeRecord:
    """A minimal stand-in for a pre-primed `latch.registry.record.Record`."""

    def __init__(
        self,
        name: str = "s1",
        values: Mapping[str, object] | None = None,
        last_updated: dt.datetime | None = None,
    ) -> None:
        self._name = name
        self._values = values or {}
        self._last_updated = last_updated or dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)

    def get_name(self, *, load_if_missing: bool = True) -> str:  # noqa: ARG002
        return self._name

    def get_values(self, *, load_if_missing: bool = True) -> dict[str, object]:  # noqa: ARG002
        return dict(self._values)

    def get_last_updated(self, *, load_if_missing: bool = True) -> dt.datetime:  # noqa: ARG002
        return self._last_updated


def _linked_record(name: str) -> object:
    """Build a real, offline-primed `Record` for use as a link cell's raw value."""
    from latch.registry.record import Record

    record = Record(id=f"linked-{name}")
    record._cache.name = name
    return record


def _stub_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub schema build, record fetch, and workspace lookup for a one-table export."""
    name_col = ColumnSchema("__name__", "name", "VARCHAR", pl.Utf8(), False, False, None, None)
    table = TableSchema("1", "samples", "Samples", (name_col,))
    monkeypatch.setattr("latch_registry_export.api.build_schemas", lambda _tables: [table])

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield _FakeRecord()

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)
    monkeypatch.setattr("latch_registry_export.api._workspace_meta", lambda: ("ws", "Test"))


def test_export_refuses_existing_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` raises `OutputExistsError` and leaves the existing file untouched."""
    _stub_schemas(monkeypatch)
    out = tmp_path / "r.duckdb"
    out.write_text("existing")
    with pytest.raises(OutputExistsError):
        export([TableConfig(id="1")], output=out, overwrite=False)
    assert out.read_text() == "existing"


def test_export_writes_and_report_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` writes a DuckDB file that `load_report` can read back."""
    _stub_schemas(monkeypatch)
    out = tmp_path / "r.duckdb"
    export([TableConfig(id="1")], output=out)
    assert out.exists()
    report = load_report(out)
    assert report.row_counts["samples"] == 1
    assert report.workspace_id == "ws"


def test_export_overwrite_replaces_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` with `overwrite=True` atomically replaces an existing output file."""
    _stub_schemas(monkeypatch)
    out = tmp_path / "r.duckdb"
    out.write_text("existing")
    export([TableConfig(id="1")], output=out, overwrite=True)
    report = load_report(out)
    assert report.row_counts["samples"] == 1


def test_export_discards_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` leaves no temp files behind in the output directory when writing fails."""
    _stub_schemas(monkeypatch)

    def boom(*args: object, **kwargs: object) -> None:  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr("latch_registry_export.api.write_export", boom)
    out = tmp_path / "r.duckdb"
    with pytest.raises(RuntimeError, match="boom"):
        export([TableConfig(id="1")], output=out)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_removes_wal_sidecar_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` removes the temp `.duckdb.wal` sidecar, not just the `.duckdb` file, on failure."""
    _stub_schemas(monkeypatch)

    def write_then_boom(
        schemas: object,  # noqa: ARG001
        plan: object,  # noqa: ARG001
        *,
        conn: duckdb.DuckDBPyConnection,
        run_meta: object,  # noqa: ARG001
    ) -> None:
        # Insert through the real connection so DuckDB actually writes a WAL
        # sidecar next to the temp file, then fail before it is checkpointed.
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")

    monkeypatch.setattr("latch_registry_export.api.write_export", write_then_boom)
    out = tmp_path / "r.duckdb"
    out.write_text("existing")
    with pytest.raises(RuntimeError, match="boom"):
        export([TableConfig(id="1")], output=out, overwrite=True)
    assert out.read_text() == "existing"
    assert list(tmp_path.iterdir()) == [out]


def test_export_rejects_invalid_page_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`export` raises `ValueError` for a non-positive `page_size`."""
    _stub_schemas(monkeypatch)
    out = tmp_path / "r.duckdb"
    with pytest.raises(ValueError, match="page_size"):
        export([TableConfig(id="1")], output=out, page_size=0)


def test_export_load_report_reconstructs_degraded_links_and_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_report` surfaces a self-link column's degraded status and its dangling-link issue."""
    name_col = ColumnSchema("__name__", "name", "VARCHAR", pl.Utf8(), False, False, None, None)
    manager_col = ColumnSchema("manager", "manager", "VARCHAR", pl.Utf8(), True, False, "1", None)
    table = TableSchema("1", "employees", "Employees", (name_col, manager_col))
    monkeypatch.setattr("latch_registry_export.api.build_schemas", lambda _tables: [table])
    monkeypatch.setattr("latch_registry_export.api._workspace_meta", lambda: ("ws", "Test"))

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield _FakeRecord("alice", {"manager": _linked_record("ghost")})

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    out = tmp_path / "r.duckdb"
    export([TableConfig(id="1")], output=out)

    report = load_report(out)
    assert report.degraded_links == (("employees", "manager"),)
    assert report.issue_counts == {"dangling_link": 1}
