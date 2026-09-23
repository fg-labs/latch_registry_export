import duckdb
import polars as pl
import pytest

from latch_registry_export.ddl import create_table_sql
from latch_registry_export.ddl import export_metadata_ddl
from latch_registry_export.dependencies import resolve_dependencies
from latch_registry_export.schema import ColumnSchema
from latch_registry_export.schema import TableSchema


def _name_col() -> ColumnSchema:
    return ColumnSchema("__name__", "name", "VARCHAR", pl.Utf8(), False, False, None, None)


def test_create_table_has_pk_and_types() -> None:
    schema = TableSchema(
        "1",
        "samples",
        "Samples",
        (
            _name_col(),
            ColumnSchema("conc", "conc", "DOUBLE", pl.Float64(), True, False, None, None),
            ColumnSchema("files", "files", "VARCHAR[]", pl.List(pl.Utf8()), True, True, None, None),
        ),
    )
    plan = resolve_dependencies([schema])
    con = duckdb.connect(":memory:")
    con.execute(create_table_sql(schema, plan))
    cols = con.execute("PRAGMA table_info('samples')").fetchall()
    by_name = {row[1]: row for row in cols}
    assert by_name["name"][5] is True  # pk flag
    assert by_name["conc"][2] == "DOUBLE"
    assert "VARCHAR[]" in by_name["files"][2]


def _connect_with_pools_and_samples_fk() -> duckdb.DuckDBPyConnection:
    """Create `pools`/`samples` tables in a fresh in-memory db, `samples.pool` FK-enforced."""
    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema(
        "1",
        "samples",
        "Samples",
        (_name_col(), ColumnSchema("pool", "pool", "VARCHAR", pl.Utf8(), True, False, "2", None)),
    )
    plan = resolve_dependencies([samples, pools])
    con = duckdb.connect(":memory:")
    con.execute(create_table_sql(pools, plan))
    con.execute(create_table_sql(samples, plan))
    return con


def test_enforced_fk_emitted() -> None:
    con = _connect_with_pools_and_samples_fk()
    # inserting a child row referencing a missing parent must fail
    with pytest.raises(duckdb.Error, match="[Ff]oreign key|[Vv]iolat"):
        con.execute("INSERT INTO samples (name, pool) VALUES ('s1', 'ghost')")


def test_enforced_fk_allows_valid_reference() -> None:
    con = _connect_with_pools_and_samples_fk()
    con.execute("INSERT INTO pools (name) VALUES ('p1')")
    con.execute("INSERT INTO samples (name, pool) VALUES ('s1', 'p1')")
    assert con.execute("SELECT pool FROM samples WHERE name = 's1'").fetchone() == ("p1",)


def test_non_enforced_link_emits_no_foreign_key() -> None:
    schema = TableSchema(
        "1",
        "samples",
        "Samples",
        (
            _name_col(),
            ColumnSchema(
                "list_no_fk", "list_no_fk", "VARCHAR[]", pl.List(pl.Utf8()), True, True, "1", None
            ),
            ColumnSchema("self_link", "self_link", "VARCHAR", pl.Utf8(), True, False, "1", None),
        ),
    )
    plan = resolve_dependencies([schema])
    sql = create_table_sql(schema, plan)
    assert "FOREIGN KEY" not in sql


def test_export_metadata_ddl_creates_four_tables() -> None:
    con = duckdb.connect(":memory:")
    for stmt in export_metadata_ddl():
        con.execute(stmt)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert {"_export_run", "_export_tables", "_export_columns", "_export_issues"} <= tables
