"""Command-line entry point for the Latch Registry exporter."""

from __future__ import annotations

import logging
import sys

import defopt
import duckdb
from latch_sdk_gql import AuthenticationError

from latch_registry_export import ExportError
from latch_registry_export.tools.export import export_cli

logger = logging.getLogger("latch_registry_export")


def setup_logging(level: str = "INFO") -> None:
    """
    Set up basic logging to print to the console.

    The `gql` logger is capped at WARNING: its transports log every full request and
    response body at INFO, which can reach gigabytes for large tables.

    Args:
        level: The root logging level, e.g. "INFO" or "DEBUG".
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s:%(funcName)s:%(lineno)s [%(levelname)s]: %(message)s",
    )
    logging.getLogger("gql").setLevel(logging.WARNING)


def run() -> None:
    """Set up logging, then hand over to defopt for running the exporter."""
    setup_logging()
    logger.info(f"Executing: {' '.join(sys.argv)}")
    try:
        defopt.run(export_cli, argv=sys.argv[1:], version=True)
    except (ExportError, ValueError, OSError, duckdb.Error, AuthenticationError) as e:
        logger.error(str(e))
        raise SystemExit(1) from e
    logger.info("Finished executing successfully.")
