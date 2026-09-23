"""Tests for the CLI entry point."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from latch_sdk_gql import AuthenticationError
from pytest import CaptureFixture
from pytest import LogCaptureFixture
from pytest import MonkeyPatch

from latch_registry_export import __version__
from latch_registry_export.api import ExportReport
from latch_registry_export.config import TableConfig
from latch_registry_export.errors import OutputExistsError
from latch_registry_export.main import run
from latch_registry_export.main import setup_logging
from latch_registry_export.tools.export import export_cli


def _report(
    *,
    output: Path,
    row_counts: dict[str, int],
    issue_counts: dict[str, int] | None = None,
    degraded_links: tuple[tuple[str, str], ...] = (),
) -> ExportReport:
    """Build a minimal but real `ExportReport` for use as a stub return value."""
    return ExportReport(
        output=output,
        tool_version="0.0.0",
        dependency_versions={},
        workspace_id="ws-1",
        workspace_name="ws-1",
        row_counts=row_counts,
        issue_counts=issue_counts or {},
        degraded_links=degraded_links,
    )


@pytest.fixture
def stubbed_export(monkeypatch: MonkeyPatch) -> dict[str, object]:
    """
    Stub `export`/`load_report` on `main`, recording calls, and return the recording dict.

    The stubbed `export` records its arguments; the stubbed `load_report` returns a
    real `ExportReport` built from `row_counts`/`issue_counts`/`degraded_links` set
    on the returned `called` dict before `export_cli` is invoked (defaults: empty).
    """
    called: dict[str, object] = {
        "row_counts": {},
        "issue_counts": {},
        "degraded_links": (),
    }

    def fake_export(
        tables: list[TableConfig], *, output: Path, page_size: int, overwrite: bool
    ) -> None:
        called["tables"] = tables
        called["output"] = output
        called["page_size"] = page_size
        called["overwrite"] = overwrite

    def fake_load_report(path: Path) -> ExportReport:
        called["report_path"] = path
        return _report(
            output=path,
            row_counts=called["row_counts"],  # type: ignore[arg-type]
            issue_counts=called["issue_counts"],  # type: ignore[arg-type]
            degraded_links=called["degraded_links"],  # type: ignore[arg-type]
        )

    monkeypatch.setattr("latch_registry_export.tools.export.export", fake_export)
    monkeypatch.setattr("latch_registry_export.tools.export.load_report", fake_load_report)
    return called


@pytest.fixture
def table_config(tmp_path: Path) -> Iterator[Path]:
    """Write a minimal `tables.toml` with one table to `tmp_path` and yield its path."""
    config = tmp_path / "t.toml"
    config.write_text('[[tables]]\nid = "1"\n')
    yield config


def test_export_cli_invokes_export_and_prints_summary(
    tmp_path: Path,
    stubbed_export: dict[str, object],
    table_config: Path,
    capsys: CaptureFixture[str],
) -> None:
    """`export_cli` loads the config, calls `export`, then prints the report."""
    stubbed_export["row_counts"] = {"samples": 3, "runs": 1}
    stubbed_export["issue_counts"] = {"conversion": 2}
    stubbed_export["degraded_links"] = (("samples", "parent"),)
    output = tmp_path / "r.duckdb"

    export_cli(config=table_config, output=output, page_size=50, overwrite=True)

    tables = stubbed_export["tables"]
    assert isinstance(tables, list)
    assert len(tables) == 1
    assert tables[0].id == "1"
    assert stubbed_export["output"] == output
    assert stubbed_export["page_size"] == 50
    assert stubbed_export["overwrite"] is True
    assert stubbed_export["report_path"] == output

    out = capsys.readouterr().out
    assert f"Exported to {output}" in out
    assert "samples: 3" in out
    assert "runs: 1" in out
    assert "issue - conversion: 2" in out
    assert "degraded link - samples.parent" in out


def test_export_cli_uses_default_page_size_and_overwrite(
    tmp_path: Path, stubbed_export: dict[str, object], table_config: Path
) -> None:
    """Defaults for `page_size` and `overwrite` are passed through to `export`."""
    output = tmp_path / "r.duckdb"

    export_cli(config=table_config, output=output)

    assert stubbed_export["page_size"] == 100
    assert stubbed_export["overwrite"] is False


def test_export_cli_omits_empty_issue_and_degraded_sections(
    tmp_path: Path,
    stubbed_export: dict[str, object],
    table_config: Path,
    capsys: CaptureFixture[str],
) -> None:
    """No issues and no degraded links means those summary lines are not printed."""
    stubbed_export["row_counts"] = {"samples": 3}
    output = tmp_path / "r.duckdb"

    export_cli(config=table_config, output=output)

    out = capsys.readouterr().out
    assert "samples: 3" in out
    assert "issue" not in out
    assert "degraded" not in out


@pytest.fixture
def restore_log_levels() -> Iterator[None]:
    """Restore the root and `gql` logger levels after a test changes them."""
    loggers = [logging.getLogger(), logging.getLogger("gql")]
    original_levels = [lg.level for lg in loggers]
    yield
    for lg, level in zip(loggers, original_levels, strict=True):
        lg.setLevel(level)


@pytest.mark.parametrize(
    ("log_level", "expected"),
    [
        pytest.param(None, logging.INFO, id="default-info"),
        pytest.param("DEBUG", logging.DEBUG, id="debug"),
        pytest.param("WARNING", logging.WARNING, id="warning"),
        pytest.param("ERROR", logging.ERROR, id="error"),
    ],
)
@pytest.mark.usefixtures("stubbed_export", "restore_log_levels")
def test_cli_log_level_sets_root_level(
    monkeypatch: MonkeyPatch,
    table_config: Path,
    tmp_path: Path,
    log_level: str | None,
    expected: int,
) -> None:
    """`--log-level` sets the root logger level; gql stays capped at WARNING."""
    argv = ["latch_registry_export", "--config", str(table_config)]
    argv += ["--output", str(tmp_path / "r.duckdb")]
    if log_level is not None:
        argv += ["--log-level", log_level]
    monkeypatch.setattr("sys.argv", argv)

    run()

    assert logging.getLogger().level == expected
    assert not logging.getLogger("gql.transport.requests").isEnabledFor(logging.INFO)


def test_cli_rejects_unknown_log_level(monkeypatch: MonkeyPatch, table_config: Path) -> None:
    """An unknown `--log-level` is a usage error (exit 2)."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "latch_registry_export",
            "--config",
            str(table_config),
            "--output",
            "x.duckdb",
            "--log-level",
            "LOUD",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 2


