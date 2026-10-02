from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest

from latch_registry_export import __version__
from latch_registry_export.config import TableConfig
from latch_registry_export.config import load_table_configs
from latch_registry_export.generate import DiscoveredTable
from latch_registry_export.generate import discover_tables
from latch_registry_export.generate import to_config_toml


def _table(
    table_id: str,
    display_name: str | None,
    column_keys: Sequence[str] = ("a",),
    project_name: str = "Project",
) -> DiscoveredTable:
    return DiscoveredTable(
        id=table_id,
        project_name=project_name,
        display_name=display_name,
        column_keys=tuple(column_keys),
    )


def _round_trip(tmp_path: Path, tables: Sequence[DiscoveredTable]) -> list[TableConfig]:
    path = tmp_path / "tables.toml"
    path.write_text(to_config_toml(tables, workspace_id="ws-1"), encoding="utf-8")
    return load_table_configs(path)


@pytest.mark.parametrize(
    ("tables", "expected"),
    [
        pytest.param(
            [_table("1", "Samples", ["Gene", "Sample"])],
            [TableConfig(id="1", name="samples", include_columns=("Gene", "Sample"))],
            id="name-from-display-name-and-all-columns-in-registry-order",
        ),
        pytest.param(
            [_table("1", "S", ['Quote "q"', "back\\slash", "Ünïcode key", "tab\there"])],
            [
                TableConfig(
                    id="1",
                    name="s",
                    include_columns=('Quote "q"', "back\\slash", "Ünïcode key", "tab\there"),
                )
            ],
            id="column-keys-needing-toml-escapes",
        ),
        pytest.param(
            [_table("7", None), _table("8", "")],
            [
                TableConfig(id="7", name="table_7", include_columns=("a",)),
                TableConfig(id="8", name="table_8", include_columns=("a",)),
            ],
            id="missing-or-empty-display-name-falls-back-to-table-id",
        ),
        pytest.param(
            [_table("1", "Samples!"), _table("2", "samples"), _table("3", "Runs")],
            [
                TableConfig(id="1", name="samples", include_columns=("a",)),
                TableConfig(id="2", name="samples_2", include_columns=("a",)),
                TableConfig(id="3", name="runs", include_columns=("a",)),
            ],
            id="lowest-id-keeps-colliding-name-and-others-get-id-suffix",
        ),
        pytest.param(
            [_table("10", "A"), _table("9", "A")],
            [
                TableConfig(id="10", name="a_10", include_columns=("a",)),
                TableConfig(id="9", name="a", include_columns=("a",)),
            ],
            id="lowest-numeric-id-keeps-name-regardless-of-input-order",
        ),
        pytest.param(
            [_table("3", "サンプル"), _table("4", "試料"), _table("5", "2024 runs")],
            [
                TableConfig(id="3", name="table_3", include_columns=("a",)),
                TableConfig(id="4", name="table_4", include_columns=("a",)),
                TableConfig(id="5", name="col_2024_runs", include_columns=("a",)),
            ],
            id="display-name-without-ascii-letters-or-digits-falls-back-to-table-id",
        ),
        pytest.param(
            [_table("5", "A"), _table("6", "A"), _table("9", "A 6")],
            [
                TableConfig(id="5", name="a", include_columns=("a",)),
                TableConfig(id="6", name="a_6", include_columns=("a",)),
                TableConfig(id="9", name="a_6_9", include_columns=("a",)),
            ],
            id="suffixed-name-colliding-with-natural-name-is-suffixed-again",
        ),
        pytest.param(
            [_table("1", "Empty", [])],
            [TableConfig(id="1", name="empty")],
            id="table-without-columns-omits-include-columns",
        ),
        pytest.param(
            [_table("1", "Full"), _table("2", "Empty", [])],
            [
                TableConfig(id="1", name="full", include_columns=("a",)),
                TableConfig(id="2", name="empty"),
            ],
            id="table-without-columns-after-another-table-stays-a-separate-entry",
        ),
        pytest.param(
            [_table("1", "Multi\nline\r\ntable", project_name="Proj\nX")],
            [TableConfig(id="1", name="multi_line_table", include_columns=("a",))],
            id="newlines-in-display-names-do-not-break-comments",
        ),
        pytest.param(
            [_table("1", "A\x0bB\x00", project_name="P\x7f\x1fQ\tR")],
            [TableConfig(id="1", name="a_b", include_columns=("a",))],
            id="control-characters-in-display-names-do-not-break-comments",
        ),
    ],
)
def test_to_config_toml_round_trips(
    tmp_path: Path, tables: Sequence[DiscoveredTable], expected: list[TableConfig]
) -> None:
    assert _round_trip(tmp_path, tables) == expected


def test_to_config_toml_comments_describe_workspace_and_each_table() -> None:
    text = to_config_toml(
        [
            _table("1", "Samples", project_name="Assay"),
            _table("2", None, project_name="Assay"),
            _table("3", "Runs", [], project_name="Assay"),
        ],
        workspace_id="ws-1",
    )
    assert f"latch_registry_export {__version__}" in text
    assert "from workspace ws-1" in text
    assert '# Assay / Samples\n[[tables]]\nid = "1"' in text
    assert '# Assay / (no display name)\n[[tables]]\nid = "2"' in text
    assert (
        "# Assay / Runs\n# No columns yet, so the export includes every column added later.\n"
        '[[tables]]\nid = "3"'
    ) in text


def test_to_config_toml_rejects_empty_table_list() -> None:
    with pytest.raises(ValueError, match="no Registry tables"):
        to_config_toml([], workspace_id="ws-1")


class _FakeColumn:
    pass


class _FakeTable:
    def __init__(self, table_id: str, display_name: str | None, column_keys: Sequence[str]) -> None:
        self.id = table_id
        self._display_name = display_name
        self._column_keys = column_keys

    def get_display_name(self) -> str | None:
        return self._display_name

    def get_columns(self) -> dict[str, _FakeColumn] | None:
        return {key: _FakeColumn() for key in self._column_keys} if self._column_keys else None


class _FakeProject:
    def __init__(self, display_name: str, tables: Sequence[_FakeTable]) -> None:
        self._display_name = display_name
        self._tables = list(tables)

    def get_display_name(self) -> str:
        return self._display_name

    def list_tables(self) -> list[_FakeTable]:
        return self._tables


def _fake_account_class(projects: Mapping[str, Sequence[_FakeTable]]) -> type:
    class _FakeAccount:
        def __init__(self, *, id: str) -> None:  # noqa: A002 - matches the real `Account(id=...)`
            self.id = id

        def list_registry_projects(self) -> list[_FakeProject]:
            assert self.id == "ws-9"
            return [_FakeProject(name, tables) for name, tables in projects.items()]

    return _FakeAccount


def test_discover_tables_lists_every_table_sorted_by_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_account_cls = _fake_account_class({
        "Assay": [_FakeTable("100", "Runs", ["z", "a"]), _FakeTable("9", "Samples", ["k"])],
        "Empty project": [],
        "Other": [_FakeTable("20", None, [])],
    })
    monkeypatch.setattr("latch.account.Account", fake_account_cls)

    assert discover_tables(workspace_id="ws-9") == [
        DiscoveredTable(id="9", project_name="Assay", display_name="Samples", column_keys=("k",)),
        DiscoveredTable(id="20", project_name="Other", display_name=None, column_keys=()),
        DiscoveredTable(
            id="100", project_name="Assay", display_name="Runs", column_keys=("z", "a")
        ),
    ]
