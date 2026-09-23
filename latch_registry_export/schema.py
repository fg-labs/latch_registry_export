"""
Schema intermediate representation and Registry→DuckDB/polars type mapping.

Identifiers produced here are always double-quoted in downstream DDL, so SQL
reserved words (e.g. `select`, `order`) are safe as identifiers and are not
specially guarded against.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import polars as pl

from latch_registry_export.errors import UnsupportedTypeError

_NON_IDENT = re.compile(r"[^a-z0-9]+")

RECORD_NAME_COLUMN = "name"
"""SQL name of the leading column that holds each record's name (the primary key)."""


class HasUpstreamType(Protocol):
    """Structural contract for a Latch `Column`-like object: exposes `upstream_type`."""

    upstream_type: Mapping[str, object]


def quote_identifier(identifier: str) -> str:
    """
    Double-quote a SQL identifier (identifiers are pre-sanitized, so this is safe).

    Args:
        identifier: The unquoted SQL identifier.

    Returns:
        `identifier`, double-quoted, with any embedded `"` doubled.
    """
    return '"' + identifier.replace('"', '""') + '"'


def sanitize_identifier(raw: str) -> str:
    """
    Turn an arbitrary string into a deterministic lower-snake SQL identifier.

    An empty or all-symbol input yields `"col_"`.

    Args:
        raw: The source string (a Registry display name or column key).

    Returns:
        A valid identifier: lower-case, `[a-z0-9_]`, no leading digit or underscore.
    """
    lowered = raw.strip().lower()
    collapsed = _NON_IDENT.sub("_", lowered).strip("_")
    if not collapsed or collapsed[0].isdigit():
        collapsed = f"col_{collapsed}"
    return collapsed


def resolve_column_sql_names(registry_keys: Iterable[str], *, reserved: set[str]) -> dict[str, str]:
    """
    Map each registry column key to a unique sql identifier, deterministically.

    Args:
        registry_keys: The column keys to map.
        reserved: Identifiers already taken (e.g. {"name"}).

    Returns:
        A dict {registry_key: sql_name}, collisions disambiguated with `_2`, `_3`, ….
    """
    taken = set(reserved)
    result: dict[str, str] = {}
    for key in sorted(registry_keys):
        base = sanitize_identifier(key)
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}_{suffix}"
            suffix += 1
        taken.add(candidate)
        result[key] = candidate
    return result


@dataclass(frozen=True)
class ColumnSchema:
    """A single resolved column in the export intermediate representation (IR)."""

    registry_key: str
    sql_name: str
    duckdb_type: str
    polars_dtype: pl.DataType
    nullable: bool
    is_list: bool
    link_target_table_id: str | None
    enum_members: tuple[str, ...] | None


@dataclass(frozen=True)
class TableSchema:
    """A resolved table in the export IR (columns include the leading `name` primary key (PK))."""

    table_id: str
    sql_name: str
    display_name: str | None
    columns: tuple[ColumnSchema, ...]


# Instantiate each dtype (e.g. `pl.Utf8()` not bare `pl.Utf8`): polars dtype "shortcuts"
# are classes, not `DataType` instances, which strict mypy rejects for this field.
_PRIMITIVE_MAP: dict[str, tuple[str, pl.DataType]] = {
    "string": ("VARCHAR", pl.Utf8()),
    "integer": ("BIGINT", pl.Int64()),
    "number": ("DOUBLE", pl.Float64()),
    "boolean": ("BOOLEAN", pl.Boolean()),
    "date": ("DATE", pl.Date()),
    "datetime": ("TIMESTAMP", pl.Datetime("us")),  # values normalized to naive UTC
    "blob": ("VARCHAR", pl.Utf8()),  # resolved latch path
    "enum": ("VARCHAR", pl.Utf8()),  # member .name
    "link": ("VARCHAR", pl.Utf8()),  # linked record name
    "null": ("VARCHAR", pl.Utf8()),  # all-NULL
}


