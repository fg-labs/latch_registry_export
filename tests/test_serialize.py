import datetime as dt
import enum

import polars as pl
import pytest

from latch_registry_export.schema import ColumnSchema
from latch_registry_export.serialize import INT64_MAX
from latch_registry_export.serialize import INT64_MIN
from latch_registry_export.serialize import Issue
from latch_registry_export.serialize import serialize


class _Nucleic(enum.Enum):
    """A tiny auto-int enum: `.name` is the member string, `.value` is an int."""

    DNA = enum.auto()
    RNA = enum.auto()


def _col(
    duckdb_type: str = "VARCHAR",
    polars_dtype: pl.DataType | None = None,
    *,
    registry_key: str = "c",
    sql_name: str = "c",
    nullable: bool = True,
    is_list: bool = False,
    link_target_table_id: str | None = None,
    enum_members: tuple[str, ...] | None = None,
) -> ColumnSchema:
    return ColumnSchema(
        registry_key=registry_key,
        sql_name=sql_name,
        duckdb_type=duckdb_type,
        polars_dtype=polars_dtype if polars_dtype is not None else pl.Utf8(),
        nullable=nullable,
        is_list=is_list,
        link_target_table_id=link_target_table_id,
        enum_members=enum_members,
    )


@pytest.mark.parametrize(
    ("value", "col", "expected"),
    [
        ("x", _col(), "x"),
        (3, _col("BIGINT", pl.Int64()), 3),
        (True, _col("BOOLEAN", pl.Boolean()), True),
        (1.5, _col("DOUBLE", pl.Float64()), 1.5),
        (dt.date(2024, 1, 1), _col("DATE", pl.Date()), dt.date(2024, 1, 1)),
        (INT64_MIN, _col("BIGINT", pl.Int64()), INT64_MIN),
        (INT64_MAX, _col("BIGINT", pl.Int64()), INT64_MAX),
    ],
    ids=["str", "int", "bool", "float", "date", "int64-min-boundary", "int64-max-boundary"],
)
def test_scalar_passthrough(value: object, col: ColumnSchema, expected: object) -> None:
    assert serialize(value, col) == (expected, [])


@pytest.mark.parametrize(
    "value",
    [2**63, -(2**63) - 1],
    ids=["above-int64-max", "below-int64-min"],
)
def test_int_overflow_nulls_and_issues(value: int) -> None:
    val, issues = serialize(value, _col("BIGINT", pl.Int64()))
    assert val is None
    assert len(issues) == 1
    assert issues[0].issue_type == "invalid_value"
    assert issues[0].raw_value == str(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            dt.datetime(2024, 1, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=5))),
            dt.datetime(2024, 1, 1, 7, 0),  # 12:00+05:00 -> 07:00 UTC, naive
        ),
        (
            dt.datetime(2024, 1, 1, 12, 0),  # naive input, assumed already UTC
            dt.datetime(2024, 1, 1, 12, 0),
        ),
    ],
    ids=["aware-converted-to-naive-utc", "naive-assumed-utc"],
)
def test_datetime_serializes_to_naive_utc(value: dt.datetime, expected: dt.datetime) -> None:
    got, issues = serialize(value, _col("TIMESTAMP", pl.Datetime("us")))
    assert got == expected
    assert isinstance(got, dt.datetime)
    assert got.tzinfo is None
    assert got.isoformat() + "Z" == expected.isoformat() + "Z"
    assert issues == []


def test_enum_uses_name_not_value() -> None:
    got, issues = serialize(_Nucleic.DNA, _col(enum_members=("DNA", "RNA")))
    assert got == "DNA"  # not _Nucleic.DNA.value (an auto-int)
    assert issues == []


def test_invalid_value_nulls_with_raw() -> None:
    from latch.registry.types import InvalidValue

    got, issues = serialize(InvalidValue(raw_value="oops"), _col())
    assert got is None
    assert len(issues) == 1
    assert issues[0] == Issue(
        column_sql_name="c",
        registry_key="c",
        issue_type="invalid_value",
        raw_value="oops",
        detail=None,
    )


def test_empty_cell_nulls_no_issue() -> None:
    from latch.registry.upstream_types.values import EmptyCell

    got, issues = serialize(EmptyCell(), _col())
    assert got is None
    assert issues == []


