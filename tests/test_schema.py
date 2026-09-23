from collections.abc import Mapping

import polars as pl
import pytest

from latch_registry_export.errors import UnsupportedTypeError
from latch_registry_export.schema import SchemaIndex
from latch_registry_export.schema import build_table_schema
from latch_registry_export.schema import map_registry_type
from latch_registry_export.schema import resolve_column_sql_names
from latch_registry_export.schema import sanitize_identifier


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sample Name", "sample_name"),
        ("read-count", "read_count"),
        ("  spaced  ", "spaced"),
        ("2fast", "col_2fast"),
        ("", "col_"),
        ("A__B", "a_b"),
        ("__x__", "x"),
        ("!!!", "col_"),
    ],
    ids=[
        "spaces-lowercased",
        "dash-to-underscore",
        "trim",
        "leading-digit",
        "empty",
        "collapse-repeats",
        "strip-underscores",
        "all-symbols",
    ],
)
def test_sanitize_identifier(raw: str, expected: str) -> None:
    assert sanitize_identifier(raw) == expected


def test_resolve_column_collisions_deterministic() -> None:
    keys = ["Read Files", "read_files", "name", "read files", "Name"]
    expected = {
        "Name": "name_2",
        "Read Files": "read_files",
        "name": "name_3",
        "read files": "read_files_2",
        "read_files": "read_files_3",
    }
    mapping = resolve_column_sql_names(keys, reserved={"name"})
    assert mapping == expected
    assert len(set(mapping.values())) == len(mapping)
    # A second call with the same input is deterministic (no reliance on set/dict order).
    assert resolve_column_sql_names(keys, reserved={"name"}) == mapping


RawDBType = dict[str, object]


def _basic(primitive: str, *, allow_empty: bool = True) -> RawDBType:
    return {"type": {"primitive": primitive}, "allowEmpty": allow_empty}


def _link(experiment_id: str) -> RawDBType:
    return {"type": {"primitive": "link", "experimentId": experiment_id}, "allowEmpty": True}


def _enum(members: list[str]) -> RawDBType:
    return {"type": {"primitive": "enum", "members": members}, "allowEmpty": True}


def _array(inner: RawDBType) -> RawDBType:
    inner_type = inner["type"]
    return {"type": {"array": inner_type}, "allowEmpty": True}


def _union(variants: dict[str, RawDBType]) -> RawDBType:
    return {"type": {"union": {k: v["type"] for k, v in variants.items()}}, "allowEmpty": True}


@pytest.mark.parametrize(
    ("db_type", "duckdb_type", "polars_dtype", "is_list", "link_target", "members"),
    [
        (_basic("string"), "VARCHAR", pl.Utf8(), False, None, None),
        (_basic("integer"), "BIGINT", pl.Int64(), False, None, None),
        (_basic("number"), "DOUBLE", pl.Float64(), False, None, None),
        (_basic("boolean"), "BOOLEAN", pl.Boolean(), False, None, None),
        (_basic("date"), "DATE", pl.Date(), False, None, None),
        (_basic("datetime"), "TIMESTAMP", pl.Datetime("us"), False, None, None),
        (_basic("blob"), "VARCHAR", pl.Utf8(), False, None, None),
        (_basic("null"), "VARCHAR", pl.Utf8(), False, None, None),
        (_link("999"), "VARCHAR", pl.Utf8(), False, "999", None),
        (_enum(["DNA", "RNA"]), "VARCHAR", pl.Utf8(), False, None, ("DNA", "RNA")),
        (_array(_basic("string")), "VARCHAR[]", pl.List(pl.Utf8()), True, None, None),
        (_array(_link("999")), "VARCHAR[]", pl.List(pl.Utf8()), True, "999", None),
        (
            _array(_array(_basic("string"))),
            "VARCHAR[][]",
            pl.List(pl.List(pl.Utf8())),
            True,
            None,
            None,
        ),
        (
            _array(_enum(["DNA", "RNA"])),
            "VARCHAR[]",
            pl.List(pl.Utf8()),
            True,
            None,
            ("DNA", "RNA"),
        ),
    ],
    ids=[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "blob",
        "null",
        "link",
        "enum",
        "array-string",
        "array-link",
        "array-of-array-string",
        "array-enum-propagates-members",
    ],
)
def test_map_registry_type(
    db_type: RawDBType,
    duckdb_type: str,
    polars_dtype: pl.DataType,
    is_list: bool,
    link_target: str | None,
    members: tuple[str, ...] | None,
) -> None:
    got_ddl, got_pl, got_is_list, got_link, got_members = map_registry_type(db_type)
    assert got_ddl == duckdb_type
    assert got_pl == polars_dtype
    assert got_is_list is is_list
    assert got_link == link_target
    assert got_members == members


def test_map_registry_type_unknown_primitive_raises() -> None:
    with pytest.raises(UnsupportedTypeError, match="bogus"):
        map_registry_type(_basic("bogus"))


def test_map_registry_type_union_raises() -> None:
    db_type = _union({"a": _basic("string"), "b": _basic("datetime")})
    with pytest.raises(UnsupportedTypeError, match="union"):
        map_registry_type(db_type)


class _FakeColumn:
    def __init__(self, upstream_type: RawDBType) -> None:
        self.upstream_type: Mapping[str, object] = upstream_type


def test_build_table_schema_prepends_name_and_orders_columns() -> None:
    cols = {
        "zeta": _FakeColumn(_basic("string")),
        "alpha": _FakeColumn(_basic("integer")),
    }
    schema = build_table_schema("11730", display_name="Samples", columns=cols, sql_name="samples")
    names = [c.sql_name for c in schema.columns]
    assert names[0] == "name"
    # data columns sorted by registry_key: alpha before zeta
    assert names[1:] == ["alpha", "zeta"]
    assert schema.columns[0].duckdb_type == "VARCHAR"


def test_build_table_schema_not_allow_empty_is_not_nullable() -> None:
    cols = {"required": _FakeColumn(_basic("string", allow_empty=False))}
    schema = build_table_schema("1", display_name=None, columns=cols, sql_name="t")
    (required_col,) = [c for c in schema.columns if c.registry_key == "required"]
    assert required_col.nullable is False


def test_schema_index_from_schemas() -> None:
    samples = build_table_schema("11730", display_name="Samples", columns={}, sql_name="samples")
    runs = build_table_schema("11731", display_name="Runs", columns={}, sql_name="runs")
    index = SchemaIndex.from_schemas([samples, runs])

    assert index.by_id == {"11730": samples, "11731": runs}
    assert index.by_sql_name == {"samples": samples, "runs": runs}
    assert index.sql_name_for("11730") == "samples"
    assert index.sql_name_for("11731") == "runs"
