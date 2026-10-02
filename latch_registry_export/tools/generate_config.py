"""The `generate-config` CLI command."""

from __future__ import annotations

import logging
import os
import stat
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
        overwrite: Replace an existing output file. A symlink is followed, and the file
            keeps its mode.
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
    _write_config(output, data, overwrite=overwrite)
    print(f"Wrote {len(tables)} table(s) to {output}")


def _write_config(output: Path, data: bytes, *, overwrite: bool) -> None:
    """
    Write `data` to `output` so that a failed write never leaves a partial config.

    Raises:
        OutputExistsError: `output` exists and `overwrite` is False.
    """
    if overwrite:
        _replace_file(output.resolve(), data)
    else:
        _create_file(output, data)


def _create_file(output: Path, data: bytes) -> None:
    """Create `output` exclusively, and remove it again if the write fails."""
    try:
        fh = output.open("xb")
    except FileExistsError as exc:
        raise OutputExistsError(f"output exists (use overwrite): {output}") from exc
    try:
        with fh:
            fh.write(data)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _replace_file(target: Path, data: bytes) -> None:
    """Replace `target` in one step, keeping its mode, so a failed write leaves it unchanged."""
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