def test_none_nulls_no_issue() -> None:
    got, issues = serialize(None, _col())
    assert got is None
    assert issues == []


@pytest.mark.parametrize(
    ("nullable", "expected_issue_type"),
    [
        (False, "missing_required"),
        (True, "invalid_value"),
    ],
    ids=["required-column-missing-is-missing_required", "optional-column-missing-is-invalid_value"],
)
def test_missing_value_issue_type_depends_on_nullable(
    nullable: bool, expected_issue_type: str
) -> None:
    from latch.registry.types import InvalidValue

    got, issues = serialize(InvalidValue(raw_value=""), _col(nullable=nullable))
    assert got is None
    assert issues[0].issue_type == expected_issue_type


def test_list_preserves_order_and_flags_bad_element() -> None:
    from latch.registry.types import InvalidValue

    col = _col("VARCHAR[]", pl.List(pl.Utf8()), is_list=True)
    got, issues = serialize(["a", InvalidValue(raw_value="bad"), "c"], col)
    assert got == ["a", None, "c"]
    assert len(issues) == 1
    assert issues[0].detail == "index=1"
    assert issues[0].raw_value == "bad"


def test_list_element_int_overflow_records_index_and_preserves_reason() -> None:
    col = _col("BIGINT[]", pl.List(pl.Int64()), is_list=True)
    got, issues = serialize([1, 2**63, 3], col)
    assert got == [1, None, 3]
    assert issues[0].detail == "index=1: int64 overflow"
    assert issues[0].issue_type == "invalid_value"


def test_bool_is_checked_before_int() -> None:
    # bool is an int subclass in Python; `isinstance(value, bool)` must be checked
    # before `isinstance(value, int)` so a BOOLEAN column keeps a Python bool
    # rather than degrading to 1/0. A future `int(value)`-style coercion of the
    # int branch would flip this to fail.
    got, issues = serialize(True, _col("BOOLEAN", pl.Boolean()))
    assert got is True
    assert type(got) is bool
    assert issues == []


@pytest.mark.parametrize(
    ("is_list", "value", "expected_detail"),
    [
        (True, "not-a-list", "expected list"),
        (False, ["a", "b"], "unexpected list"),
    ],
    ids=["list-column-given-scalar", "scalar-column-given-list"],
)
def test_shape_mismatch(is_list: bool, value: object, expected_detail: str) -> None:
    col = _col("VARCHAR[]" if is_list else "VARCHAR", pl.Utf8(), is_list=is_list)
    got, issues = serialize(value, col)
    assert got is None
    assert len(issues) == 1
    assert issues[0].issue_type == "invalid_value"
    assert issues[0].detail == expected_detail


def test_list_element_none_and_empty_cell_null_without_issue() -> None:
    from latch.registry.upstream_types.values import EmptyCell

    col = _col("VARCHAR[]", pl.List(pl.Utf8()), is_list=True)
    got, issues = serialize(["a", None, EmptyCell()], col)
    assert got == ["a", None, None]
    assert issues == []


def test_record_get_name_offline_returns_none_when_uncached() -> None:
    from latch.registry.record import Record

    # No network call: load_if_missing=False returns None for an uncached record
    # rather than triggering `Record.load()`, keeping serialization pure/offline.
    got, issues = serialize(Record(id="123"), _col())
    assert got is None
    assert issues == []


def test_record_serializes_to_primed_cached_name() -> None:
    from latch.registry.record import Record

    record = Record(id="123")
    record._cache.name = "sample-42"  # simulate a name already loaded elsewhere
    got, issues = serialize(record, _col())
    assert got == "sample-42"
    assert issues == []


def test_latch_file_and_dir_serialize_to_path_string() -> None:
    from latch.types.directory import LatchDir
    from latch.types.file import LatchFile

    file_got, file_issues = serialize(LatchFile("/tmp/foo.txt"), _col())
    dir_got, dir_issues = serialize(LatchDir("/tmp/bar"), _col())
    assert file_got == "/tmp/foo.txt"
    assert dir_got == "/tmp/bar"
    assert file_issues == []
    assert dir_issues == []
