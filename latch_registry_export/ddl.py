"""DuckDB DDL generation from the schema IR and FK plan."""

from __future__ import annotations

from latch_registry_export.dependencies import FkStatus
from latch_registry_export.dependencies import ResolvedPlan
from latch_registry_export.schema import RECORD_NAME_COLUMN
from latch_registry_export.schema import TableSchema
from latch_registry_export.schema import quote_identifier


def create_table_sql(schema: TableSchema, plan: ResolvedPlan) -> str:
    """
    Build the `CREATE TABLE` statement for one table.

    All data columns are emitted nullable by design; only the `name` PK column is
    `NOT NULL`. This is intentional, not an oversight: a `NULL` value paired with a
    sidecar `_export_issues` row (e.g. a value that failed to parse) must never
    violate a `NOT NULL` constraint at load time.

    The referenced parent tables must already exist when this statement runs, since
    a `FOREIGN KEY` clause requires its target table's schema. Callers must execute
    the statements in `plan.insert_order` (parents before children), or DuckDB
    raises a catalog error at `CREATE TABLE` time.

    Args:
        schema: The table's IR.
        plan: The resolved FK plan (decides which links get enforced FK constraints).

    Returns:
        A `CREATE TABLE` SQL string with `name` as PK and enforced FK constraints.
    """
    lines: list[str] = []
    for col in schema.columns:
        if col.sql_name == RECORD_NAME_COLUMN:
            lines.append(f"  {quote_identifier(col.sql_name)} {col.duckdb_type} PRIMARY KEY")
        else:
            lines.append(f"  {quote_identifier(col.sql_name)} {col.duckdb_type}")

    for lp in plan.link_plans:
        if lp.table_sql_name == schema.sql_name and lp.fk_status == FkStatus.ENFORCED:
            quoted_name_col = quote_identifier(RECORD_NAME_COLUMN)
            lines.append(
                f"  FOREIGN KEY ({quote_identifier(lp.column_sql_name)}) "
                f"REFERENCES {quote_identifier(lp.target_table_sql_name)} ({quoted_name_col})"
            )

    body = ",\n".join(lines)
    return f"CREATE TABLE {quote_identifier(schema.sql_name)} (\n{body}\n);"


def export_metadata_ddl() -> list[str]:
    """
    Return the `CREATE TABLE` statements for the `_export_*` tables.

    In order: `_export_run`, `_export_tables`, `_export_columns`, `_export_issues`.
    """
    return [
        """
        CREATE TABLE _export_run (
          exported_at TIMESTAMP, tool_version VARCHAR, fglatch_version VARCHAR,
          latch_version VARCHAR, duckdb_version VARCHAR, polars_version VARCHAR,
          pyarrow_version VARCHAR, workspace_id VARCHAR, workspace_name VARCHAR,
          page_size BIGINT
        );
        """,
        """
        CREATE TABLE _export_tables (
          table_sql_name VARCHAR, registry_table_id VARCHAR, display_name VARCHAR,
          row_count BIGINT, max_last_updated TIMESTAMP
        );
        """,
        """
        CREATE TABLE _export_columns (
          table_sql_name VARCHAR, column_sql_name VARCHAR, registry_key VARCHAR,
          duckdb_type VARCHAR, nullable BOOLEAN, is_list BOOLEAN,
          link_target_table_id VARCHAR, fk_status VARCHAR, enum_members VARCHAR[]
        );
        """,
        """
        CREATE TABLE _export_issues (
          table_sql_name VARCHAR, record_name VARCHAR, column_sql_name VARCHAR,
          registry_key VARCHAR, issue_type VARCHAR, raw_value VARCHAR, detail VARCHAR
        );
        """,
    ]
