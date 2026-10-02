import os
import stat
from pathlib import Path
from typing import IO
from typing import Any

import pytest
from latch.account import AccountNotFoundError
from pytest import CaptureFixture
from pytest import LogCaptureFixture
from pytest import MonkeyPatch

from latch_registry_export.config import TableConfig
from latch_registry_export.config import load_table_configs
from latch_registry_export.errors import OutputExistsError
from latch_registry_export.generate import DiscoveredTable
from latch_registry_export.main import run
from latch_registry_export.tools.generate_config import generate_config_cli

_TABLES = [
    DiscoveredTable(id="1", project_name="P", display_name="Samples", column_keys=("Gene",)),
    DiscoveredTable(id="2", project_name="P", display_name="Runs", column_keys=()),
]


@pytest.fixture
def stubbed_discovery(monkeypatch: MonkeyPatch) -> None:
    """Stub workspace discovery so the CLI never calls Latch."""
    monkeypatch.setattr(
        "latch_registry_export.tools.generate_config.discover_tables",
        lambda *, workspace_id: _TABLES if workspace_id == "ws-1" else [],
    )
    monkeypatch.setattr("latch.utils.current_workspace", lambda: "ws-1")


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_writes_loadable_config(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    output = tmp_path / "tables.toml"

    generate_config_cli(output=output)

    assert load_table_configs(output) == [
        TableConfig(id="1", name="samples", include_columns=("Gene",)),
        TableConfig(id="2", name="runs"),
    ]
    assert "ws-1" in output.read_text(encoding="utf-8")
    assert f"Wrote 2 table(s) to {output}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overwrite", "expect_error"),
    [
        pytest.param(False, True, id="existing-output-refused-by-default"),
        pytest.param(True, False, id="existing-output-replaced-with-overwrite"),
    ],
)
@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_existing_output(
    tmp_path: Path, overwrite: bool, expect_error: bool
) -> None:
    output = tmp_path / "tables.toml"
    output.write_text("# hand-edited\n")

    if expect_error:
        with pytest.raises(OutputExistsError, match="use overwrite"):
            generate_config_cli(output=output, overwrite=overwrite)
        assert output.read_text() == "# hand-edited\n"
    else:
        generate_config_cli(output=output, overwrite=overwrite)
        assert "[[tables]]" in output.read_text()


