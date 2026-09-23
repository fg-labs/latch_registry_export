"""Streams Registry records into DuckDB tables and records provenance."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass

import duckdb
import polars as pl
from fglatch.registry import fetch_table_records
from latch.registry.record import Record

from latch_registry_export.ddl import create_table_sql
from latch_registry_export.ddl import export_metadata_ddl
from latch_registry_export.dependencies import FkStatus
from latch_registry_export.dependencies import ResolvedPlan
from latch_registry_export.errors import ConversionError
from latch_registry_export.errors import DuplicateRecordNameError
from latch_registry_export.errors import EmptyRecordNameError
from latch_registry_export.schema import RECORD_NAME_COLUMN
from latch_registry_export.schema import TableSchema
from latch_registry_export.schema import quote_identifier
from latch_registry_export.serialize import Issue
from latch_registry_export.serialize import serialize
from latch_registry_export.serialize import to_naive_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunMeta:
    """Provenance fields for the single `_export_run` row."""

    exported_at: dt.datetime
    tool_version: str
    fglatch_version: str
    latch_version: str
    duckdb_version: str
    polars_version: str
    pyarrow_version: str
    workspace_id: str
    workspace_name: str
    page_size: int


def write_export(
    schemas: Sequence[TableSchema],
    plan: ResolvedPlan,
    *,
    conn: duckdb.DuckDBPyConnection,
    run_meta: RunMeta,
) -> None:
    """
    Create every table, stream-load its records, and populate provenance tables.

    Creates the `_export_*` tables and data tables (parents first, per
    `plan.insert_order`), streams and inserts each table's records page by page,
    records per-table/-column provenance, anti-joins non-enforced links for
    dangling references, inserts every `_export_issues` row in one deterministic
    batch, then records the run metadata.

    Reproducibility means content-equivalent output, not byte-identical: DuckDB
    does not guarantee physical row order on disk, so consumers must `ORDER BY`
    when row order matters (e.g. when diffing exports).

    Args:
        schemas: The table IRs to export.
        plan: The resolved FK/topo plan for `schemas`.
        conn: An open DuckDB connection to write into.
        run_meta: Provenance fields for the single `_export_run` row; also
            supplies the page size for `fetch_table_records` and per-page inserts.

    Raises:
        EmptyRecordNameError: A record has an empty name.
        DuplicateRecordNameError: Two records in one table share a name.
        ConversionError: The Latch SDK failed to fetch or convert a record.
    """
    page_size = run_meta.page_size
    for statement in export_metadata_ddl():
        conn.execute(statement)

    schema_by_sql_name = {schema.sql_name: schema for schema in schemas}
    for sql_name in plan.insert_order:
        conn.execute(create_table_sql(schema_by_sql_name[sql_name], plan))

    written_names: dict[str, set[str]] = {schema.sql_name: set() for schema in schemas}
    id_to_sql = {schema.table_id: schema.sql_name for schema in schemas}

    all_issue_rows: list[_IssueRow] = []
    for sql_name in plan.insert_order:
        schema = schema_by_sql_name[sql_name]
        row_count, max_last_updated, issue_rows = _load_table(
            schema,
            plan,
            conn=conn,
            page_size=page_size,
            written_names=written_names,
            id_to_sql=id_to_sql,
        )
        all_issue_rows.extend(issue_rows)
        conn.execute(
            "INSERT INTO _export_tables "
            "(table_sql_name, registry_table_id, display_name, row_count, max_last_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            [schema.sql_name, schema.table_id, schema.display_name, row_count, max_last_updated],
        )
        _insert_column_provenance(schema, plan, conn=conn)

    all_issue_rows.extend(_run_post_load_antijoin(schemas, plan, conn=conn))

    # Issues are rare in a healthy export, so holding every row in memory before
    # one sorted, deterministic insert is an acceptable trade-off here. The sort
    # key covers the full row tuple, so insert order is intrinsically
    # deterministic rather than relying on Python's stable sort plus append
    # order; `raw_value`/`detail` are nullable, so `None` is normalized to `""`
    # for the comparison only (the inserted row keeps its original `None`).
    all_issue_rows.sort(key=lambda row: tuple("" if field is None else field for field in row))
    if all_issue_rows:
        conn.executemany(
            "INSERT INTO _export_issues "
            "(table_sql_name, record_name, column_sql_name, registry_key, issue_type, "
            "raw_value, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            all_issue_rows,
        )

    conn.execute(
        "INSERT INTO _export_run "
        "(exported_at, tool_version, fglatch_version, latch_version, duckdb_version, "
        "polars_version, pyarrow_version, workspace_id, workspace_name, page_size) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run_meta.exported_at,
            run_meta.tool_version,
            run_meta.fglatch_version,
            run_meta.latch_version,
            run_meta.duckdb_version,
            run_meta.polars_version,
            run_meta.pyarrow_version,
            run_meta.workspace_id,
            run_meta.workspace_name,
            run_meta.page_size,
        ],
    )
    logger.info("Exported %d tables.", len(schemas))


_IssueRow = tuple[str, str, str, str, str, str | None, str | None]


def _serialize_row(
    schema: TableSchema,
    name: str,
    values: Mapping[str, object],
    *,
    enforced_targets: Mapping[str, str],
    written_names: Mapping[str, set[str]],
) -> tuple[dict[str, object], list[_IssueRow]]:
    """
    Serialize one record's data columns and collect its sidecar `_export_issues` rows.

    For an enforced-FK column, a cell is only dangling-checked here when its raw
    value is an actual `Record` (a real link). An empty cell serializes to `None`
    via `serialize` and is left alone: it is a legitimately empty link, not a
    dangling one, and must not be flagged.

    Args:
        schema: The table's IR.
        name: The record's (already-validated) name.
        values: The record's raw Registry values, keyed by `registry_key`.
        enforced_targets: Map of this table's enforced-FK column sql_names to
            their target table's sql_name.
        written_names: Map of table sql_name to the record names already written.

    Returns:
        `(row, issue_rows)`: the row dict keyed by column sql_name (name column
        included), and the `_export_issues` rows this record produced.
    """
    row: dict[str, object] = {RECORD_NAME_COLUMN: name}
    issue_rows: list[_IssueRow] = []
    for col in schema.columns:
        if col.sql_name == RECORD_NAME_COLUMN:
            continue
        raw = values.get(col.registry_key)
        primitive, issues = serialize(raw, col)
        target_sql_name = enforced_targets.get(col.sql_name)
        if target_sql_name is not None and isinstance(raw, Record):
            if primitive is None or primitive not in written_names[target_sql_name]:
                # Record the resolved name before nulling, to match the anti-join's raw_value.
                resolved_name = primitive if isinstance(primitive, str) else None
                issues = [
                    *issues,
                    Issue(col.sql_name, col.registry_key, "dangling_link", resolved_name, "nulled"),
                ]
                primitive = None
        row[col.sql_name] = primitive
        for issue in issues:
            issue_rows.append((
                schema.sql_name,
                name,
                issue.column_sql_name,
                issue.registry_key,
                issue.issue_type,
                issue.raw_value,
                issue.detail,
            ))
    return row, issue_rows


def _load_table(
    schema: TableSchema,
    plan: ResolvedPlan,
    *,
    conn: duckdb.DuckDBPyConnection,
    page_size: int,
    written_names: dict[str, set[str]],
    id_to_sql: Mapping[str, str],
) -> tuple[int, dt.datetime | None, list[_IssueRow]]:
    """
    Stream one table's records, serialize each page, insert it, and collect issues.

    Only the SDK boundary — iterating `fetch_table_records` and calling a
    record's `get_name`/`get_last_updated`/`get_values` accessors — is wrapped
    as `ConversionError`. Serialization and DuckDB inserts run outside that
    wrapper, so a DuckDB or polars failure surfaces as itself, not a
    mislabeled "fetch/convert" error.

    Args:
        schema: The table's IR.
        plan: The resolved FK plan.
        conn: An open DuckDB connection with the table already created.
        page_size: Page size for `fetch_table_records` and per-page inserts.
        written_names: Map of table sql_name to the record names already written;
            read for enforced-FK dangling checks and updated with this table's names.
        id_to_sql: Map of table_id to sql_name, for all exported tables.

    Returns:
        `(row_count, max_last_updated, issue_rows)` for this table;
        `max_last_updated` is normalized to naive UTC, or `None` if no record
        carried a timestamp; `issue_rows` are collected, not yet inserted.

    Raises:
        EmptyRecordNameError: A record has an empty name.
        DuplicateRecordNameError: Two records in this table share a name.
        ConversionError: `fetch_table_records` or a record accessor
            (`get_name`/`get_last_updated`/`get_values`) raised while fetching
            or converting a record.
    """
    enforced_targets = {
        col.sql_name: id_to_sql[col.link_target_table_id]
        for col in schema.columns
        if col.link_target_table_id is not None
        and plan.fk_status_by_column.get((schema.sql_name, col.sql_name)) == FkStatus.ENFORCED
    }
    own_names = written_names[schema.sql_name]
    row_count = 0
    max_last_updated: dt.datetime | None = None
    page: list[dict[str, object]] = []
    issue_rows: list[_IssueRow] = []

    def flush_page() -> None:
        if not page:
            return
        df = pl.DataFrame(page, schema={c.sql_name: c.polars_dtype for c in schema.columns})
        conn.register("_page", df)
        try:
            conn.execute(
                f"INSERT INTO {quote_identifier(schema.sql_name)} BY NAME SELECT * FROM _page"
            )
        finally:
            conn.unregister("_page")
        page.clear()

    records_iter = iter(fetch_table_records(schema.table_id, page_size=page_size))
    while True:
        try:
            record = next(records_iter)
            raw_name = record.get_name(load_if_missing=False)
            last_updated = record.get_last_updated(load_if_missing=False)
            values = record.get_values(load_if_missing=False) or {}
        except StopIteration:
            break
        except Exception as e:  # noqa: BLE001 - surface SDK fetch/convert failures with context
            raise ConversionError(
                f"failed to fetch/convert records for table {schema.table_id!r}: {e}"
            ) from e

        if not raw_name:
            raise EmptyRecordNameError(
                f"table {schema.sql_name!r}: record has an empty name; cannot serve as the name PK"
            )
        name = raw_name
        if name in own_names:
            raise DuplicateRecordNameError(
                f"table {schema.sql_name!r}: duplicate record name {name!r}"
            )
        own_names.add(name)

        if last_updated is not None:
            last_updated = to_naive_utc(last_updated)
            max_last_updated = (
                last_updated if max_last_updated is None else max(max_last_updated, last_updated)
            )

        row, row_issues = _serialize_row(
            schema,
            name,
            values,
            enforced_targets=enforced_targets,
            written_names=written_names,
        )
        issue_rows.extend(row_issues)
        page.append(row)
        row_count += 1
        if len(page) >= page_size:
            flush_page()

    flush_page()
    return row_count, max_last_updated, issue_rows


def _insert_column_provenance(
    schema: TableSchema, plan: ResolvedPlan, *, conn: duckdb.DuckDBPyConnection
) -> None:
    """Insert one `_export_columns` row per data column (skips the `name` PK)."""
    for col in schema.columns:
        if col.sql_name == RECORD_NAME_COLUMN:
            continue
        fk_status = plan.fk_status_by_column.get((schema.sql_name, col.sql_name), FkStatus.NONE)
        conn.execute(
            "INSERT INTO _export_columns "
            "(table_sql_name, column_sql_name, registry_key, duckdb_type, nullable, is_list, "
            "link_target_table_id, fk_status, enum_members) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                schema.sql_name,
                col.sql_name,
                col.registry_key,
                col.duckdb_type,
                col.nullable,
                col.is_list,
                col.link_target_table_id,
                fk_status,
                list(col.enum_members) if col.enum_members else None,
            ],
        )


def _run_post_load_antijoin(
    schemas: Sequence[TableSchema], plan: ResolvedPlan, *, conn: duckdb.DuckDBPyConnection
) -> list[_IssueRow]:
    """
    Build a `dangling_link` issue row (detail='kept') for each non-enforced dangling link.

    Runs after every table is loaded, since a `back_edge`/`self_link` target may
    not exist yet when its own row is inserted. Enforced links are checked inline
    while streaming instead (see `_load_table`), so only `back_edge`, `self_link`,
    and `list_no_fk` links are anti-joined here. Rows are returned, not inserted;
    the caller inserts them as part of one deterministic, sorted batch.

    Args:
        schemas: The table IRs.
        plan: The resolved FK/topo plan.
        conn: An open DuckDB connection, with every table already loaded.

    Returns:
        The `_export_issues` rows found by the anti-join, in query order.
    """
    schema_by_sql_name = {schema.sql_name: schema for schema in schemas}
    issue_rows: list[_IssueRow] = []
    for link in plan.link_plans:
        if link.fk_status not in (FkStatus.BACK_EDGE, FkStatus.SELF_LINK, FkStatus.LIST_NO_FK):
            continue
        schema = schema_by_sql_name[link.table_sql_name]
        col = next(c for c in schema.columns if c.sql_name == link.column_sql_name)
        quoted_name_col = quote_identifier(RECORD_NAME_COLUMN)
        quoted_table = quote_identifier(schema.sql_name)
        quoted_col = quote_identifier(col.sql_name)
        if col.is_list:
            select = (
                f"SELECT t.{quoted_name_col} AS record_name, u.elem AS linked_name "
                f"FROM {quoted_table} t, UNNEST(t.{quoted_col}) AS u(elem)"
            )
        else:
            select = (
                f"SELECT {quoted_name_col} AS record_name, "
                f"{quoted_col} AS linked_name FROM {quoted_table}"
            )
        # `linked_name NOT IN (SELECT name FROM ...)` is NULL-safe here only because
        # `name` is the target table's NOT NULL primary key: no NULL on the right-hand
        # side can turn the whole `NOT IN` into an unmatched NULL.
        quoted_target = quote_identifier(link.target_table_sql_name)
        dangling = conn.execute(
            f"SELECT record_name, linked_name FROM ({select}) s "
            f"WHERE linked_name IS NOT NULL "
            f"AND linked_name NOT IN (SELECT {quoted_name_col} FROM {quoted_target}) "
            f"ORDER BY record_name, linked_name"
        ).fetchall()
        for record_name, linked_name in dangling:
            issue_rows.append((
                schema.sql_name,
                record_name,
                col.sql_name,
                col.registry_key,
                "dangling_link",
                linked_name,
                "kept",
            ))
    return issue_rows
