"""The `export` CLI command."""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from latch_registry_export import export
from latch_registry_export import load_report
from latch_registry_export import load_table_configs

logger = logging.getLogger("latch_registry_export")


class LogLevel(StrEnum):
    """Logging levels accepted by the CLI."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def export_cli(
    *,
    config: Path,
    output: Path,
    page_size: int = 100,
    overwrite: bool = False,
    log_level: LogLevel = LogLevel.INFO,
) -> None:
    """
    Export configured Latch Registry tables to a DuckDB file.

    Args:
        config: Path to the TOML config listing tables to export.
        output: Destination DuckDB file.
        page_size: Number of records fetched per page.
        overwrite: Overwrite an existing output file.
        log_level: Logging level. gql request and response bodies are never logged.
    """
    logging.getLogger().setLevel(log_level)
    tables = load_table_configs(config)
    logger.info(f"Exporting {len(tables)} table(s) from {config} to {output}")
    export(tables, output=output, page_size=page_size, overwrite=overwrite)

    report = load_report(output)
    print(f"Exported to {report.output}")
    for name, count in sorted(report.row_counts.items()):
        print(f"  {name}: {count} rows")
    for kind, count in sorted(report.issue_counts.items()):
        print(f"  issue - {kind}: {count}")
    for table, field in sorted(report.degraded_links):
        print(f"  degraded link - {table}.{field}")