@pytest.mark.usefixtures("stubbed_discovery")
def test_cli_runs_generate_config_subcommand(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    output = tmp_path / "tables.toml"
    monkeypatch.setattr(
        "sys.argv", ["latch_registry_export", "generate-config", "--output", str(output)]
    )

    run()

    assert len(load_table_configs(output)) == 2


def test_cli_surfaces_inaccessible_workspace_and_exits_one(
    monkeypatch: MonkeyPatch, tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    def failing_discovery(*, workspace_id: str) -> list[DiscoveredTable]:
        raise AccountNotFoundError(
            f"account does not exist or you lack permissions: id={workspace_id}"
        )

    monkeypatch.setattr(
        "latch_registry_export.tools.generate_config.discover_tables", failing_discovery
    )
    monkeypatch.setattr("latch.utils.current_workspace", lambda: "9")
    monkeypatch.setattr(
        "sys.argv",
        ["latch_registry_export", "generate-config", "--output", str(tmp_path / "t.toml")],
    )

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
        run()

    assert exc_info.value.code == 1
    assert "lack permissions" in caplog.text


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_creates_missing_parent_directories(tmp_path: Path) -> None:
    output = tmp_path / "configs" / "nested" / "tables.toml"

    generate_config_cli(output=output)

    assert len(load_table_configs(output)) == 2


def test_generate_config_cli_refuses_output_created_during_discovery(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A file that appears while the workspace is listed is not replaced without `overwrite`."""
    output = tmp_path / "tables.toml"

    def discovery_racing_another_writer(*, workspace_id: str) -> list[DiscoveredTable]:
        output.write_text(f"# written by another run in {workspace_id}\n")
        return _TABLES

    monkeypatch.setattr(
        "latch_registry_export.tools.generate_config.discover_tables",
        discovery_racing_another_writer,
    )
    monkeypatch.setattr("latch.utils.current_workspace", lambda: "ws-1")

    with pytest.raises(OutputExistsError, match="use overwrite"):
        generate_config_cli(output=output)
    assert output.read_text() == "# written by another run in ws-1\n"


def test_generate_config_cli_leaves_no_file_when_encoding_fails(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A display name that cannot encode as UTF-8 leaves no file to block a retry."""
    output = tmp_path / "tables.toml"
    lone_surrogate = DiscoveredTable(
        id="1", project_name="P", display_name="bad\ud800", column_keys=("a",)
    )
    monkeypatch.setattr(
        "latch_registry_export.tools.generate_config.discover_tables",
        lambda *, workspace_id: [lone_surrogate] if workspace_id == "ws-1" else [],
    )
    monkeypatch.setattr("latch.utils.current_workspace", lambda: "ws-1")

    with pytest.raises(UnicodeEncodeError):
        generate_config_cli(output=output)
    assert not output.exists()


def _fail_writes_to(monkeypatch: MonkeyPatch, *, mode_flag: str) -> None:
    """Make `write` fail on files that `Path.open` opens with `mode_flag` in the mode."""
    real_open = Path.open

    class _FailingWriter:
        def __init__(self, fh: IO[bytes]) -> None:
            self._fh = fh

        def __enter__(self) -> "_FailingWriter":
            return self

        def __exit__(self, *_exc: object) -> None:
            self._fh.close()

        def write(self, _data: bytes) -> int:
            raise OSError("disk full")

    def open_with_failing_write(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        fh = real_open(self, mode, *args, **kwargs)
        return _FailingWriter(fh) if mode_flag in mode else fh

    monkeypatch.setattr(Path, "open", open_with_failing_write)


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_failed_exclusive_write_leaves_no_file(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A partial file from a failed write is removed, so it does not block a retry."""
    output = tmp_path / "tables.toml"
    _fail_writes_to(monkeypatch, mode_flag="x")

    with pytest.raises(OSError, match="disk full"):
        generate_config_cli(output=output)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_failed_overwrite_keeps_old_config(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "tables.toml"
    output.write_text("# hand-edited\n")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", fail)

    with pytest.raises(OSError, match="disk full"):
        generate_config_cli(output=output, overwrite=True)
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == {"tables.toml": "# hand-edited\n"}


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_overwrite_writes_through_symlink(tmp_path: Path) -> None:
    target = tmp_path / "shared" / "tables.toml"
    target.parent.mkdir()
    target.write_text("# hand-edited\n")
    link = tmp_path / "tables.toml"
    link.symlink_to(target)

    generate_config_cli(output=link, overwrite=True)

    assert link.is_symlink()
    assert len(load_table_configs(target)) == 2


@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_overwrite_keeps_file_mode(tmp_path: Path) -> None:
    output = tmp_path / "tables.toml"
    output.write_text("# hand-edited\n")
    output.chmod(0o640)  # neither the umask default nor `mkstemp`'s 0600

    generate_config_cli(output=output, overwrite=True)

    assert stat.S_IMODE(output.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    "overwrite",
    [
        pytest.param(False, id="exclusive-create"),
        pytest.param(True, id="overwrite-to-new-file"),
    ],
)
@pytest.mark.usefixtures("stubbed_discovery")
def test_generate_config_cli_new_file_mode_follows_umask(tmp_path: Path, overwrite: bool) -> None:
    """A new config gets the umask mode, not the owner-only mode of a temporary file."""
    output = tmp_path / "tables.toml"
    umask = os.umask(0o022)
    try:
        generate_config_cli(output=output, overwrite=overwrite)
    finally:
        os.umask(umask)

    assert stat.S_IMODE(output.stat().st_mode) == 0o644
