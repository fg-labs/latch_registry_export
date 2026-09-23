from collections.abc import Mapping

import pytest

from latch_registry_export.config import TableConfig
from latch_registry_export.errors import InvalidTableNameError
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
