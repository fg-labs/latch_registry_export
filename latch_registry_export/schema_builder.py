"""Discovers Registry table/column metadata and builds the export IR."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from collections.abc import Sequence
from typing import TypeVar

from latch_registry_export.config import RESERVED_PREFIX
from latch_registry_export.config import TableConfig
from latch_registry_export.errors import InvalidTableNameError
from latch_registry_export.errors import UnknownColumnError
from latch_registry_export.schema import HasUpstreamType
from latch_registry_export.schema import TableSchema
from latch_registry_export.schema import build_table_schema
from latch_registry_export.schema import sanitize_identifier

logger = logging.getLogger(__name__)

_ColumnT = TypeVar("_ColumnT")


class _UpstreamTypeColumn:
    """
    Adapts a real Latch `Column` to `schema.HasUpstreamType`'s loose protocol.

    Copies `upstream_type` into a `Mapping[str, object]` field to satisfy
    `HasUpstreamType` without a `cast()`. Must be settable, so not frozen.
    """

    def __init__(self, upstream_type: Mapping[str, object]) -> None:
        self.upstream_type = upstream_type


def build_schemas(tables: Sequence[TableConfig]) -> list[TableSchema]:
    """
    Fetch Registry column metadata and build the export IR for every table.

    Args:
        tables: The configured tables to export.

    Returns:
        One `TableSchema` per table, in `tables` order, with DuckDB table names
        resolved (from the config override, else the sanitized display name, else
        `table_<id>`) and validated against the reserved `_export_` prefix and
        against each other.

    Raises:
        InvalidTableNameError: A resolved table name uses the reserved
            `_export_` prefix, or two tables resolve to the same name.
        UnknownColumnError: A table's `include_columns`/`exclude_columns` names a
            column the table does not have, or `exclude_columns` drops every column.
    """
    from latch.registry.table import Table

    schemas: list[TableSchema] = []
    used_sql_names: set[str] = set()
    for cfg in tables:
        table = Table(id=cfg.id)
        display_name = table.get_display_name()
        columns: dict[str, HasUpstreamType] = {
            key: _UpstreamTypeColumn(dict(col.upstream_type))
            for key, col in _select_columns(cfg, table.get_columns() or {}).items()
        }
        if cfg.include_columns is not None or cfg.exclude_columns is not None:
            logger.info("Table %s: exporting columns %s", cfg.id, sorted(columns))
        sql_name = (
            cfg.name
            if cfg.name is not None
            else (sanitize_identifier(display_name) if display_name else f"table_{cfg.id}")
        )
        _validate_resolved_table_name(sql_name, used_sql_names)
        used_sql_names.add(sql_name)
        schemas.append(
            build_table_schema(
                cfg.id, display_name=display_name, columns=columns, sql_name=sql_name
            )
        )
    return schemas


def _select_columns(cfg: TableConfig, columns: Mapping[str, _ColumnT]) -> dict[str, _ColumnT]:
    """
    Apply a table config's `include_columns`/`exclude_columns` to its Registry columns.

    Args:
        cfg: The table config.
        columns: The table's Registry columns, keyed by column key.

    Returns:
        The selected subset of `columns`; all of `columns` if neither list is set.

    Raises:
        UnknownColumnError: A listed key is not in `columns` (keys match exactly, so
            case matters), or `exclude_columns` drops every column.
    """
    field_name, listed = (
        ("include_columns", cfg.include_columns)
        if cfg.include_columns is not None
        else ("exclude_columns", cfg.exclude_columns)
    )
    if listed is None:
        return dict(columns)

    unknown = sorted(set(listed) - set(columns))
    if unknown:
        raise UnknownColumnError(
            f"table {cfg.id} has no column(s) {unknown} listed in {field_name}; "
            f"available columns: {sorted(columns)}"
        )

    if field_name == "include_columns":
        return {key: col for key, col in columns.items() if key in listed}
    selected = {key: col for key, col in columns.items() if key not in listed}
    if not selected:
        raise UnknownColumnError(f"table {cfg.id}: exclude_columns excludes every data column")
    return selected


def _validate_resolved_table_name(sql_name: str, used: set[str]) -> None:
    """
    Reject a resolved table name that hits the reserved prefix or collides.

    Args:
        sql_name: The resolved table name to validate.
        used: The resolved table names seen so far.

    Raises:
        InvalidTableNameError: `sql_name` uses the reserved `_export_` prefix,
            or is already in `used`.
    """
    if sql_name.startswith(RESERVED_PREFIX):
        raise InvalidTableNameError(
            f"resolved table name {sql_name!r} uses the reserved {RESERVED_PREFIX!r} prefix"
        )
    if sql_name in used:
        raise InvalidTableNameError(f"resolved table name {sql_name!r} collides with another table")
