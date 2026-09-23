import itertools
from collections.abc import Callable

import polars as pl
import pytest

from latch_registry_export.dependencies import resolve_dependencies
from latch_registry_export.errors import MissingTableError
from latch_registry_export.schema import ColumnSchema
from latch_registry_export.schema import TableSchema


def _name_col() -> ColumnSchema:
    return ColumnSchema(
        "__latch_record_name__", "name", "VARCHAR", pl.Utf8(), False, False, None, None
    )


def _link_col(sql_name: str, target: str, *, is_list: bool = False) -> ColumnSchema:
    return ColumnSchema(
        registry_key=sql_name,
        sql_name=sql_name,
        duckdb_type="VARCHAR[]" if is_list else "VARCHAR",
        polars_dtype=pl.List(pl.Utf8()) if is_list else pl.Utf8(),
        nullable=True,
        is_list=is_list,
        link_target_table_id=target,
        enum_members=None,
    )


def _table(table_id: str, sql_name: str, *link_cols: ColumnSchema) -> TableSchema:
    return TableSchema(table_id, sql_name, sql_name, (_name_col(), *link_cols))


@pytest.mark.parametrize(
    ("build_schemas", "status_key", "expected_status", "expected_insert_order"),
    [
        pytest.param(
            lambda: [_table("1", "samples", _link_col("pool", "2")), _table("2", "pools")],
            ("samples", "pool"),
            "enforced",
            ("pools", "samples"),
            id="scalar_link_to_other_table_is_enforced_and_orders_parent_first",
        ),
        pytest.param(
            lambda: [_table("1", "samples", _link_col("parent", "1"))],
            ("samples", "parent"),
            "self_link",
            ("samples",),
            id="self_referential_scalar_link_is_self_link_with_no_ordering_wedge",
        ),
        pytest.param(
            lambda: [
                _table("1", "samples", _link_col("pools", "2", is_list=True)),
                _table("2", "pools"),
            ],
            ("samples", "pools"),
            "list_no_fk",
            None,
            id="array_link_is_list_no_fk_regardless_of_target",
        ),
    ],
)
def test_link_column_fk_status_by_shape(
    build_schemas: Callable[[], list[TableSchema]],
    status_key: tuple[str, str],
    expected_status: str,
    expected_insert_order: tuple[str, ...] | None,
) -> None:
    plan = resolve_dependencies(build_schemas())
    assert plan.fk_status_by_column[status_key] == expected_status
    if expected_insert_order is not None:
        assert plan.insert_order == expected_insert_order


@pytest.mark.parametrize("is_list", [False, True], ids=["scalar_link", "array_link"])
def test_missing_target_table_fails(*, is_list: bool) -> None:
    samples = _table("1", "samples", _link_col("pool", "999", is_list=is_list))
    with pytest.raises(MissingTableError, match="999"):
        resolve_dependencies([samples])


def test_multiple_missing_target_tables_are_all_reported_in_one_error() -> None:
    """A missing-target error names every offending link, not just the first found."""
    samples = _table(
        "1", "samples", _link_col("pool", "999"), _link_col("batch", "888", is_list=True)
    )
    runs = _table("2", "runs", _link_col("sample", "1"), _link_col("pool", "999"))
    with pytest.raises(MissingTableError) as exc_info:
        resolve_dependencies([samples, runs])
    message = str(exc_info.value)
    assert "999" in message
    assert "888" in message
    assert "3 link(s)" in message


def test_two_node_cycle_degrades_deterministic_back_edge() -> None:
    a = _table("1", "a", _link_col("b_ref", "2"))
    b = _table("2", "b", _link_col("a_ref", "1"))
    plan = resolve_dependencies([a, b])
    # exactly one of the two edges is the back_edge, deterministically the one whose
    # (source_id, target_id) sorts last: ("2","1") > ("1","2") -> b.a_ref is back_edge
    assert plan.fk_status_by_column[("b", "a_ref")] == "back_edge"
    assert plan.fk_status_by_column[("a", "b_ref")] == "enforced"


def test_three_node_cycle_breaks_deterministic_back_edge() -> None:
    # Cycle a(1) -> b(2) -> c(3) -> a(1). The in-cycle edge whose (source_id,
    # target_id) sorts last is ("3","1") = c.a_ref, so that one demotes.
    a = _table("1", "a", _link_col("b_ref", "2"))
    b = _table("2", "b", _link_col("c_ref", "3"))
    c = _table("3", "c", _link_col("a_ref", "1"))
    plan = resolve_dependencies([a, b, c])
    assert plan.fk_status_by_column[("c", "a_ref")] == "back_edge"
    assert plan.fk_status_by_column[("a", "b_ref")] == "enforced"
    assert plan.fk_status_by_column[("b", "c_ref")] == "enforced"
    assert plan.insert_order == ("c", "b", "a")


def test_two_independent_two_node_cycles_each_break_separately() -> None:
    a = _table("1", "a", _link_col("b_ref", "2"))
    b = _table("2", "b", _link_col("a_ref", "1"))
    c = _table("3", "c", _link_col("d_ref", "4"))
    d = _table("4", "d", _link_col("c_ref", "3"))
    plan = resolve_dependencies([a, b, c, d])
    assert plan.fk_status_by_column[("b", "a_ref")] == "back_edge"
    assert plan.fk_status_by_column[("a", "b_ref")] == "enforced"
    assert plan.fk_status_by_column[("d", "c_ref")] == "back_edge"
    assert plan.fk_status_by_column[("c", "d_ref")] == "enforced"


def test_two_columns_same_source_and_target_both_demote_on_shared_edge() -> None:
    # b has two link columns to a, both collapse to the single graph edge b->a.
    # When that edge breaks the cycle, both columns demote together.
    a = _table("1", "a", _link_col("b_ref", "2"))
    b = _table("2", "b", _link_col("a_ref_1", "1"), _link_col("a_ref_2", "1"))
    plan = resolve_dependencies([a, b])
    assert plan.fk_status_by_column[("b", "a_ref_1")] == "back_edge"
    assert plan.fk_status_by_column[("b", "a_ref_2")] == "back_edge"
    assert plan.fk_status_by_column[("a", "b_ref")] == "enforced"


def test_resolution_is_input_order_invariant() -> None:
    pools = _table("2", "pools")
    samples = _table("1", "samples", _link_col("pool", "2"))
    runs = _table("3", "runs", _link_col("sample", "1"))
    schemas = [pools, samples, runs]

    plans = [resolve_dependencies(list(perm)) for perm in itertools.permutations(schemas)]

    first = plans[0]
    for plan in plans[1:]:
        assert plan.insert_order == first.insert_order
        assert plan.fk_status_by_column == first.fk_status_by_column
