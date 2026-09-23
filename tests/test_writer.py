import datetime as dt
from collections.abc import Iterator
from collections.abc import Mapping

import duckdb
import polars as pl
import pytest

from latch_registry_export.dependencies import resolve_dependencies
from latch_registry_export.errors import ConversionError
from latch_registry_export.errors import DuplicateRecordNameError
from latch_registry_export.errors import EmptyRecordNameError
from latch_registry_export.schema import ColumnSchema
from latch_registry_export.schema import TableSchema
from latch_registry_export.writer import RunMeta
from latch_registry_export.writer import write_export


def _name_col() -> ColumnSchema:
    return ColumnSchema("__name__", "name", "VARCHAR", pl.Utf8(), False, False, None, None)


def _pool_link_col() -> ColumnSchema:
    return ColumnSchema("pool", "pool", "VARCHAR", pl.Utf8(), True, False, "2", None)


class _FakeRecord:
    """A minimal stand-in for a pre-primed `latch.registry.record.Record`."""

    def __init__(
        self,
        name: str,
        values: Mapping[str, object],
        last_updated: dt.datetime | None = None,
    ) -> None:
        self._name = name
        self._values = values
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


def _run_meta(*, page_size: int = 100) -> RunMeta:
    return RunMeta(
        exported_at=dt.datetime(2024, 1, 1, 0, 0),
        tool_version="0.1.0",
        fglatch_version="x",
        latch_version="x",
        duckdb_version="x",
        polars_version="x",
        pyarrow_version="x",
        workspace_id="ws",
        workspace_name="Test",
        page_size=page_size,
    )