def _map_registry_type(
    reg_type: Mapping[str, object],
) -> tuple[str, pl.DataType, bool, str | None, tuple[str, ...] | None]:
    """
    Map a raw Registry `RegistryType` (no `allowEmpty` wrapper) to DuckDB/polars types.

    Args:
        reg_type: The raw `RegistryType` mapping (`array` | `union` | `primitive` variant).

    Returns:
        `(duckdb_type, polars_dtype, is_list, link_target_table_id, enum_members)`.

    Raises:
        UnsupportedTypeError: `reg_type` is a `union` (not creatable via the Latch SDK,
            and unsupported here), has none of `array`/`union`/`primitive`, or its
            primitive has no DuckDB/polars mapping.

    Note:
        The `isinstance` asserts below narrow freshly-extracted `object` values for
        mypy; malformed upstream data instead raises `UnsupportedTypeError`.
    """
    if "array" in reg_type:
        inner_reg_type = reg_type["array"]
        assert isinstance(inner_reg_type, Mapping)
        inner_ddl, inner_pl, _, link_target, members = _map_registry_type(inner_reg_type)
        return f"{inner_ddl}[]", pl.List(inner_pl), True, link_target, members

    if "union" in reg_type:
        union_variants = reg_type["union"]
        assert isinstance(union_variants, Mapping)
        raise UnsupportedTypeError(
            f"union columns are not supported (variants: {sorted(union_variants)})"
        )

    if "primitive" not in reg_type:
        raise UnsupportedTypeError(f"Registry type has no array/union/primitive key: {reg_type!r}")

    primitive = reg_type["primitive"]
    if primitive == "link":
        return "VARCHAR", pl.Utf8(), False, str(reg_type["experimentId"]), None
    if primitive == "enum":
        raw_members = reg_type["members"]
        assert isinstance(raw_members, list)  # internal invariant; upstream Registry contract
        return "VARCHAR", pl.Utf8(), False, None, tuple(str(m) for m in raw_members)
    if primitive not in _PRIMITIVE_MAP:
        raise UnsupportedTypeError(f"Unknown Registry primitive type: {primitive!r}")
    duckdb_type, polars_dtype = _PRIMITIVE_MAP[primitive]
    return duckdb_type, polars_dtype, False, None, None


def map_registry_type(
    db_type: Mapping[str, object],
) -> tuple[str, pl.DataType, bool, str | None, tuple[str, ...] | None]:
    """
    Map a raw Registry `DBType` to DuckDB + polars types and link/enum metadata.

    Args:
        db_type: The raw `DBType` mapping (`{"type": RegistryType, "allowEmpty": bool}`).

    Returns:
        `(duckdb_type, polars_dtype, is_list, link_target_table_id, enum_members)`.

    Raises:
        UnsupportedTypeError: `db_type` has no `"type"` key, or the Registry type it
            wraps is malformed or unmappable. See `_map_registry_type`.
    """
    if "type" not in db_type:
        raise UnsupportedTypeError(f"DBType mapping has no 'type' key: {db_type!r}")
    reg_type = db_type["type"]
    assert isinstance(reg_type, Mapping)  # narrows for mypy; see _map_registry_type
    return _map_registry_type(reg_type)


def build_table_schema(
    table_id: str,
    *,
    display_name: str | None,
    columns: Mapping[str, HasUpstreamType],
    sql_name: str,
) -> TableSchema:
    """
    Build the IR for one table from its Registry columns.

    Args:
        table_id: The Registry table id.
        display_name: The table's display name (may be None).
        columns: Mapping of column key -> Latch `Column` (has `.upstream_type`).
        sql_name: The already-resolved DuckDB table name for this table.

    Returns:
        The `TableSchema`, with a leading `name VARCHAR` primary key (PK) column then
        data columns in sorted-registry-key order.
    """
    name_col = ColumnSchema(
        registry_key="__latch_record_name__",
        sql_name=RECORD_NAME_COLUMN,
        duckdb_type="VARCHAR",
        polars_dtype=pl.Utf8(),
        nullable=False,
        is_list=False,
        link_target_table_id=None,
        enum_members=None,
    )
    sql_names = resolve_column_sql_names(columns.keys(), reserved={RECORD_NAME_COLUMN})
    data_cols: list[ColumnSchema] = []
    for key in sql_names:
        col = columns[key]
        db_type = col.upstream_type
        ddl, pl_dtype, is_list, link_target, members = map_registry_type(db_type)
        data_cols.append(
            ColumnSchema(
                registry_key=key,
                sql_name=sql_names[key],
                duckdb_type=ddl,
                polars_dtype=pl_dtype,
                nullable=bool(db_type["allowEmpty"]),
                is_list=is_list,
                link_target_table_id=link_target,
                enum_members=members,
            )
        )
    return TableSchema(
        table_id=table_id,
        sql_name=sql_name,
        display_name=display_name,
        columns=(name_col, *data_cols),
    )