def test_cli_help_lists_expected_flags(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    """`run()` with `--help` documents every CLI flag."""
    monkeypatch.setattr("sys.argv", ["latch_registry_export", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--config", "--output", "--page-size", "--overwrite", "--log-level"):
        assert flag in out


def test_cli_version_prints_version_and_exits_zero(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    """`run()` with `--version` prints the package version and exits 0."""
    monkeypatch.setattr("sys.argv", ["latch_registry_export", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        run()
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


@pytest.mark.parametrize(
    "raised",
    [
        OutputExistsError("{output} already exists"),
        ValueError("page_size must be >= 1"),
        OSError("disk full"),
        duckdb.Error("Catalog Error: table _export_run does not exist"),
        AuthenticationError("Unable to find credentials to connect to gql server, aborting"),
    ],
    ids=["export-error", "value-error", "os-error", "duckdb-error", "unauthenticated"],
)
def test_cli_surfaces_known_errors_and_exits_one(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    table_config: Path,
    caplog: LogCaptureFixture,
    raised: Exception,
) -> None:
    """Each error type `run()` documents catching is logged and exits with status 1."""
    output = tmp_path / "r.duckdb"
    message = str(raised).format(output=output)

    def failing_export(_tables: list[TableConfig], **_kwargs: object) -> None:
        raise type(raised)(message)

    monkeypatch.setattr("latch_registry_export.tools.export.export", failing_export)
    monkeypatch.setattr(
        "sys.argv",
        ["latch_registry_export", "--config", str(table_config), "--output", str(output)],
    )

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
        run()

    assert exc_info.value.code == 1
    assert message in caplog.text


def test_cli_lets_unexpected_errors_propagate(
    monkeypatch: MonkeyPatch,
    table_config: Path,
    tmp_path: Path,
) -> None:
    """An exception `run()` does not document catching propagates with a traceback."""

    def failing_export(_tables: list[TableConfig], **_kwargs: object) -> None:
        raise RuntimeError("genuinely unexpected")

    monkeypatch.setattr("latch_registry_export.tools.export.export", failing_export)
    output = tmp_path / "r.duckdb"
    monkeypatch.setattr(
        "sys.argv",
        ["latch_registry_export", "--config", str(table_config), "--output", str(output)],
    )

    with pytest.raises(RuntimeError, match="genuinely unexpected"):
        run()


def test_setup_logging_silences_gql_payload_logs() -> None:
    """Gql logs full request/response bodies at INFO; `setup_logging` must suppress them."""
    gql_logger = logging.getLogger("gql")
    original_level = gql_logger.level
    try:
        setup_logging()
        transport_logger = logging.getLogger("gql.transport.requests")
        assert not transport_logger.isEnabledFor(logging.INFO)
        assert transport_logger.isEnabledFor(logging.WARNING)
    finally:
        gql_logger.setLevel(original_level)