def test_write_export_two_linked_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema("1", "samples", "Samples", (_name_col(), _pool_link_col()))
    plan = resolve_dependencies([samples, pools])

    pages = {
        "2": [_FakeRecord("p1", {})],
        "1": [_FakeRecord("s1", {"pool": _linked_record("p1")})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT name FROM pools").fetchall() == [("p1",)]
    assert con.execute("SELECT name, pool FROM samples").fetchall() == [("s1", "p1")]
    assert con.execute(
        "SELECT row_count FROM _export_tables WHERE table_sql_name='samples'"
    ).fetchone() == (1,)

    run_row = con.execute(
        "SELECT exported_at, tool_version, fglatch_version, latch_version, duckdb_version, "
        "polars_version, pyarrow_version, workspace_id, workspace_name, page_size FROM _export_run"
    ).fetchone()
    assert run_row == (
        dt.datetime(2024, 1, 1, 0, 0),
        "0.1.0",
        "x",
        "x",
        "x",
        "x",
        "x",
        "ws",
        "Test",
        100,
    )

    pool_col_row = con.execute(
        "SELECT table_sql_name, column_sql_name, registry_key, duckdb_type, nullable, is_list, "
        "link_target_table_id, fk_status, enum_members FROM _export_columns "
        "WHERE table_sql_name='samples' AND column_sql_name='pool'"
    ).fetchone()
    assert pool_col_row == (
        "samples",
        "pool",
        "pool",
        "VARCHAR",
        True,
        False,
        "2",
        "enforced",
        None,
    )


def test_enforced_dangling_link_nulled_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema("1", "samples", "Samples", (_name_col(), _pool_link_col()))
    plan = resolve_dependencies([samples, pools])

    pages: dict[str, list[_FakeRecord]] = {
        "2": [],
        "1": [_FakeRecord("s1", {"pool": _linked_record("ghost")})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT pool FROM samples").fetchone() == (None,)
    row = con.execute(
        "SELECT issue_type, detail FROM _export_issues WHERE table_sql_name='samples'"
    ).fetchone()
    assert row == ("dangling_link", "nulled")


def test_enforced_dangling_link_unresolved_name_nulled_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An enforced link whose target name never resolved (not just missing) is nulled.

    Distinct from `test_enforced_dangling_link_nulled_and_recorded`: there the raw
    `Record` has a resolved-but-dangling name ("ghost"); here `get_name` itself
    returns `None`, so the `dangling_link` issue's raw_value is also `None`.
    """
    from latch.registry.record import Record

    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema("1", "samples", "Samples", (_name_col(), _pool_link_col()))
    plan = resolve_dependencies([samples, pools])

    unresolved = Record(id="linked-unresolved")  # never primed: get_name(...) -> None

    pages: dict[str, list[_FakeRecord]] = {
        "2": [],
        "1": [_FakeRecord("s1", {"pool": unresolved})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT pool FROM samples").fetchone() == (None,)
    row = con.execute(
        "SELECT issue_type, raw_value, detail FROM _export_issues WHERE table_sql_name='samples'"
    ).fetchone()
    assert row == ("dangling_link", None, "nulled")


def _pool_link_col_not_nullable() -> ColumnSchema:
    return ColumnSchema("pool", "pool", "VARCHAR", pl.Utf8(), False, False, "2", None)


@pytest.mark.parametrize(
    ("pool_col", "cell_kind", "expected_issue_types"),
    [
        pytest.param(_pool_link_col(), "missing_key", (), id="missing_key_no_dangling_issue"),
        pytest.param(_pool_link_col(), "explicit_none", (), id="explicit_none_no_dangling_issue"),
        pytest.param(
            _pool_link_col_not_nullable(),
            "non_nullable_empty_invalid_value",
            ("missing_required",),
            id="non_nullable_empty_invalid_value_no_dangling_issue",
        ),
    ],
)
def test_empty_enforced_link_variants_have_no_dangling_issue(
    monkeypatch: pytest.MonkeyPatch,
    pool_col: ColumnSchema,
    cell_kind: str,
    expected_issue_types: tuple[str, ...],
) -> None:
    """
    An empty (unset) enforced-link cell must NOT be flagged as `dangling_link`.

    Regression test: a naive "name did not preload" heuristic can mistake a
    genuinely empty cell for a dangling reference. `serialize` returns `None`
    with no issue for a missing key or an explicit `None`; a non-nullable empty
    `InvalidValue("")` cell raises `missing_required` instead, and never
    `dangling_link`. The writer must leave all three alone rather than treating
    an unresolved name as evidence of a dangling link.
    """
    from latch.registry.types import InvalidValue

    values: dict[str, object]
    if cell_kind == "missing_key":
        values = {}
    elif cell_kind == "explicit_none":
        values = {"pool": None}
    else:
        values = {"pool": InvalidValue(raw_value="")}

    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema("1", "samples", "Samples", (_name_col(), pool_col))
    plan = resolve_dependencies([samples, pools])

    pages: dict[str, list[_FakeRecord]] = {"2": [], "1": [_FakeRecord("s1", values)]}

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT pool FROM samples").fetchone() == (None,)
    issue_types = [
        t
        for (t,) in con.execute(
            "SELECT issue_type FROM _export_issues WHERE table_sql_name='samples'"
        ).fetchall()
    ]
    assert issue_types == list(expected_issue_types)


def test_empty_enforced_link_is_null_with_no_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An empty (unset) enforced-link cell must NOT be flagged as `dangling_link`.

    Regression test: a naive "name did not preload" heuristic can mistake a
    genuinely empty cell for a dangling reference. `serialize` returns `None`
    with no issue for `EmptyCell`; the writer must leave that alone rather than
    treating the missing name as evidence of a dangling link.
    """
    from latch.registry.upstream_types.values import EmptyCell

    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    samples = TableSchema("1", "samples", "Samples", (_name_col(), _pool_link_col()))
    plan = resolve_dependencies([samples, pools])

    pages: dict[str, list[_FakeRecord]] = {
        "2": [],
        "1": [_FakeRecord("s1", {"pool": EmptyCell()})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT pool FROM samples").fetchone() == (None,)
    assert con.execute(
        "SELECT count(*) FROM _export_issues WHERE table_sql_name='samples'"
    ).fetchone() == (0,)


def test_self_link_dangling_recorded_via_post_load_antijoin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-enforced (self) link keeps a dangling value; the post-load anti-join flags it."""
    parent_col = ColumnSchema("parent", "parent", "VARCHAR", pl.Utf8(), True, False, "1", None)
    samples = TableSchema("1", "samples", "Samples", (_name_col(), parent_col))
    plan = resolve_dependencies([samples])

    pages: dict[str, list[_FakeRecord]] = {
        "1": [_FakeRecord("s1", {"parent": _linked_record("ghost-parent")})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT parent FROM samples").fetchone() == ("ghost-parent",)
    row = con.execute(
        "SELECT issue_type, detail FROM _export_issues WHERE table_sql_name='samples'"
    ).fetchone()
    assert row == ("dangling_link", "kept")


def test_back_edge_dangling_recorded_via_post_load_antijoin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A two-table cyclic link demotes one edge to `back_edge`; its dangling value is anti-joined.

    Table `a` links to `b` (stays `enforced`); table `b` links back to `a`, which
    `resolve_dependencies` demotes to `back_edge` to break the cycle, so `b` loads
    before `a` exists. A dangling `back_edge` value must survive the load (unlike
    an `enforced` one, which is nulled inline) and be flagged by the post-load
    anti-join, distinct from the already-covered `self_link`/`list_no_fk` cases.
    """
    name_col = _name_col()
    a_to_b = ColumnSchema("b_link", "b_link", "VARCHAR", pl.Utf8(), True, False, "2", None)
    b_to_a = ColumnSchema("a_link", "a_link", "VARCHAR", pl.Utf8(), True, False, "1", None)
    a = TableSchema("1", "a", "A", (name_col, a_to_b))
    b = TableSchema("2", "b", "B", (name_col, b_to_a))
    plan = resolve_dependencies([a, b])
    assert plan.insert_order == ("b", "a")
    assert plan.fk_status_by_column[("b", "a_link")] == "back_edge"

    pages: dict[str, list[_FakeRecord]] = {
        "1": [_FakeRecord("a1", {"b_link": _linked_record("b1")})],
        "2": [_FakeRecord("b1", {"a_link": _linked_record("ghost-a")})],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([a, b], plan, conn=con, run_meta=_run_meta())

    assert con.execute("SELECT a_link FROM b").fetchone() == ("ghost-a",)
    row = con.execute(
        "SELECT table_sql_name, record_name, issue_type, raw_value, detail FROM _export_issues "
        "WHERE table_sql_name='b'"
    ).fetchone()
    assert row == ("b", "b1", "dangling_link", "ghost-a", "kept")


def test_list_no_fk_dangling_recorded_via_post_load_antijoin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An array link is never enforced; the post-load anti-join UNNESTs it per-element.

    Also exercises the mid-loop page flush (`page_size=1` with two source records).
    """
    pools_col = ColumnSchema(
        "pools", "pools", "VARCHAR[]", pl.List(pl.Utf8()), True, True, "2", None
    )
    samples = TableSchema("1", "samples", "Samples", (_name_col(), pools_col))
    pools = TableSchema("2", "pools", "Pools", (_name_col(),))
    plan = resolve_dependencies([samples, pools])

    pages: dict[str, list[_FakeRecord]] = {
        "2": [_FakeRecord("p1", {})],
        "1": [
            _FakeRecord("s1", {"pools": [_linked_record("p1"), _linked_record("ghost")]}),
            _FakeRecord("s2", {"pools": []}),
        ],
    }

    def fake_fetch(table_id: str, *, page_size: int = 100) -> Iterator[_FakeRecord]:  # noqa: ARG001
        yield from pages[table_id]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)

    con = duckdb.connect(":memory:")
    write_export([samples, pools], plan, conn=con, run_meta=_run_meta(page_size=1))

    assert con.execute("SELECT name, pools FROM samples ORDER BY name").fetchall() == [
        ("s1", ["p1", "ghost"]),
        ("s2", []),
    ]
    row = con.execute(
        "SELECT record_name, issue_type, raw_value, detail FROM _export_issues "
        "WHERE table_sql_name='samples'"
    ).fetchone()
    assert row == ("s1", "dangling_link", "ghost", "kept")


def _raise_after_one() -> Iterator[_FakeRecord]:
    yield _FakeRecord("s1", {})
    raise RuntimeError("boom: simulated SDK conversion failure")


@pytest.mark.parametrize(
    ("make_records", "expected_exception"),
    [
        pytest.param(
            lambda: iter([_FakeRecord("", {})]),
            EmptyRecordNameError,
            id="empty_record_name_rejected",
        ),
        pytest.param(
            lambda: iter([_FakeRecord("s1", {}), _FakeRecord("s1", {})]),
            DuplicateRecordNameError,
            id="duplicate_record_name_rejected",
        ),
        pytest.param(
            _raise_after_one,
            ConversionError,
            id="sdk_conversion_failure_wrapped",
        ),
    ],
)
def test_write_export_name_and_conversion_guards(
    monkeypatch: pytest.MonkeyPatch,
    make_records: object,
    expected_exception: type[Exception],
) -> None:
    t = TableSchema("1", "samples", "S", (_name_col(),))
    plan = resolve_dependencies([t])

    def fake_fetch(
        table_id: str,  # noqa: ARG001
        *,
        page_size: int = 100,  # noqa: ARG001
    ) -> Iterator[_FakeRecord]:
        yield from make_records()  # type: ignore[operator]

    monkeypatch.setattr("latch_registry_export.writer.fetch_table_records", fake_fetch)
    con = duckdb.connect(":memory:")
    with pytest.raises(expected_exception):
        write_export([t], plan, conn=con, run_meta=_run_meta())
