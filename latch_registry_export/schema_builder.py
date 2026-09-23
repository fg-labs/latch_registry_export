"""Discovers Registry table/column metadata and builds the export IR."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence

from latch_registry_export.config import RESERVED_PREFIX
from latch_registry_export.config import TableConfig
from latch_registry_export.errors import InvalidTableNameError
from latch_registry_export.schema import HasUpstreamType
from latch_registry_export.schema import TableSchema
from latch_registry_export.schema import build_table_schema
from latch_registry_export.schema import sanitize_identifier


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
    """
    from latch.registry.table import Table

    schemas: list[TableSchema] = []
    used_sql_names: set[str] = set()
    for cfg in tables:
        table = Table(id=cfg.id)
        display_name = table.get_display_name()
        columns: dict[str, HasUpstreamType] = {
            key: _UpstreamTypeColumn(dict(col.upstream_type))
            for key, col in (table.get_columns() or {}).items()
        }
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
