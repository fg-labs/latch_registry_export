from collections.abc import Mapping

import pytest

from latch_registry_export.config import TableConfig
from latch_registry_export.dependencies import resolve_dependencies
from latch_registry_export.errors import InvalidTableNameError
from latch_registry_export.errors import MissingTableError
from latch_registry_export.errors import UnknownColumnError
from latch_registry_export.schema import RECORD_NAME_COLUMN
from latch_registry_export.schema_builder import build_schemas


class _FakeColumn:
    """A minimal stand-in for a `latch.registry.types.Column`."""

    def __init__(self, upstream_type: Mapping[str, object]) -> None:
        self.upstream_type = upstream_type


def _fake_table_class(
    display_names: Mapping[str, str | None],
    columns: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> type:
    class _FakeTable:
        def __init__(self, *, id: str) -> None:  # noqa: A002 - matches the real `Table(id=...)`
            self.id = id

        def get_display_name(self) -> str | None:
            return display_names[self.id]

        def get_columns(self) -> dict[str, _FakeColumn]:
            return {key: _FakeColumn(spec) for key, spec in columns.get(self.id, {}).items()}

    return _FakeTable


def test_build_schemas_resolves_names_and_ir(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_table_cls = _fake_table_class(
        display_names={"1": "Samples", "2": "Pools"},
        columns={"1": {"conc": {"type": {"primitive": "number"}, "allowEmpty": True}}},
    )
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    schemas = build_schemas([TableConfig(id="1"), TableConfig(id="2", name="pool_table")])

    by_id = {s.table_id: s for s in schemas}
    assert by_id["1"].sql_name == "samples"
    assert by_id["2"].sql_name == "pool_table"
    data_cols = {c.registry_key: c for c in by_id["1"].columns}
    assert data_cols["conc"].duckdb_type == "DOUBLE"


def test_build_schemas_rejects_reserved_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_table_cls = _fake_table_class(display_names={"1": None}, columns={})
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    with pytest.raises(InvalidTableNameError):
        build_schemas([TableConfig(id="1", name="_export_bad")])


def test_build_schemas_rejects_name_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_table_cls = _fake_table_class(display_names={"1": "Dup", "2": "Dup"}, columns={})
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    with pytest.raises(InvalidTableNameError):
        build_schemas([TableConfig(id="1"), TableConfig(id="2")])


_STRING = {"type": {"primitive": "string"}, "allowEmpty": True}
_SELECTION_COLUMNS = {"1": {"Gene": _STRING, "Sample": _STRING, "Transcript Sequence": _STRING}}


@pytest.mark.parametrize(
    ("cfg", "expected_keys"),
    [
        pytest.param(
            TableConfig(id="1"), ["Gene", "Sample", "Transcript Sequence"], id="no-selection"
        ),
        pytest.param(
            TableConfig(id="1", include_columns=("Sample", "Gene")),
            ["Gene", "Sample"],
            id="include-keeps-only-listed-in-sorted-order",
        ),
        pytest.param(
            TableConfig(id="1", exclude_columns=("Transcript Sequence",)),
            ["Gene", "Sample"],
            id="exclude-drops-listed",
        ),
    ],
)
def test_build_schemas_selects_columns(
    monkeypatch: pytest.MonkeyPatch, cfg: TableConfig, expected_keys: list[str]
) -> None:
    """Column selection keeps the `name` PK first, then the selected data columns."""
    fake_table_cls = _fake_table_class(display_names={"1": "T"}, columns=_SELECTION_COLUMNS)
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    (schema,) = build_schemas([cfg])

    assert schema.columns[0].sql_name == RECORD_NAME_COLUMN
    assert [c.registry_key for c in schema.columns[1:]] == expected_keys


@pytest.mark.parametrize(
    ("cfg", "match"),
    [
        pytest.param(
            TableConfig(id="1", include_columns=("Gene", "gene", "Nope")),
            r"table 1 has no column\(s\) \['Nope', 'gene'\].*include_columns",
            id="include-unknown-and-wrong-case",
        ),
        pytest.param(
            TableConfig(id="1", exclude_columns=("Transcript Seq",)),
            r"table 1 has no column\(s\) \['Transcript Seq'\].*exclude_columns",
            id="exclude-unknown",
        ),
        pytest.param(
            TableConfig(id="1", exclude_columns=("Gene", "Sample", "Transcript Sequence")),
            "excludes every data column",
            id="exclude-everything",
        ),
    ],
)
def test_build_schemas_rejects_bad_column_selection(
    monkeypatch: pytest.MonkeyPatch, cfg: TableConfig, match: str
) -> None:
    fake_table_cls = _fake_table_class(display_names={"1": "T"}, columns=_SELECTION_COLUMNS)
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    with pytest.raises(UnknownColumnError, match=match):
        build_schemas([cfg])


def test_build_schemas_excluding_link_column_drops_its_fk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A link to a table outside the export is fine once that link column is excluded."""
    link_to_missing = {"type": {"primitive": "link", "experimentId": "99"}, "allowEmpty": True}
    fake_table_cls = _fake_table_class(
        display_names={"1": "T"}, columns={"1": {"Gene": _STRING, "Parent": link_to_missing}}
    )
    monkeypatch.setattr("latch.registry.table.Table", fake_table_cls)

    with pytest.raises(MissingTableError):
        resolve_dependencies(build_schemas([TableConfig(id="1")]))

    plan = resolve_dependencies(build_schemas([TableConfig(id="1", exclude_columns=("Parent",))]))
    assert plan.link_plans == ()
