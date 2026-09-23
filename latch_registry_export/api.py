"""Top-level export entry point and report reader."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import os
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from latch_registry_export.config import TableConfig
from latch_registry_export.dependencies import DEGRADED_FK_STATUSES
from latch_registry_export.dependencies import resolve_dependencies
from latch_registry_export.errors import OutputExistsError
from latch_registry_export.schema_builder import build_schemas
from latch_registry_export.serialize import to_naive_utc
from latch_registry_export.writer import RunMeta
from latch_registry_export.writer import write_export


@dataclass(frozen=True)
class ExportReport:
    """A summary of one export run, read back from the `_export_*` tables."""

    output: Path
    tool_version: str
    dependency_versions: Mapping[str, str]
    workspace_id: str
    row_counts: Mapping[str, int]
    issue_counts: Mapping[str, int]
    degraded_links: tuple[tuple[str, str], ...]


def _workspace_meta() -> tuple[str, str]:
    """
    Return the current Latch workspace id and name.

    Returns:
        A `(workspace_id, workspace_name)` pair. `workspace_name` currently
        mirrors `workspace_id`: a forward-looking placeholder until a separate
        display-name lookup is wired up, not a fallback for a missing name.
    """
    from latch.utils import current_workspace

    ws_id = current_workspace()
    return ws_id, ws_id


def export(
    tables: Sequence[TableConfig],
    *,
    output: Path,
    page_size: int = 100,
    overwrite: bool = False,
) -> None:
    """
    Export the configured Registry tables to a DuckDB file at `output`.

    Writes to a unique temporary file in the output's directory first, then
    atomically replaces `output` on success. The temporary file is discarded on
    any failure, so a failed export never leaves a partial or corrupt file at
    `output`.

    The reproducibility target is content-equivalence, not a byte-identical
    output file: DuckDB does not guarantee physical row order on disk, so
    consumers must `ORDER BY` when row order matters.

    Args:
        tables: The tables to export.
        output: Destination DuckDB path.
        page_size: Page size for streaming records.
        overwrite: If False and `output` exists, raise `OutputExistsError`.

    Raises:
        OutputExistsError: `output` exists and `overwrite` is False.
        ValueError: `page_size` is less than 1.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    output = output.resolve()
    if output.exists() and not overwrite:
        raise OutputExistsError(f"output exists (use overwrite): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    schemas = build_schemas(tables)
    plan = resolve_dependencies(schemas)
    workspace_id, workspace_name = _workspace_meta()
    run_meta = RunMeta(
        exported_at=to_naive_utc(dt.datetime.now(dt.timezone.utc)),
        tool_version=importlib.metadata.version("latch_registry_export"),
        fglatch_version=importlib.metadata.version("fglatch"),
        latch_version=importlib.metadata.version("latch"),
        duckdb_version=importlib.metadata.version("duckdb"),
        polars_version=importlib.metadata.version("polars"),
        pyarrow_version=importlib.metadata.version("pyarrow"),
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        page_size=page_size,
    )

    fd, tmp_name = tempfile.mkstemp(suffix=".duckdb", dir=str(output.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.unlink()  # let DuckDB create the file fresh
        conn = duckdb.connect(str(tmp_path))
        try:
            write_export(schemas, plan, conn=conn, run_meta=run_meta)
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
        os.replace(tmp_path, output)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        # DuckDB names the WAL sidecar by appending ".wal" to the full database
        # filename (e.g. "foo.duckdb.wal"), not by replacing the ".duckdb" suffix.
        Path(str(tmp_path) + ".wal").unlink(missing_ok=True)
        raise


def load_report(db_path: Path) -> ExportReport:
    """
    Read an `ExportReport` back from a finished export's `_export_*` tables.

    Args:
        db_path: Path to a DuckDB file produced by `export`.

    Returns:
        The reconstructed report.

    Raises:
        ValueError: `db_path`'s `_export_run` table has no row (an empty or
            interrupted export).
        duckdb.Error: `db_path` is not a file produced by `export` (e.g. it
            lacks the `_export_*` tables), surfaced as a DuckDB catalog error.
    """
    db_path = db_path.resolve()
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        run = conn.execute(
            "SELECT tool_version, fglatch_version, latch_version, duckdb_version, "
            "polars_version, pyarrow_version, workspace_id FROM _export_run"
        ).fetchone()
        if run is None:
            raise ValueError(f"no run metadata found in {db_path}")
        row_counts = dict(
            conn.execute("SELECT table_sql_name, row_count FROM _export_tables").fetchall()
        )
        issue_counts = dict(
            conn.execute(
                "SELECT issue_type, COUNT(*) FROM _export_issues GROUP BY issue_type"
            ).fetchall()
        )
        # `list_no_fk` is intentionally excluded: an array link is inherently
        # non-FK (DuckDB has no array FOREIGN KEY), never a demoted FK, so it is
        # not a "degraded" link the way a demoted back_edge/self_link is.
        degraded_status_list = ", ".join(f"'{status.value}'" for status in DEGRADED_FK_STATUSES)
        degraded = conn.execute(
            "SELECT table_sql_name, column_sql_name FROM _export_columns "
            f"WHERE fk_status IN ({degraded_status_list}) "
            "ORDER BY table_sql_name, column_sql_name"
        ).fetchall()
    finally:
        conn.close()
    return ExportReport(
        output=db_path,
        tool_version=run[0],
        dependency_versions={
            "fglatch": run[1],
            "latch": run[2],
            "duckdb": run[3],
            "polars": run[4],
            "pyarrow": run[5],
        },
        workspace_id=run[6],
        row_counts={k: int(v) for k, v in row_counts.items()},
        issue_counts={k: int(v) for k, v in issue_counts.items()},
        degraded_links=tuple(degraded),
    )
