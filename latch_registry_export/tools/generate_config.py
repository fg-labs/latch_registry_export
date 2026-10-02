"""The `generate-config` CLI command."""

from __future__ import annotations

import logging
from pathlib import Path

from latch_registry_export.errors import OutputExistsError
from latch_registry_export.generate import discover_tables
from latch_registry_export.generate import to_config_toml
from latch_registry_export.tools.export import LogLevel

logger = logging.getLogger("latch_registry_export")


def generate_config_cli(
    *,
    output: Path,
    overwrite: bool = False,
    log_level: LogLevel = LogLevel.INFO,
) -> None:
    """
    Write a TOML config that exports every column of every table in the workspace.

    Edit the config by hand to rename tables or remove tables and columns, then pass it
    to `export`.

    Args:
        output: Destination TOML file.
        overwrite: Overwrite an existing output file.
        log_level: Logging level. gql request and response bodies are never logged.
    """
    from latch.utils import current_workspace

    logging.getLogger().setLevel(log_level)
    if output.exists() and not overwrite:
        raise OutputExistsError(f"output exists (use overwrite): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    workspace_id = current_workspace()
    logger.info(f"Listing Registry tables in workspace {workspace_id}")
    tables = discover_tables(workspace_id=workspace_id)
    text = to_config_toml(tables, workspace_id=workspace_id)
    # Exclusive create, so a file that appeared during discovery is not replaced.
    try:
        with output.open("w" if overwrite else "x", encoding="utf-8") as fh:
            fh.write(text)
    except FileExistsError as exc:
        raise OutputExistsError(f"output exists (use overwrite): {output}") from exc
    print(f"Wrote {len(tables)} table(s) to {output}")
