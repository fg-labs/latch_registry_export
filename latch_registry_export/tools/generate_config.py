"""The `generate-config` CLI command."""

from __future__ import annotations

import logging
import os
import tempfile
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
    data = to_config_toml(tables, workspace_id=workspace_id).encode("utf-8")
    _write_atomically(output, data, overwrite=overwrite)
    print(f"Wrote {len(tables)} table(s) to {output}")


def _write_atomically(output: Path, data: bytes, *, overwrite: bool) -> None:
    """
    Write `data` to `output` in one step, so a failed write leaves `output` unchanged.

    Writes a temporary file next to `output`, then moves it into place. Without
    `overwrite`, a hard link does the move: it fails if `output` appeared since the
    caller checked, so the file is never replaced.

    Raises:
        OutputExistsError: `output` exists and `overwrite` is False.
    """
    fd, tmp_name = tempfile.mkstemp(suffix=".toml", dir=output.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            # `mkstemp` creates the file as 0600; give it the mode a plain `open` would.
            umask = os.umask(0)
            os.umask(umask)
            os.fchmod(fh.fileno(), 0o666 & ~umask)
            fh.write(data)
        if overwrite:
            os.replace(tmp_path, output)
        else:
            os.link(tmp_path, output)
    except FileExistsError as exc:
        raise OutputExistsError(f"output exists (use overwrite): {output}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
